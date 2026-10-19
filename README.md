# 多智能体旅行规划 + 社区（Java + Python 双服务）

当前上线准备版本（2026-10-19）：统一编排、日常门票参考与模型估算兜底、失败草稿修复、会话鉴权、持久点赞队列、执行租约、MCP 复用及三端 CI。

生产部署与验收以 [上线准备与验收](docs/上线准备与验收-20261019.md) 为准。使用 `docker-compose.production.yml`；默认 `docker-compose.yml` 仅用于开发。Python SQLite 数据卷现在包含账号与会话，必须备份。

**一个由两个服务组成的系统**：

- **Python（FastAPI + LangGraph）** 负责 AI 能力：景点、天气、餐饮、酒店并行采集，编排 Agent 统一选择，
  产出带硬约束对账的逐日行程，支持 SSE 逐节点进度、行程库复用、断点续跑。
- **Java（Spring Boot + MySQL + Redis）** 负责业务与高并发：行程发布、**点赞排行榜**、
  唯一持有业务库连接，并承接"一人一赞 + 异步批量落库"的并发压力。

两者**双向联动**：Python 生成后交给 Java 入库（Java 回调 Python 生成简介）；
Java 统计出的社区热度又被 Python 读回去，作为下一次生成的参考 —— 形成
`用户行为 → 热度 → AI 生成 → 新行程发布` 的闭环。

> 当前 API 版本 **v0.8**；以下旧实验来自 **v0.3–v0.6**　·　Python `GET /health`、Java `GET /community/health`（后者会真检查 MySQL 与 Redis）
> 需求与设计：[`docs/需求文档.md`](docs/需求文档.md)（§13/§14 是契约冻结点）
> 排期与实验：[`docs/开发计划.md`](docs/开发计划.md)（§11 是实验设计）
> 面试准备：[`docs/面试问答库.md`](docs/面试问答库.md)

## 历史实测（旧版本，不能代表当前性能）

下表未在本次统一编排、会话鉴权与持久队列版本重新压测；169 个测试也不是当前数量。当前结果见上线验收文档。

| 指标 | 实测值 | 来源 |
|---|---|---|
| 单次 3 天规划（中位数） | **11 次 LLM 调用 / 1.8 万 token / 41.9 秒** | 21 次跑批 |
| 并行分支上的 Agent 化代价 | 省 token、**不省延迟**（43.6→44.7 秒，噪声内） | 对照实验 A |
| 天气超出预报窗口的编造率 | **0%**（18 个"超窗口天次"零编造） | 对照实验 B |
| 用户指定非最优酒店的代价 | 加权平均距离 **2.2 km → 4.3 km** | 对照实验 C |
| 行程库缓存 | 生成平均 40.3 秒 → **命中平均 2.2 秒**（约 18 倍，0 次 LLM） | v0.4 实测 |
| 上下文压缩 | prompt **21,755 → 13,549 token（省 38%）**，约束满足率不变 | 实验 D |
| 逐节点耗时 p50 | 住宿 **13.1 秒（46%）** > 编排 7.2 秒 > 采集 ~5.7 秒 > 预检 2.3 秒 | 实验 E |
| **点赞并发（300 用户 / 并发 64）** | 0.54 秒，**QPS 552**，p50 93.7ms / p95 172.3ms / **p99 237.3ms**，0 失败 | `scripts/interop_check.py` |
| **点赞并发（500 用户 / 并发 128）** | 1.10 秒，**QPS 456**，p95 315.2ms / **p99 333.8ms**，0 失败 | 同上 |
| **一致性** | 500 个并发赞**零丢失**，异步落库 3 秒后 **MySQL == Redis**，队列 0 积压 | 同上 |
| 测试 | Python **901 个单元测试**（零 token，自带禁止联网护栏） | `make test` |

## 架构（两条链路）

**一、当前行程生成**

```mermaid
flowchart TD
    A[城市与输入预检] --> B[景点/天气/餐饮/酒店并行采集]
    B --> C[地理与时长初筛/统一上下文]
    C --> D[编排 Agent 统一选择景点/三餐/酒店]
    D --> E[身份与结构/独立复核]
    E --> F[路线/日常门票/舒适度]
    F --> G[预算计算与复核]
    G -->|未通过且剩余调用预算| D
    G -->|通过| H[正式行程与缓存]
    G -->|仍未通过| I[可修复草稿与具体原因]
```

有来源的价格优先；模型只补充未核实的日常门票估算，来源标签贯穿预算和导出。注册、登录由 Python 管理，Java 通过真实会话核验身份，客户端不能指定用户 ID。生产默认单 Python worker；任务/会话写入带数据库租约与 token 校验。

**二、社区联动（Java 持库，Redis 扛并发）**

```
POST /community/itineraries      发布行程 → MySQL（Java 建库建表；Python 不碰 MySQL）
POST /api/ai/summary             Java 回调 Python 生成简介（Java → Python）
POST /community/itineraries/{id}/like
                                 Lua 原子：限频 + 一人一赞 + 计数 + ZSET 排行
                                 落库：每秒批量 500 条（最终一致，从流水重算而非累加）
GET  /community/leaderboard      热榜 = 点赞数 ÷ (小时数+2)^1.5（时间衰减，老的不霸榜）
GET  /community/hot              社区热度统计 → Python 生成前读它（反哺闭环）
```

**核心设计：规则写在工具里，不写在 prompt 里。**
去重、类目过滤、预报窗口对齐、加权选址数学全部住在 `services/tools.py`；
Agent 只负责决策（搜什么关键词、选哪家酒店）。这样既有自主性，又不丢确定性，**而且工具可以单测**。

另外四条贯穿全项目的约定：
- **外部数据不让模型复述**（天气在边界拼装、景点字段从候选池回填）——不烧 token，也不可能被编造。
- **降级必须可见**：`sources` 标出哪一路降级（含跨服务的 `community_hot`）；
  `success` 同时检查数据降级、结构约束、路线舒适度和预算结论。
- **生成者不能给自己打分**：`verify_plan` 与 `compose_plan` 的校验逻辑完全独立。
- **跨服务调用必须有超时且绝不抛异常**：Java 挂了，行程生成照常（只是没有热度反哺）。

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
make test        # Python 单元测试：901 个用例，不联网、0 成本
make lint        # 静态检查（只拦真错误：未定义名 / 未用导入 / 语法）
make cov         # 覆盖率

# 对照实验（会花真实 token）
cd backend
.\.venv\Scripts\python.exe -u scripts\experiment_v05.py --reps 3 --timing-runs 10

# 评测门禁
.\.venv\Scripts\python.exe -u scripts\eval_gate.py --mode fast   # 只跑预检类，不花 LLM，可当 PR 门禁
.\.venv\Scripts\python.exe -u scripts\eval_gate.py --mode full   # 含生成类，发版前跑

# 50 条规划场景评测（隔离数据库 + 可计算不变量；--mode fast 零 LLM，--mode full 花真实 token）
.\.venv\Scripts\python.exe -u scripts\eval_scenarios_30.py --mode fast
.\.venv\Scripts\python.exe -u scripts\eval_scenarios_30.py --mode full

# HTTP 契约 / SSE / 缓存语义（需要服务在 127.0.0.1:8000 运行）
.\.venv\Scripts\python.exe -u scripts\eval_http_contract.py

# ★ 跨服务联调 + 高并发压测（18 项断言：双向调用 / 一人一赞 / 限频 / 零丢失 / 最终一致 / 反哺闭环）
cd ..
.\backend\.venv\Scripts\python.exe -u scripts\interop_check.py --users 500 --concurrency 128
```

> 评测结果与修复清单见 [`docs/规划Agent评测与修复报告-20261019.md`](docs/规划Agent评测与修复报告-20261019.md)（基线 31/50 → 修复后 41/50，HTTP 44/44，901 项单测全绿）。
> 若本机系统代理不可用（高德/DeepSeek 直连正常），运行评测或服务前设置 `$env:NO_PROXY='*'`。

> `interop_check.py` 需要五个进程都在跑（前端可不起）：Python 8000 / Java 8080 / MySQL 3307 / Redis 6379。

## 部署

```powershell
# 需要 Docker Desktop 处于运行状态
docker compose up --build      # 统一入口 http://127.0.0.1:8080
```

**一条命令起五个服务**（v0.6 之后的形状）：

| 服务 | 作用 | 调试端口 |
|---|---|---|
| `frontend` | nginx 托管前端，反代 `/api`→Python、`/community`→Java | **8080**（唯一对外入口） |
| `backend` | Python：多智能体生成 / SSE / 生成缓存（SQLite 卷） | 8000 |
| `community` | Java：点赞 / 排行榜 / **唯一持有业务库连接** | 8081 |
| `mysql` | 业务库（用户 / 行程 / 点赞 / 热度） | 3307 |
| `redis` | 点赞去重 / ZSET 排行 / 限频 | 6379 |

几个刻意的设计：nginx 反代让前端**同源**（不存在 CORS，也只暴露一个端口）；
**SSE 那条路径单独关掉 `proxy_buffering`**（否则进度条会攒着一次性跳出来）；
两个数据卷按生命周期分开 —— SQLite（生成缓存，可丢）与 MySQL（业务数据，不可丢）。

生产要显式打开的两个开关（**默认关闭，启动时会打警告**）：

```dotenv
API_KEY=<随机串>              # 请求带 X-API-Key 或 Authorization: Bearer
RATE_LIMIT_PER_MINUTE=10      # 每客户端每分钟上限；一次规划几十秒 + 上万 token，必须限流
```

监控：Python 的 `GET /metrics` 是 Prometheus 文本格式（零依赖，数据来自 `run_log` 表）；
Java 的 `GET /community/health` 会**真去连** MySQL 与 Redis，连不上就返回 `degraded` 并说明是哪一路。

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
│   │   │   ├── llm.py           # DeepSeek 直连（chat_json / chat_text / chat_with_tools）
│   │   │   ├── tools.py         # 6 个工具 + 收集箱 + 角色权限
│   │   │   ├── java_bridge.py   # ★ 跨服务调用：读热度 / 发布行程（绝不抛异常、必有超时）
│   │   │   ├── store.py         # SQLite：行程库 / 运行日志 / journal / 任务状态
│   │   │   └── runner.py        # 后台任务 + SSE 事件 + 断点续跑
│   │   └── agents/              # 4 个 Agent + city/budget/verify_plan + 图装配
│   ├── tests/                   # 169 个单元测试
│   ├── scripts/                 # 跑批实验 + 评测门禁
│   └── data/                    # SQLite（gitignore）
├── java-community/              # ★ Java 社区服务（Spring Boot 3.3.5 + Java 17）
│   └── src/main/
│       ├── java/com/trip/community/
│       │   ├── controller/      # health / itineraries / like / leaderboard / hot
│       │   ├── service/
│       │   │   ├── LikeService.java        # 高并发核心：Lua 原子 + 队列批量异步落库
│       │   │   ├── LeaderboardService.java # 热榜（HN 式时间衰减）
│       │   │   ├── HotStatService.java     # 热度统计（反哺 Python 的数据源）
│       │   │   └── AiClientService.java    # 回调 Python 生成简介（失败降级）
│       │   ├── mapper/          # MyBatis-Plus（含"从流水重算 like_count"）
│       │   └── entity/
│       └── resources/
│           ├── lua/like.lua     # ★ 限频 + 一人一赞 + 计数 + 排行榜，一条脚本原子完成
│           ├── schema.sql       # 建表由 Java 启动时执行
│           └── application.yml
├── frontend/                    # Vue 3 + Vite + TS
│   └── src/
│       ├── views/HomeView.vue          # 表单 + 流式进度（三级降级）
│       ├── views/CommunityView.vue     # ★ 社区榜：排行榜 / 点赞 / 热度反哺的可见证据
│       ├── components/TripMap.vue      # 高德静态地图（失败自动降级自绘 SVG）
│       ├── components/ExportPanel.vue  # 导出 Markdown / 打印 / 发布到社区
│       └── services/api.ts             # Python 与 Java 两套客户端（含 SSE）
├── scripts/interop_check.py     # ★ 跨服务联调 + 高并发压测（18 项断言）
├── deploy/nginx.conf            # SPA fallback + SSE 不缓冲 + /community 反代
├── docs/                        # 需求 / 开发计划 / 面试问答库 / 库表设计
└── docker-compose.yml           # 五个服务一条命令起
```

## 技术栈

| 层 | 选型 |
|---|---|
| 编排 | LangGraph（StateGraph + 条件边 + 重试环） |
| 模型 | DeepSeek（OpenAI 兼容端点，httpx 直连） |
| AI 服务 | FastAPI + Pydantic v2（Python 3.13） |
| 业务服务 | Spring Boot 3.3.5 + MyBatis-Plus + Druid（Java 17） |
| 业务库 | MySQL 8.0（**只有 Java 连它**：建库建表、事务、唯一键兜底） |
| 缓存与排行 | Redis（Lua 原子点赞 + ZSET 排行 + 限频计数） |
| 生成缓存 | SQLite（标准库 `sqlite3`，零新依赖，**可再生**） |
| 外部数据 | 高德开放平台 Web 服务 REST |
| 前端 | Vue 3 + Vite + TypeScript + ant-design-vue |
| 地图 | 高德**静态地图** REST（用 Web 服务 Key；失败自动降级自绘 SVG） |
| 导出 | 原生 Blob 下载 Markdown + `window.print()` |
| 质量 | pytest（169 用例）+ ruff + vue-tsc + GitHub Actions |

## 五个必须记住的坑

1. **高德 Key 有两种类型，不能混用。** 后端用「Web 服务」Key，前端 JS 地图用「Web 端 (JS API)」Key。
   本项目前端用的是**静态地图 REST**，所以 Web 服务 Key 就够 —— 失败时会自动降级为自绘 SVG。
2. **天气接口的 `city` 参数必须是 adcode**（不是城市名），且 `extensions=all` **只返回今天起 4 天**。
3. **日志与文档一律不用 emoji。** Windows 中文控制台是 GBK，emoji 会让日志/启动直接抛异常。
4. **写进 `TripState` 的每个键都必须在 `state.py` 声明。** LangGraph 对未声明的键**静默丢弃** ——
   我们曾因此把住宿费算成 `260 × 0 = 0`，请求不报错、日志不告警。
5. **强制 JSON 模式的接口不能拿来做自由文本生成。** 用 `chat_json`（带
   `response_format: json_object`）要一句简介，DeepSeek 直接返回 HTTP 400
   （`Prompt must contain the word 'json'`）。要自由文本请用 `chat_text`。

更多踩坑记录见 [`docs/需求文档.md`](docs/需求文档.md) 第 7.3 节与 §13.5 / §14.5。

## 已知局限（主动写出来）

- **样本小**：对照实验每个条件 3 次、单城市（杭州）；结论是趋势，不是统计显著。
- **压测是单机短跑**：500 个用户、1.1 秒的瞬时并发；没有长时间稳态压测，也没测过限流打满后的表现。
- **点赞落库没有投递保证**（原先写"至少一次"是错的，已改正）：待落库的流水放在进程内的
  `LinkedBlockingQueue` 里，**进程崩溃就会丢**；队列满时 Redis 已经接受了点赞、而流水被丢弃。
  所以现在只能说"尽力而为"，不能说"至少一次交付"。要真正保证，需要持久事件队列 + 幂等消费 + 对账，
  或者用数据库事务作为事实来源。短时 QPS 压测**不能**替代崩溃恢复测试。
- **没有统一鉴权**：Java 侧用 `X-User-Id` 头占位（前端生成一个本地游客 ID），
  可被伪造 —— 真正的做法是 JWT（Java 签发、Python 用同一 secret 校验），属于微服务范畴，还没做。
- **`docs/` 文档还没覆盖 Java 服务**：需求文档/开发计划目前只写到 v0.5（Python 侧），
  v0.6 的跨服务契约只在 README 与代码注释里。
- **评测集**：`eval_gate.py` 7 个用例 + 新增 50 条规划场景（`eval_scenarios_30.py`）与 44 项 HTTP 契约（`eval_http_contract.py`）；
  生成类仍是单次观测（模型温度 0 但非逐位可复现），长行程 JSON 截断与部分城市餐饮覆盖仍是已知缺口。
- **无人工介入**：改某一天、确认酒店这类 human-in-the-loop 还没做（12-Factor F7）。
- **无 OTel 追踪**：有 `tool_calls` / `usage` / journal，但没有跨服务的分布式追踪（traceId 未贯通）。
- **镜像未在本机构建验证**：Docker Desktop 守护进程没启动，只做了 `docker compose config` 客户端校验。

## 下一步

1. 前端补齐：行程详情页（点榜单卡片进去看完整行程）
2. 统一鉴权（JWT：Java 签发、Python 校验）—— 需要先补微服务的基础知识
3. 社区行程复用：Python 生成前先查 Java 有没有已发布的同款（把单机缓存升级成跨用户复用）
4. `sqlite → Postgres` 迁移 + 迁移脚本（alembic 之类）
5. OpenTelemetry 追踪（含跨服务 traceId 贯通）与告警规则
6. 评测集扩充（多城市、多偏好组合）并接进 CI 夜间任务
