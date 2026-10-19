# 变更记录

本项目的版本节奏是"契约冻结点 + 一次架构演进"，每版都有明确的主题与实测证据。

## 2026-10-19 · 规划 Agent 评测与修复（评测驱动的一轮质量加固）

**评测**
- 新增 50 条规划场景评测集 `backend/scripts/eval_scenarios_30.py`（14 预检 + 36 真实生成，隔离数据库、逐例超时看门狗、可计算不变量）。
- 新增 44 项 HTTP 契约/生命周期/缓存语义评测 `backend/scripts/eval_http_contract.py`。
- 参考 9 个公开基准（TravelPlanner / ChinaTravel / TravelBench / TREK / TripTide / TRIP-Bench / AgentBench / tau-bench / ITBench）与 67 条场景草案：`docs/评测集调研与场景草案.md`。
- 报告：`docs/规划Agent评测与修复报告-20261019.md`；静态审计：`docs/审计发现-核心链路-20261019.md`、`docs/审计发现-服务层-20261019.md`。

**结果**
- 基线 31/50（62%）→ 修复后整体 41/50（82%）；HTTP 契约 44/44；后端 901 项测试全绿。
- 基线 4 条"3 天 660 分钟"失败全部修复；固定酒店/必去锚点歧义通过补全名称或地址可正常工作。

**主要修复**
- 统一编排的确定性完成修复：删可选景点（必去锚点/用户编辑餐次受保护）、跨天去重、提前晚餐压低固定餐窗地板；修复删点循环读旧时间表导致的过度删点。
- 编排输出的 JSON 健壮性：按天数自适应输出 token 预算 + 同节点格式修复重试（可开关对照）。
- 重排预算拆分：`repair_rounds` 与 `budget_retries` 独立计数，新增 `prepare_repair` 节点；修正 verify_plan 的契约说明。
- 缓存与任务：复核不通过的缓存删除、重启遗留 running 任务回收、缓存读取/写入异常隔离。
- 契约与可观测性：请求字段长度上限、预检失败原因透传、`CLARIFICATION_REQUIRED` 文案、监控不再把预检拒绝算成规划失败、异步缓存命中 success 与复核结论一致。
- 修复 `audit_choices` 在餐饮候选缺失时的 KeyError；餐饮距离规则对"附近无候选"降级为可见 warning。

**新增测试**
- `tests/test_completion_repair.py`（9 项）、`tests/test_hardening_20261019.py`（8 项）。

## v0.5 · 上下文压缩 / 后台任务与 SSE / journal 续跑 / 独立评估者（2026-10-10）

**能力**
- 上下文压缩：送进编排 prompt 的候选按预算裁剪（锚点必留）。实测 A/B：**21,755 → 13,549 token（省 38%）**，
  且两臂的硬约束满足率都是 100%、行程规模基本不变 —— 省 token 没有牺牲质量。
- 后台任务 + SSE 逐节点进度：`POST /api/trip/plan/async` + `GET /api/trip/plan/stream/{id}`（+ `/result` 轮询兜底）。
  **它让瓶颈第一次可见**：逐节点时间线显示住宿那一步花 98 秒（编排只 18.7 秒）→ 修复后 13.1 秒，端到端 116.4 → 31.1 秒。
- journal 断点续跑：节点产出落盘 + `build_graph(start_at=...)` 从断点继续。
  实测：从 `resolve_hotel` 续跑 **22.8 秒**出完整行程。
- 独立评估者节点 `verify_plan`（纯代码，**生成者不能给自己打分**）：地理跨度 / 时间可行性 / 重复排期 / 单日费用。
- 工具并发：同一轮的多个 tool_call 并发执行。

**工程**
- **152 个单元测试**（零 token，测试自带"禁止联网"护栏）+ ruff 静态检查 + GitHub Actions CI。
- Docker 化：`backend/Dockerfile`（多阶段不适用，依赖层缓存）、`frontend/Dockerfile`（node 构建 → nginx 托管）、
  `deploy/nginx.conf`（SPA fallback + **SSE 单独关缓冲**）、`docker-compose.yml`（行程库挂 volume）。
- 鉴权与限流：`API_KEY` / `RATE_LIMIT_PER_MINUTE`（**默认关，启动时警告**），支持 `X-API-Key` 与 Bearer。
- 监控：`GET /metrics`（Prometheus 文本格式，手工拼、零依赖，数据全部来自 `run_log`）。
- 任务状态外置：`job_state` 表 —— 内存里的 `_jobs` 只是缓存，多副本/重启后 `/result` 仍可用。
- 评测集与回归门禁：`scripts/eval_gate.py`（fast 模式不花 LLM，可当 PR 门禁）。
- Makefile / CHANGELOG / LICENSE / `.dockerignore`。
- 文档升到 v0.5 口径（需求文档 §14、开发计划 §11.5/§11.6）。

**修复**
- `patch_budget` 用浅拷贝改到了入参（测试抓出来的真 bug）。

## v0.4 · 行程库与相似请求复用（2026-10-10）

- 新模块 `services/store.py`（SQLite，标准库，零新依赖）：`itinerary`（指纹 → 行程）+ `run_log`（每次运行指标）。
- `POST /api/trip/plan` 变成两条路径：命中行程库（**平移日期 + 重取天气**）或完整生成后入库。
- `GET /api/trip/stats`：命中率、命中/未命中的平均耗时、累计 token。
- 实测：首次 45.2 秒 → 相同请求 3.7 秒 → 只改日期 2.4 秒；生成平均 40.3 秒 vs 命中平均 2.2 秒（约 18 倍）。
- 三个设计决定：指纹**不含出发日期**；命中后**仍重取天气**（天气与日期绑定，复用旧天气就是编造）；
  只有"完整且无降级"的行程才入库（否则把一次故障永久固化）。

## v0.3 · 多智能体 + 工具驱动（2026-10-10）

- 架构从"代码节点图"改成 **4 个 Agent（3 个带工具）+ 2 个代码节点**，新增 `services/tools.py`（6 个工具 +
  收集箱 + 角色权限）与 `agents/react_loop.py`（共用 ReAct 循环）。
- **确定性逻辑下沉到工具层**：去重、类目过滤、窗口对齐、选址数学都在工具里，Agent 只做决策。
- 弃用 langchain-openai（1.6.2 与 openai SDK 3.16.2 组合会让 `ChatOpenAI(...)` 构造挂死），改 httpx 直连。
- 实测：单次 3 天规划 11 次 LLM 调用 / 1.8 万 token / 41.9 秒（21 次运行中位数）。
- 对照实验（21/21 成功）：**并行分支上的 Agent 化只增加 token、不增加延迟**；
  天气超窗口 18 个"天次"**编造率为 0**；用户指定非最优酒店使加权距离从 2.2 km 涨到 4.3 km。

## v0.2 · 契约冻结（2026-10-07/08）

- 住宿后置（方案 D）、行程级住宿、锚点模型（必去景点 / 已定酒店）、天数·晚数·日期单一真相源。
- 两层校验（Pydantic 结构 + 硬约束满足性）+ `ConstraintReport` 对账。
- 可行性预检：**花钱之前拦住**不可行的请求（容量 / 锚点可解析性 / 日期一致 / 晚数范围）。

## v0.1 · 后端骨架与数据契约（2026-09-23）

- FastAPI + LangGraph 骨架；`config.py` 三层配置校验；`schemas.py` 请求/响应契约；前端接通。
