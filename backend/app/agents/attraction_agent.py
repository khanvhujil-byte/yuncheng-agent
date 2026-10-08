"""Agent 1 / 4 · 景点搜集专家（collect_attractions 节点）。

职责：把「用户的偏好 + 用户指定的必去景点」变成一份**候选景点池**，供编排节点挑选。

    输入（从 State 读）：request.city、request.preferences、must_visit_resolved
    输出（写回 State）：{"attractions": [Attraction, ...], "sources": {"attractions": ...}}

目标条数：>= travel_days * 3（每天按 3 个候选准备，因为编排每天只用 2~3 个）

必须做对的四件事（需求文档 3.2）：
    1. **每个偏好标签各查一次再合并**。
       参考项目只取 preferences[0]（keywords = request.preferences[0]），
       用户勾了「自然风光 + 美食」就只搜了自然风光。这是缺陷，不要照抄。
    2. **按 poi_id 去重**，不要按 name。
       高德会返回「西湖」和「西湖风景区」这类近重复项。
    3. **按 typecode 排除餐饮与住宿**（实测前缀：05=餐饮服务、10=住宿服务）。
       用「美食」当关键词搜会搜出一堆餐馆，那些不是景点。
       注意：只排"确定的非景点"，其余杂项交给编排节点做语义判断（理由见 _is_attraction）。
    4. **必去景点（锚点）无条件注入候选池** —— 偏好标签搜不到它，它也必须在这里。

降级要求（需求文档 FR-03）：
    工具调用失败不抛异常向上冒。写 errors + warnings，返回手里已有的，
    让 compose_plan 照常执行 —— 最终由 Day 4 的「约束满足性校验」把
    「必去景点没安排上」如实写进 ConstraintReport，而不是在这里静默丢掉。
"""

import logging

from ..models.schemas import Attraction
from ..models.state import TripState
from ..services.amap import AmapError, AmapUnreachable, search_poi

logger = logging.getLogger(__name__)

TARGET_PER_DAY = 3  # 每天备几个候选（编排每天实际用 2~3 个）
PAGE_SIZE = 20  # 每次搜索取多少条
MAX_PAGES = 2  # 每个标签最多翻几页 —— 配额保护：标签数 × 页数 次调用
EXCLUDED_TYPECODE_PREFIXES = ("05", "10")  # 05=餐饮服务 10=住宿服务（实测确认）
FALLBACK_KEYWORD = "景点"  # 用户一个偏好都没勾时的兜底关键词


def _is_attraction(poi: Attraction) -> bool:
    """按 typecode 排除餐饮与住宿 —— **只排"确定的非景点"**。

    三条实测教训（决定了现在这个写法）：

    1) typecode 可能是多个码用 "|" 拼接（如 "060101|061101|061000"），必须先 split。
    2) 判定用 any() 而不是 all()：只要**有任何一个**码是餐饮/住宿就排除。
       实测反例 —— 用「美食」搜出来的「新发现·甄选(来福士店)」（美容美发店）
       typecode 是 "050106|071100"，宽松规则（要求全中）会把它漏进候选池。
    3) **不能改成白名单**：试过只保留 11(风景名胜)/14(科教文化)/08(体育休闲)，
       但「河坊街」的 typecode 是 "190301"（地名地址信息;道路名）—— 一个真实可玩的
       街区会被误杀。宁可按需求文档只排餐饮与住宿。

    剩下的杂项（品牌服装店、纯道路名）**故意不在这里排**：那些属于语义判断，
    交给编排节点（模型有常识知道"品牌服装店"不是景点）。
    规则只做确定性的事，语义判断给模型 —— 这条原则在整个项目里保持一致。
    typecode 缺失时保留：宁可多留一条，也不静默丢数据。
    """
    codes = [code for code in (poi.typecode or "").split("|") if code]
    if not codes:
        return True
    return not any(code[:2] in EXCLUDED_TYPECODE_PREFIXES for code in codes)


def _degraded(by_id: dict[str, Attraction], errors: list[str], warnings: list[str], note: str) -> dict:
    """降级返回：把**手里已经有的**（至少是必去景点锚点）交出去，让主链路继续。"""
    logger.warning("景点采集降级 | %s | 已取到 %d 条", note, len(by_id))
    return {
        "attractions": list(by_id.values()),
        "errors": errors,
        "warnings": warnings,
        "sources": {"attractions": "degraded"},
    }


def collect_attractions(state: TripState) -> dict:
    req = state["request"]
    travel_days = state["travel_days"]
    target = travel_days * TARGET_PER_DAY

    anchors = list(state.get("must_visit_resolved") or [])
    errors: list[str] = []
    warnings: list[str] = []
    dropped = 0

    # ---- 第 1 步：硬约束锚点先入池 ----
    # 先放锚点、后放搜索结果，是为了让「先来的赢」：同一个 poi_id 若在偏好搜索里
    # 又出现一次，我们跳过它 —— 否则会把 required=True 的版本覆盖成 required=False。
    by_id: dict[str, Attraction] = {}
    for anchor in anchors:
        by_id[anchor.poi_id or anchor.name] = anchor

    # ---- 第 2 步：每个偏好标签各搜一次再合并 ----
    keywords = [p.strip() for p in (req.preferences or []) if p and p.strip()]
    if not keywords:
        keywords = [FALLBACK_KEYWORD]

    for keyword in keywords:
        for page in range(1, MAX_PAGES + 1):
            try:
                hits = search_poi(keyword, req.city, offset=PAGE_SIZE, page=page)
            except AmapError as exc:
                # 配置/配额类：如实记录原码（预检阶段已经拦过一轮 Key 错误，
                # 到这里再遇到多半是配额用尽），然后降级 —— 不抛异常打断整条请求。
                errors.append(f"{exc.code}: 搜索「{keyword}」失败（{exc.detail}）")
                warnings.append(f"景点数据获取失败（{exc.code}），本次行程不含景点推荐。")
                return _degraded(by_id, errors, warnings, f"AmapError {exc.code}")
            except AmapUnreachable as exc:
                errors.append(f"UPSTREAM_TIMEOUT: 搜索「{keyword}」失败（{exc}）")
                warnings.append("景点数据获取失败（网络不可达），本次行程不含景点推荐。")
                return _degraded(by_id, errors, warnings, "AmapUnreachable")

            if not hits:
                break  # 该关键词没有更多结果，换下一个关键词

            for poi in hits:
                if not _is_attraction(poi):
                    dropped += 1
                    continue
                key = poi.poi_id or poi.name  # poi_id 优先；极少数没有 id 的用名字兜底
                if key in by_id:
                    continue  # 已存在（可能是带 required=True 的锚点版本）→ 保留已有的
                by_id[key] = poi

            if len(hits) < PAGE_SIZE:
                break  # 最后一页了，别再翻

    result = list(by_id.values())

    # ---- 第 3 步：如实报告「不够」和「过滤掉多少」----
    if len(result) < target:
        warnings.append(
            f"景点候选只有 {len(result)} 个，少于目标 {target} 个"
            f"（{travel_days} 天 × {TARGET_PER_DAY}）；编排会按实际数量安排，不凑数。"
        )

    logger.info(
        "景点采集完成 | 关键词=%s | 候选=%d（目标 %d）| 过滤餐饮住宿=%d | 锚点=%d",
        keywords,
        len(result),
        target,
        dropped,
        len(anchors),
    )

    return {
        "attractions": result,
        "errors": errors,
        "warnings": warnings,
        "sources": {"attractions": "amap"},
    }
