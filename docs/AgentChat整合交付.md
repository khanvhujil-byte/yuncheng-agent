# AgentChat 思路整合与工程整改

这次把此前讨论的改造一次落到旅游助手：保留 LangGraph 和 ToolContext，补齐会话入口、显式长期偏好、标准 MCP、资料检索和在线质量统计。对 AgentChat 的借鉴是架构思路，未将它整仓替换成本项目，也不把第三方成果归为独立原创。

## 使用入口

后端仍按原方式在 `backend` 启动：

```powershell
.\.venv\Scripts\python.exe run.py
```

前端在 `frontend` 执行 `npm run dev`，打开 `http://localhost:5173/chat`，或从首页点击“对话规划与偏好”。先注册并登录，再填写或描述旅行要求；确认要求后才调用规划图。新注册账号的交通和住宿偏好尚未保存，需要在本次要求中填写。

- 提要求：自然语言解析成受限意图与槽位增量，也可展开结构化表单直接编辑。
- 确认：生成一个独立 run_id；会话使用长期不变的 conversation_id。
- 修改：先保存草稿，不覆盖已确认行程；再次确认才生成新版本。
- 撤销：恢复最近成功版本的请求与行程。
- 失败：保留旧行程，另存失败候选；有部分结果也显示具体失败原因。
- 断网：用原 request_id 重试查询操作结果，避免重复生成；刷新后保留待确认操作。
- 长期偏好：只在偏好面板明确保存后跨会话生效，支持查看、版本更新和撤回；本次临时要求不会自动写入。
- 私人资料：添加文本及出处后可问预约、退改等问题；回答展示原文和引用，未召回时明确拒答。资料只对所属用户可见。
- 运行质量：显示实际样本中的工具首次成功率、重试率、最终成功率和告警。没有样本时不生成业绩数据。

原有表单规划仍可使用。异步任务等待超时后只查询原任务，提供“查看上次任务结果”和断点继续；仅确认旧后端缺少异步接口（404/405）时才使用同步兼容路径。

## 核心实现与边界

```text
表单 / 多轮会话
  -> services/planning.py：统一预检、缓存、规划与最终交付审计
  -> LangGraph：城市预检 -> 景点与天气并行 -> 编排与独立复核
                -> 酒店 -> 预算 -> 有界重排
  -> ToolContext：事实候选池、天气、酒店选择、调用证据
  -> ToolResult：status / error_code / retryable / message / stats
  -> map_gateway：本地高德 或 标准 MCP
  -> SQLite：会话、版本、偏好审计、资料、运行快照、质量记录
```

1. 意图路由由模型输出受校验的枚举；工具路由由角色白名单和依赖调度决定。景点与天气汇齐后才编排，酒店搜索在排序前，模型读取新结果后才做后续选择。
2. 会话版本 CAS 与处理锁防止并发覆盖；request_id 与输入摘要防止同一个编号被用于不同操作。候选结果先持久化，再提交会话，失败后可以恢复已完成结果。进程中断且结果未知时不自动重做昂贵调用，需要用户检查后明确重新提交。
3. 登录使用加盐 PBKDF2 密码摘要，服务端只存会话令牌摘要；令牌支持过期和注销。会话、偏好、资料和会话运行记录检查用户归属；会话缓存按用户隔离。表单规划保留原有公共接口和 API Key 语义。
4. 短期上下文从结构化槽位和最近六轮的有界文本构建；事实来自领域快照。预算、交通或住宿改变时可复用五分钟内且目的地、日期、兴趣、必去锚点一致的景点和天气，再编排并重新选酒店。日期或召回条件改变、快照过期或数据曾降级时重新采集。
5. 固定酒店按住宿类目、名称、地址和城市距离解析，重名要求消歧，解析后硬性锁定；不再由模型换店。零晚住宿不调用酒店模型，也不记为数据降级或未知住宿费用。
6. 出口独立检查天数、日期、索引、候选身份、必去约束、酒店和时间可行性。错误保留、失败不缓存；JSON 根类型错误进入有界修正，不导致程序崩溃。已有候选身份时不能用同名景点绕过身份检查。
7. 费用用代码汇总；区分已知、估算、未知；预算重排最多两次，累计 token 不因重排清零。API Key 无效等确定性错误不重试，安全只读工具采用次数与时间双上限。
8. 429 解析 Retry-After 秒数或 HTTP 日期，触发共享冷却和请求间隔控制；记录首次尝试及重试后的真实结果。标准 MCP 调用也经过父进程共享请求门控。
9. 资料检索实现分块、重叠、中文二元分词、有限同义改写、正文 BM25 与标题召回、RRF 融合、重排、低相关拒答、有效期过滤和出处引用。这里是小规模文本检索，未声称实现向量语义召回或训练过的 reranker。
10. 在线监控使用确定性规则，识别高重试率、429、搜索空结果和规划失败率。最终成功不能掩盖首次失败。监控不增加一次模型调用来“猜测”系统是否正常。

## MCP 配置

已安装官方 `mcp==1.30.0` 和 JSON Schema 校验依赖，支持 stdio 与 Streamable HTTP。默认 `MAP_BACKEND=local`，原有高德适配器继续工作。切换 stdio 时在 `backend/.env` 设置：

```dotenv
MAP_BACKEND=mcp
MCP_TRANSPORT=stdio
MCP_COMMAND=C:/Users/34397/CodeBuddy/week1/trip-planner-agent/backend/.venv/Scripts/python.exe
MCP_ARGS=["-m","app.mcp_server"]
MCP_TIMEOUT=20
```

从 backend 目录启动。内置 MCP server 自己读取该目录下的高德配置；父服务的地图后端设为 MCP 时不再探测本地高德 Key。远程模式使用：

```dotenv
MAP_BACKEND=mcp
MCP_TRANSPORT=streamable_http
MCP_URL=http://127.0.0.1:8001/mcp
MCP_HEADERS={}
```

远端需暴露 `geocode/search_poi/get_weather/measure_distance` 四个只读工具，返回 `{status:"ok",data:...}` 或 `{status:"error",error_code:...,retryable:...,retry_after_seconds:...}`。客户端会 initialize、分页发现工具、校验参数，再校验领域结果；拒绝工具白名单外调用。命令、地址与认证头来自部署配置，不接受用户通过聊天修改。

## API 与验证

新接口位于 `/api/auth/*`、`/api/conversations/*`、`/api/preferences`、`/api/knowledge/*`、`/api/observability/*`。登录后带 `X-Session-Token`；若开启原全局 API Key，仍需原 `X-API-Key`。前端可通过 `VITE_API_KEY` 与现有部署配置对应。`/metrics` 补充首次成功率、重试率等指标。详细请求契约可在 `/docs` 查看。

复跑：

```powershell
# backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check app tests scripts/eval_integrated.py
.\.venv\Scripts\python.exe scripts/eval_integrated.py --output eval-integrated.json

# frontend
npm test
npm run build
```

测试使用独立临时数据库，禁止外网连接。MCP 测试确实启动独立进程，完成官方 SDK 的 stdio 和本机 HTTP 往返；贯穿测试运行真实 HTTP 路由、会话、规划图、工具调度和交付逻辑，只替换外部地图、社区与模型 I/O。评测输出逐案例结果和失败退出码，不把固定数据回归当作真实模型能力或线上成功率。

当前设计面向单进程单 worker：SQLite 处理锁、内存任务执行保护和 Key 请求门控不能直接宣称支持多进程部署。进程中断发生在外部调用与本地落盘之间时，无法保证外部调用 exactly-once；本项目选择阻止未知操作被自动重做。生产集群需要共享租约、队列、分布式限流和更细的权限管理。此次未调用真实供应商做付费评测。

## 参考来源

- AgentChat：[Shy2593666979/AgentChat](https://github.com/Shy2593666979/AgentChat)，参考其会话服务分层、用户资源隔离、MCP 会话生命周期和 RAG 分层思路。原仓库使用 MIT 许可证。
- 本项目保留并加强原旅游业务的候选池溯源、约束对账、预算确定性计算、有界重试与 journal 恢复。
- MCP 使用官方 Python SDK；新增代码围绕本项目领域契约实现，未将其他项目的整仓代码改名归为独立作品。
