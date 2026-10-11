# 旅游 Agent 公开测试数据集清单

核查日期：2026-10-11。核查对象为原始论文、官方仓库、数据卡和实际数据文件；本次没有运行模型、官方评分器或复现论文结果。

优先建议：**ChinaTravel 用于国内旅行与中文需求；TREK 用于可行性、无解处理与确定性评分；TravelBench 用于后续工具调用和多轮交互。**

## 1. ChinaTravel：当前项目的第一选择

- [代码与评测](https://github.com/LAMDASZ-ML/ChinaTravel)
- [查询数据：Hugging Face](https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel)
- [查询数据：官方认可的 ModelScope 镜像](https://modelscope.cn/datasets/Cbphcr/ChinaTravel)
- [配套沙箱：Hugging Face](https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel-Sandbox)
- [配套沙箱：ModelScope 镜像](https://modelscope.cn/datasets/Cbphcr/ChinaTravel-Sandbox)
- [论文](https://arxiv.org/abs/2412.13682)

查询数据与环境数据分开发布。查询 CSV 包含自然语言、出发城市、目的城市、人数、天数和可执行约束标注；沙箱包含景点、餐馆、酒店、火车、航班、POI、地铁站，以及官方工具和评测所需的数据。

当前数据卡列出的拆分：easy 300、medium 150、human 154、human1000 测试查询 1000；还有偏好数据。TPC2026_phase2 额外提供完整 2000 条英文查询及其中 100 条官方评测子集。这些不是彼此全部独立的集合，不应直接相加作为总独立任务数。

实际下载并解析了 easy.csv，确认 300 条。第一条原始请求是：

> 当前位置上海。我一个人想去杭州玩一天，预算1500人民币，请给我一个旅行规划。

对应字段包括 `uid`, `start_city`, `target_city`, `days`, `people_number`, `hard_logic_py`, `nature_language`, `nature_language_en`。预算约束明确出现于约束标注，而不只是文本描述。

读取示例，依照实际数据卡配置：

```python
from datasets import load_dataset

queries = load_dataset("LAMDA-NeSy/ChinaTravel", "default", split="easy")
attractions = load_dataset(
    "LAMDA-NeSy/ChinaTravel-Sandbox", "attractions", split="zh"
)
```

Hugging Face 在本次环境内连接失败，但 ModelScope 的查询、数据卡和沙箱文件目录均可读取，easy.csv 已实际下载。可直接用 ModelScope 文件页面下载 CSV；正式运行原评测应下载沙箱 `raw/ChinaTravel_sandbox_zh.zip`，使用其原目录结构，不能只下载查询。

沙箱数据卡当前版本是 `2026.08.2`；中英文各有 3413 条景点、4655 条餐馆、3866 条住宿、5345 条火车、720 条航班。查询、实体名称、DSL 和沙箱版本需要匹配。Parquet 表便于分析，正式评测的原始 ZIP 是数据卡指定的权威快照。

许可：代码 MIT；查询和沙箱数据 CC BY 4.0，需保留归属和引用。

适配成本：较低到中等。预算和中文 POI 很贴近项目，但其完整任务还包含人数、跨城交通和房间等当前系统未支持的约束。可先选择系统能够完整表达的开发任务；裁剪后的任务必须单列为派生评测，不能直接沿用原基准成绩。

## 2. TREK：预算、时间可行性和无解测试的第二选择

- [代码、任务、知识库和评分器](https://github.com/TonyQJH/TREK-A-Travel-Reasoning-and-Evaluation-Kit-for-LLM-Agents-in-Complex-Trip-Planning)
- [任务 CSV](https://github.com/TonyQJH/TREK-A-Travel-Reasoning-and-Evaluation-Kit-for-LLM-Agents-in-Complex-Trip-Planning/blob/main/trek_queries.csv)
- [参考答案 JSONL](https://github.com/TonyQJH/TREK-A-Travel-Reasoning-and-Evaluation-Kit-for-LLM-Agents-in-Complex-Trip-Planning/blob/main/trek_gold.jsonl)
- [确定性评分器](https://github.com/TonyQJH/TREK-A-Travel-Reasoning-and-Evaluation-Kit-for-LLM-Agents-in-Complex-Trip-Planning/blob/main/score_trek.py)
- [论文](https://arxiv.org/abs/2607.26977)

实际下载并解析了任务 CSV 和参考答案：各 800 条；参考答案的 `is_feasible` 统计为 533 条 true、267 条 false，与 README 一致。任务包含自然语言、显式要求、酒店/景点/航班要求和 `impossible` 等字段；参考答案包含 `is_feasible`, `refusal_reason`, `plan`，计划中有活动起止时间。

仓库包含 `api/data/v2/` 知识库。作者声明其为合成且内部一致的数据，覆盖 375 城市、212530 条记录。坐标、价格、资源和时间应作为沙箱事实使用，不应当作现实旅行建议。

根据 README，评分器检查预算、实体依据、开放时间、时空可达性、用户需求和不可行处理；不依赖 LLM judge。`verify_gold.py` 可离线复核参考答案，作者声称其全部通过。本次只检查文件与标签，没有执行该脚本，因此未独立验证这一正确性声明。

```powershell
git clone https://github.com/TonyQJH/TREK-A-Travel-Reasoning-and-Evaluation-Kit-for-LLM-Agents-in-Complex-Trip-Planning.git
cd TREK-A-Travel-Reasoning-and-Evaluation-Kit-for-LLM-Agents-in-Complex-Trip-Planning
pip install -r requirements.txt
python verify_gold.py
```

本次只下载了任务与参考答案，未下载完整知识库或安装其依赖。上面的命令是官方说明的运行路径，不是已经完成的执行记录。官方 Agent runner 使用 Amazon Bedrock；复用数据和离线评分器不要求使用相同模型服务。

许可：代码 MIT，数据 CC BY 4.0。论文和仓库当前声明为审稿中，不能称为已接收论文。

适配成本：中等。对 CP-SAT 的可行/不可行区分很有用，但完整任务涵盖多城市和航班，当前单城市模型不能直接覆盖所有任务。美元预算和不同计价口径也不能直接当成人民币使用。

## 3. TravelPlanner：经典标准基线

- [代码与评测](https://github.com/OSU-NLP-Group/TravelPlanner)
- [查询数据](https://huggingface.co/datasets/osunlp/TravelPlanner)
- [JSONL 参考信息目录](https://github.com/OSU-NLP-Group/TravelPlanner/tree/main/database)
- [论文](https://arxiv.org/abs/2402.01622)

论文介绍 1225 个规划意图，主要针对美国旅行。覆盖预算、人数、住宿、餐饮、交通等约束。官方支持收集信息后规划的 two-stage 模式，以及提供参考信息的 sole-planning 模式。

```python
from datasets import load_dataset

data = load_dataset("osunlp/TravelPlanner", "validation")["validation"]
```

这是按 README 给出的 config/split 用法调整的验证集读取示例。本次未成功连接 Hugging Face 下载查询，因此没有独立统计其 split 行数。GitHub 目录已确认包含 `train_ref_info.jsonl`, `validation_ref_info.jsonl`, `test_ref_info.jsonl`；README 还提供独立数据库下载入口。参考信息文件不能直接等同于查询集或人工参考解。

官方提供 validation 离线评分；test 成绩应遵循官方提交与基准使用规则。代码仓库 MIT，数据的单独许可本次未成功读取数据卡核实，应在实际分发前确认。

适配成本：中等到较高。适合与既有旅行 Agent 研究对齐，较不适合直接支撑当前国内单城市 MVP。其预算单位、跨城交通与输出格式需要单独适配。

## 4. TravelBench：中文多轮交互与工具能力边界

- [本清单指的是 small-xiangcheng/TravelBench](https://github.com/small-xiangcheng/TravelBench)
- [任务目录](https://github.com/small-xiangcheng/TravelBench/tree/main/datas)
- [工具缓存](https://github.com/small-xiangcheng/TravelBench/blob/main/sandbox_cache.zip)
- [论文：Beyond Itinerary Planning](https://arxiv.org/abs/2512.22673)

实际下载并逐行解析了三个 JSONL：`single_turn.jsonl` 500 条、`multi_turn.jsonl` 500 条、`unsolve.jsonl` 100 条。工具缓存 ZIP 在仓库中，大小约 73 MB，本次没有下载。

数据字段包括 `query`, `context`, `intent`, `primary_intent`, `missing_tool`, `missing_info`, `no_actionable`；多轮任务额外包含 `user_profile`。实际请求包含口语化中文、位置、导航上下文和 POI 查询，范围比“生成多日旅行行程”更宽。

重要区分：`unsolve` 主要涉及工具缺失、信息缺失或不可执行请求，并非统一的数学约束不可满足。它适合测试“系统应澄清、拒绝还是调用工具”，不适合作为 CP-SAT 无解正确率的直接标签来源。

官方完整流程使用用户模拟器、工具模拟器、embedding 服务和模型评判，因此不是下载 JSONL 就能完成零成本确定性评测。对本项目宜作为交互阶段扩展，而非第一批 CP-SAT 用例。

许可：代码 MIT；数据 CC BY-NC 4.0。另有名称相同的旅行领域 NLP 数据集，查找时应确认仓库和论文标题。

## 5. AcCoRD-Travel：偏好协商与变化

- [官方代码与场景](https://github.com/tejas1995/accord_benchmark)
- [实际场景文件](https://github.com/tejas1995/accord_benchmark/blob/main/scenario_data/travelgym/v3.3.1_test_100scenarios.json)
- [论文](https://arxiv.org/abs/2608.27818)

实际下载并解析该文件：JSON 数组，100 个旅行场景，字段包括 `initial_desc`, `preferences`, `all_options`, `arguments`, `dimensions`。示例涉及航班、租车及用户逐步表达的偏好。适合测试未说明偏好、可协商偏好、不可实现偏好与交互中触发的新偏好。

当前 README 示例提到另一个文件名，实际 Git tree 中存在的是上面的 `v3.3.1_test_100scenarios.json`，应以实际文件和版本为准。

仓库显示 MIT 许可，本次未发现单独明确的数据许可说明，分发前应进一步确认。完整评测需要用户模拟器和相应环境；它不直接提供景点时间窗排程基准，接入优先级低于 ChinaTravel 和 TREK。

## 6. 查到但暂不作为可用数据推荐

| 候选 | 当前核查结果 |
|---|---|
| [UTP-Bench](https://github.com/ETCHARLAREVANTHRAO/UTP-Bench) | 论文描述 1000 个查询与参考行程，但官方 README 的数据下载仍为 Coming soon，不能当作已获得数据 |
| [TripCraft](https://aclanthology.org/2025.acl-long.834/) | 找到论文；本次查看的 arXiv 正文仅承诺开放，没有核实到完整、明确可下载的官方数据包，因此不列入第一批依赖 |
| [TripTide](https://arxiv.org/abs/2510.21329) | 论文提供匿名代码入口，适合后续关注扰动修复；本次未核实完整数据与许可，不作为已可下载资源 |

## 7. 对 Phase B 的具体选择

建议使用 ChinaTravel easy/medium 的开发任务与配套中文沙箱建立国内基线，再用 TREK 的可行/不可行案例设计求解器诊断测试。不要为了得到好看的 10 条结果，只挑原系统已经能通过的任务。

最小评测应明确两个层次：

1. **原基准评测**：保留原始任务、事实、约束和官方评测规则，报告系统实际支持的范围与全量分母。
2. **项目派生评测**：为单城市模型筛选或改写任务，显式记录删改内容、原始 ID、派生 ID、数据版本和适用约束；重新建立可行性标签，单独报告结果。

未知价格、估算超限与已知费用超限未必在公开基准中形成完整三态覆盖。应另外构造受控缺失和估算费用用例，保留数据来源并标注为派生测试，不能冒称原基准自带案例。

`hard_logic_py`、`impossible`、gold 和隐藏偏好等标注应留给评测器或模拟用户。比较 LLM 排程与 LLM 抽取 + CP-SAT 时，两组应看到相同自然语言与可用工具数据；不能只给求解器组参考约束或正确答案，再声称算法公平胜出。若要比较 oracle 约束条件，应单独设置并清楚命名该实验臂。

## 8. 本次核查的仓库快照

GitHub Git tree API 返回以下快照 SHA，均未被截断：

- ChinaTravel：`f445e0011f42594fcc29b8c752ece06ded9a8218`
- TREK：`6ceb4ebb2debd69c5c7c4ba34b5b17524756912b`
- TravelPlanner：`e52c87f4ac348a3410c46dc3553c519db5ec5e23`
- TravelBench：`445a29d9a9b6457fc95fe647c532b6b79e21c43f`
- AcCoRD：`01fa00f79a83cfcef105d81d96fc11bb2d9c65d7`

实际下载的数据用于字段、数量和标签核查，尚未接入项目评测，也没有把外部仓库批量放入本项目。
