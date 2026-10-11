# 多智能体旅行规划助手

[![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)

基于 **LangGraph** 的多智能体旅行规划服务：4 个专职 Agent 分工协作（景点搜集 / 天气 / 住宿 / 行程编排），
产出结构化的逐日行程，带硬约束对账、行程库复用、后台任务与断点续跑。

> 版本 **v0.5**　·　`GET /health` 与 `/docs` 可看当前版本
> 需求与设计：[`docs/需求文档.md`](docs/需求文档.md)（§13/§14 是契约冻结点）
> 排期与实验：[`docs/开发计划.md`](docs/开发计划.md)（§11 是实验设计）
> 面试准备：[`docs/面试问答库.md`](docs/面试问答库.md)

## 实测数字（不是估的）

| 指标 | 实测值 | 来源 |
|---|---|---|
| 单次 3 天规划（中位数） | **11 次 LLM 调用 / 1.8 万 token / 41.9 秒** | 21 次跑批 |
| 并行分支上的 Agent 化代价 | 省 token、**不省延迟**（43.6→44.7 秒，噪声内） | 对照实验 A |
| 天气超出预报窗口的编造率 | **0%**（18 个"超窗口天次"零编造） | 对照实验 B |
| 用户指定非最优酒店的代价 | 加权平均距离 **2.2 km → 4.3 km** | 对照实验 C |
| 行程库缓存 | 生成平均 40.3 秒 → **命中平均 2.2 秒**（约 18 倍，0 次 LLM） | v0.4 实测 |
| 上下文压缩 | prompt **21,755 → 13,549 token（省 38%）**，约束满足率不变 | 实验 D |
| 逐节点耗时 p50 | 住宿 **13.1 秒（46%）** > 编排 7.2 秒 > 采集 ~5.7 秒 > 预检 2.3 秒 | 实验 E |
| 测试 | **152 个单元测试**（零 token，自带禁止联网护栏） | `make test` |

## 架构：4 个 Agent + 3 个代码节点 + 6 个工具

```
START → validate_city（代码：预检 + 锚点解析，0 次 LLM）
      → collect_attractions（Agent 1，工具 search_attractions）
        ∥ collect_weather（Agent 2，工具 get_weather，可用开关关掉）
      → compose_plan（Agent 3，无工具：生成 + 两层校验 + 回喂重试）
      → verify_plan（代码：独立复核 —— 时间可行性 / 重复排期 / 地理跨度）
      → resolve_hotel（Agent 4，工具 search_hotels / rank_hotels / measure_distance / choose_hotel）
      → patch_budget（代码：加法）
      → END
```

**核心设计：规则写在工具里，不写在 prompt 里。**
去重、类目过滤、预报窗口对齐、加权选址数学全部住在 `services/tools.py`；
Agent 只负责决策（搜什么关键词、选哪家酒店）。这样既有自主性，又不丢确定性，**而且工具可以单测**。

另外三条贯穿全项目的约定：
- **外部数据不让模型复述**（天气在边界拼装、景点字段从候选池回填）——不烧 token，也不可能被编造。
- **降级必须可见**：`sources` 标出哪一路降级；`success` 的判据是"有没有 degraded"，不是"warnings 是否为空"。
- **生成者不能给自己打分**：`verify_plan` 与 `compose_plan` 的校验逻辑完全独立。

## 快速开始

```powershell
# 后端
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # 填入 LLM_API_KEY 与 AMAP_API_KEY
python run.py                   # http://127.0.0.1:8000  （/docs 看接口）

# 前端（另开一个终端）
cd frontend
npm install
npm run dev                     # http://127.0.0.1:5173
```

## 测试 / 实验 / 评测

```powershell
make test        # 单元测试：152 个用例，不联网、0 成本
make lint        # 静态检查（只拦真错误：未定义名 / 未用导入 / 语法）
make cov         # 覆盖率

# 对照实验（会花真实 token）
cd backend
.\.venv\Scripts\python.exe -u scripts\experiment_v05.py --reps 3 --timing-runs 10

# 评测门禁
.\.venv\Scripts\python.exe -u scripts\eval_gate.py --mode fast   # 只跑预检类，不花 LLM，可当 PR 门禁
.\.venv\Scripts\python.exe -u scripts\eval_gate.py --mode full   # 含生成类，发版前跑
```

## 部署

```powershell
# 需要 Docker Desktop 处于运行状态
docker compose up --build      # 前端 http://127.0.0.1:8080，后端 http://127.0.0.1:8000
```

一条命令起前后端：nginx 托管前端并把 `/api` 反代到后端（同源就没有 CORS），
**SSE 那条路径单独关掉了 `proxy_buffering`**（否则进度条会攒着一次性跳出来）；
行程库挂成 volume，容器重建数据还在。

生产要显式打开的两个开关（**默认关闭，启动时会打警告**）：

```dotenv
API_KEY=<随机串>              # 请求带 X-API-Key 或 Authorization: Bearer
RATE_LIMIT_PER_MINUTE=10      # 每客户端每分钟上限；一次规划几十秒 + 上万 token，必须限流
```

监控：`GET /metrics` 是 Prometheus 文本格式（零依赖，数据来自 `run_log` 表）。

## 目录结构

```
trip-planner-agent/
├── backend/                     # FastAPI + LangGraph
│   ├── app/
│   │   ├── config.py            # 配置 + 三层校验 + 功能开关
│   │   ├── api/
│   │   │   ├── main.py          # 应用装配 / CORS / /health / /metrics
│   │   │   ├── security.py      # API Key 鉴权 + 滑动窗口限流
│   │   │   └── routes/trip.py   # 同步 / 异步 / SSE / 续跑 / 统计（唯一组装点）
│   │   ├── models/
│   │   │   ├── schemas.py       # 对外契约
│   │   │   └── state.py         # 图状态 + 四个 reducer
│   │   ├── services/
│   │   │   ├── amap.py          # 高德 REST 适配（错误分类 + 脏数据清洗）
│   │   │   ├── llm.py           # DeepSeek 直连（chat_json / chat_with_tools）
│   │   │   ├── tools.py         # 6 个工具 + 收集箱 + 角色权限
│   │   │   ├── store.py         # SQLite：行程库 / 运行日志 / journal / 任务状态
│   │   │   └── runner.py        # 后台任务 + SSE 事件 + 断点续跑
│   │   └── agents/              # 4 个 Agent + city/budget/verify_plan + 图装配
│   ├── tests/                   # 152 个单元测试
│   ├── scripts/                 # 跑批实验 + 评测门禁
│   └── data/                    # SQLite（gitignore）
├── frontend/                    # Vue 3 + Vite + TS
│   └── src/
│       ├── views/HomeView.vue          # 表单 + 流式进度（三级降级）
│       ├── components/TripMap.vue      # 高德静态地图（失败自动降级自绘 SVG）
│       ├── components/ExportPanel.vue  # 导出 Markdown / 打印
│       └── services/api.ts             # 同步 + SSE 两条客户端
├── deploy/nginx.conf            # SPA fallback + SSE 不缓冲
├── docs/                        # 需求 / 开发计划 / 面试问答库
└── docker-compose.yml
```

## 技术栈

| 层 | 选型 |
|---|---|
| 编排 | LangGraph（StateGraph + 条件边 + 重试环） |
| 模型 | DeepSeek（OpenAI 兼容端点，httpx 直连） |
| 后端 | FastAPI + Pydantic v2（Python 3.13） |
| 存储 | SQLite（标准库 `sqlite3`，零新依赖） |
| 外部数据 | 高德开放平台 Web 服务 REST |
| 前端 | Vue 3 + Vite + TypeScript + ant-design-vue |
| 地图 | 高德**静态地图** REST（用 Web 服务 Key；失败自动降级自绘 SVG） |
| 导出 | 原生 Blob 下载 Markdown + `window.print()` |
| 质量 | pytest（152 用例）+ ruff + GitHub Actions |

## 四个必须记住的坑

1. **高德 Key 有两种类型，不能混用。** 后端用「Web 服务」Key，前端 JS 地图用「Web 端 (JS API)」Key。
   本项目前端用的是**静态地图 REST**，所以 Web 服务 Key 就够 —— 失败时会自动降级为自绘 SVG。
2. **天气接口的 `city` 参数必须是 adcode**（不是城市名），且 `extensions=all` **只返回今天起 4 天**。
3. **日志与文档一律不用 emoji。** Windows 中文控制台是 GBK，emoji 会让日志/启动直接抛异常。
4. **写进 `TripState` 的每个键都必须在 `state.py` 声明。** LangGraph 对未声明的键**静默丢弃** ——
   我们曾因此把住宿费算成 `260 × 0 = 0`，请求不报错、日志不告警。

更多踩坑记录见 [`docs/需求文档.md`](docs/需求文档.md) 第 7.3 节与 §13.5 / §14.5。

## 已知局限（主动写出来）

- **样本小**：对照实验每个条件 3 次、单城市（杭州）；结论是趋势，不是统计显著。
- **SQLite 单机**：多副本部署需要换 Postgres 与 Redis 限流；任务状态已外置到表，但库本身还是单文件。
- **评测集偏小**：`eval_gate.py` 目前 7 个用例，覆盖"该拦的"和"编造率"，还没覆盖行程质量的主观维度。
- **无人工介入**：改某一天、确认酒店这类 human-in-the-loop 还没做（12-Factor F7）。
- **无 OTel 追踪**：有 `tool_calls` / `usage` / journal，但没有跨服务的分布式追踪。

## 下一步

1. 人工介入（改某一天）与结果反馈闭环
2. `sqlite → Postgres` 迁移 + 迁移脚本（alembic 之类）
3. OpenTelemetry 追踪与告警规则
4. 评测集扩充（多城市、多偏好组合）并接进 CI 夜间任务
