# 多智能体旅行规划助手

基于 **LangGraph** 的多智能体旅行规划 Web 应用。四个助手分工协作（景点搜集 / 天气查询 / 酒店推荐 / 行程编排），产出结构化的逐日行程。

需求与设计详见 [`docs/需求文档.md`](docs/需求文档.md)，排期详见 [`docs/开发计划.md`](docs/开发计划.md)。

## 四个 Agent

| # | Agent | 实现文件 |
|---|---|---|
| 1 | 景点搜集专家 | `backend/app/agents/attraction_agent.py` |
| 2 | 天气查询专家 | `backend/app/agents/weather_agent.py` |
| 3 | 酒店推荐专家 | `backend/app/agents/hotel_agent.py` |
| 4 | 行程编排专家 | `backend/app/agents/planner_agent.py` |

四个 Agent 的 system prompt 集中在 `backend/app/agents/prompts.py`。
另有三个不进 LLM 的辅助节点：`city.py`（城市解析）、`budget.py`（预算加总）、`graph.py`（图装配）。

## 当前状态

**骨架阶段** —— 目录结构已建好，业务逻辑尚未实现。

每个功能文件的正文就是一段 docstring，写明了「第几天实现、要写哪个函数、要注意什么」。
唯一的例外是前端的入口样板（`main.ts` / `App.vue` / `vite.config.ts` 等），不写就跑不起来。

## 目录结构

```
trip-planner-agent/
├── docs/
│   ├── 需求文档.md              # 需求与设计（v0.1）
│   └── 开发计划.md              # 5~7 天排期
├── backend/                     # 后端（FastAPI + LangGraph）
│   ├── .env.example             # 配置模板，复制为 .env
│   ├── requirements.txt         # Python 依赖
│   ├── run.py                   # 启动入口
│   └── app/
│       ├── config.py            # 配置加载与三层校验
│       ├── api/
│       │   ├── main.py          # FastAPI 应用装配
│       │   └── routes/
│       │       └── trip.py      # POST /api/trip/plan
│       ├── models/
│       │   ├── schemas.py       # 请求/响应数据契约
│       │   └── state.py         # LangGraph 图状态 TripState
│       ├── services/
│       │   ├── amap.py          # 高德 REST 适配器
│       │   └── llm.py           # DeepSeek 客户端
│       └── agents/
│           ├── prompts.py            # 四个 Agent 的 system prompt
│           ├── city.py               # 辅助节点：城市解析（无 LLM）
│           ├── attraction_agent.py   # Agent 1 景点搜集专家
│           ├── weather_agent.py      # Agent 2 天气查询专家
│           ├── hotel_agent.py        # Agent 3 酒店推荐专家
│           ├── planner_agent.py      # Agent 4 行程编排专家
│           ├── budget.py             # 辅助节点：预算加总（无 LLM）
│           └── graph.py              # 图装配
└── frontend/                    # 前端（Vue 3 + Vite + TS + ant-design-vue）
    ├── .env.example             # 前端配置模板，复制为 .env.local
    ├── package.json             # npm 依赖
    ├── index.html
    ├── vite.config.ts
    ├── tsconfig.json
    └── src/
        ├── main.ts              # 入口样板
        ├── App.vue              # 根组件样板
        ├── router/index.ts      # 路由配置
        ├── services/api.ts      # 后端接口封装（待实现）
        ├── types/trip.ts        # 类型定义（待实现）
        └── views/HomeView.vue   # 行程规划首页（待实现）
```

## 技术栈

| 层 | 选型 |
|---|---|
| 编排 | LangGraph |
| 模型 | DeepSeek（`deepseek-chat`，走 OpenAI 兼容端点） |
| 后端 | FastAPI + Pydantic v2（Python 3.13.2） |
| 外部数据 | 高德开放平台 Web 服务 REST（直连，不走 MCP） |
| 前端 | Vue 3 + Vite + TypeScript + ant-design-vue |
| 地图 | `@amap/amap-jsapi-loader`（需 Web 端 JS API Key） |
| 导出 | `html2canvas` + `jspdf` |

## 快速开始（骨架阶段）

前端骨架是可直接启动的：

```powershell
cd frontend
npm install
npm run dev     # http://localhost:5173
```

后端要等 Day 1 实现 `config.py` / `api/main.py` 之后才能启动：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
copy .env.example .env      # 然后填入 LLM_API_KEY 和 AMAP_API_KEY
python run.py               # http://127.0.0.1:8000
```

## 三个必须记住的坑

1. **高德 Key 有两种类型，不能混用。** 后端用「Web 服务」Key，前端地图用「Web 端 (JS API)」Key。用错类型不会报错，只会地图白屏。
2. **天气接口的 `city` 参数必须是 adcode，不是城市名**；且 `extensions=all` 只返回今天起 4 天的预报。
3. **本项目的日志一律不用 emoji。** Windows 中文控制台 GBK 编码不了 emoji，会让 FastAPI startup 事件抛异常直接退出。

更多踩坑记录见 `docs/需求文档.md` 第 7.3 节。
