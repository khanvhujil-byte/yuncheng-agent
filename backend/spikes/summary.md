# CP-SAT 排程 spike 运行摘要

本文件由 `cpsat_scheduling.py` 自动生成，内容全部是当次运行的原始输出。

- 命令行参数：`--timeout 5.0 --reps 3`
- ortools 版本：9.15.6755
- Python：3.13.2
- 结论只在给定候选池、给定天/日时间窗、haversine 估算通勤（18.0 km/h 直线折算）与估算/未知票价口径下成立；换候选池、换时间窗、换通勤估算或换票价数据，结论都可能改变。

## 臂 固定排期基线 的判定分布

| 判定 | 任务数 | 任务 |
|---|---|---|
| FEASIBLE | 8 | T01_relaxed_feasible, T03_unknown_cost, T06_cross_day_split, T07_two_must_one_slot, T08_budget_tight, T09_budget_unverified, T10_unknown_bound_trips, T11_known_price_over_budget |
| INFEASIBLE | 3 | T02_capacity_style_infeasible, T04_tight_window_feasible, T05_five_minutes_short |
| UNKNOWN | 0 | - |

## 臂 CP-SAT 的判定分布

| 判定 | 任务数 | 任务 |
|---|---|---|
| FEASIBLE | 8 | T01_relaxed_feasible, T03_unknown_cost, T04_tight_window_feasible, T06_cross_day_split, T07_two_must_one_slot, T08_budget_tight, T09_budget_unverified, T10_unknown_bound_trips |
| INFEASIBLE | 3 | T02_capacity_style_infeasible, T05_five_minutes_short, T11_known_price_over_budget |
| UNKNOWN | 0 | - |

## 逐任务明细（取第 1 次重复）

| 任务 | 基线判定 | 基线打分 | CP-SAT 判定 | 求解状态 | 打分 | 求解耗时(s) | 预算状态 | 与 expect 是否一致 |
|---|---|---|---|---|---|---|---|---|
| T01_relaxed_feasible | FEASIBLE | PASS | FEASIBLE | OPTIMAL | PASS | 0.154 | unverified | 一致 |
| T02_capacity_style_infeasible | INFEASIBLE | FAIL | INFEASIBLE | INFEASIBLE | FAIL | 0.157 | - | 一致 |
| T03_unknown_cost | FEASIBLE | PASS | FEASIBLE | OPTIMAL | PASS | 0.132 | unverified | 一致 |
| T04_tight_window_feasible | INFEASIBLE | FAIL | FEASIBLE | OPTIMAL | PASS | 0.014 | within | 一致 |
| T05_five_minutes_short | INFEASIBLE | FAIL | INFEASIBLE | INFEASIBLE | FAIL | 0.121 | - | 一致 |
| T06_cross_day_split | FEASIBLE | PASS | FEASIBLE | OPTIMAL | PASS | 0.075 | within | 一致 |
| T07_two_must_one_slot | FEASIBLE | PASS | FEASIBLE | OPTIMAL | PASS | 0.007 | within | 一致 |
| T08_budget_tight | FEASIBLE | PASS | FEASIBLE | OPTIMAL | PASS | 0.194 | estimated_exceeded | 一致 |
| T09_budget_unverified | FEASIBLE | PASS | FEASIBLE | OPTIMAL | PASS | 0.124 | unverified | 一致 |
| T10_unknown_bound_trips | FEASIBLE | PASS | FEASIBLE | OPTIMAL | PASS | 0.030 | unverified | 一致 |
| T11_known_price_over_budget | FEASIBLE | FAIL | INFEASIBLE | INFEASIBLE | FAIL | 0.053 | - | 一致 |

## 不可行诊断（充分子集，不是最小冲突集）

- **T02_capacity_style_infeasible**：每天 POI 数量上限(第 1 天) <= 2; 灵隐寺（飞来峰景区） 的开放时间窗 07:30-17:30; 河坊街（清河坊历史街区） 的开放时间窗 10:00-22:00; 中国丝绸博物馆 的开放时间窗 09:00-17:00; 第 1 天结束不得晚于 18:00; 已知费用不超过预算上限 800
  - 精简后：每天 POI 数量上限(第 1 天) <= 2
  - 可核查性：只保留该组约束 -> INFEASIBLE（充分性已实测证实）
  - 领域下界证明：必去项 3 个 > 天数 1 × 每天上限 2 = 2，纯计数即可证明无解，无需进入搜索。
- **T05_five_minutes_short**：每天 POI 数量上限(第 1 天) <= 3; 雷峰塔 的开放时间窗 08:00-20:00; 净慈寺 的开放时间窗 11:05-11:55; 苏东坡纪念馆 的开放时间窗 08:30-17:00; 钱王祠 的开放时间窗 08:30-17:00; 第 1 天结束不得晚于 16:00; 已知费用不超过预算上限 300
  - 精简后：净慈寺 的开放时间窗 11:05-11:55
  - 可核查性：只保留该组约束 -> INFEASIBLE（充分性已实测证实）
- **T11_known_price_over_budget**：每天 POI 数量上限(第 1 天) <= 2; 每天 POI 数量上限(第 2 天) <= 2; 灵隐寺（飞来峰景区） 的开放时间窗 07:30-17:30; 雷峰塔 的开放时间窗 08:00-20:00; 钱王祠 的开放时间窗 08:30-17:00; 第 1 天结束不得晚于 18:00; 第 2 天结束不得晚于 18:00; 已知费用不超过预算上限 100
  - 精简后：已知费用不超过预算上限 100
  - 可核查性：只保留该组约束 -> INFEASIBLE（充分性已实测证实）

> 口径说明：上面列出的是**充分子集** —— 让这些约束同时生效会导致无解，
> 组内可能有冗余（`精简后` 一栏是逐条剔除得到的较小集合，仍不保证是全局最小）。
> **不声称是最小冲突集/不可约不可行子集（IIS）**。

## 预算口径

| 任务 | known | estimated | 未知项上界估计 | 上限 | 预算状态 |
|---|---|---|---|---|---|
| T01_relaxed_feasible | 75.0 | 20.0 | 115.0 | 1000 | unverified |
| T02_capacity_style_infeasible | - | - | - | 800 | 无行程可判（INFEASIBLE） |
| T03_unknown_cost | 0.0 | 20.0 | 40.0 | 60 | unverified |
| T04_tight_window_feasible | 40.0 | 10.0 | 50.0 | 300 | within |
| T05_five_minutes_short | - | - | - | 300 | 无行程可判（INFEASIBLE） |
| T06_cross_day_split | 0.0 | 140.0 | 140.0 | 400 | within |
| T07_two_must_one_slot | 0.0 | 0 | 0.0 | 200 | within |
| T08_budget_tight | 115.0 | 110.0 | 225.0 | 140 | estimated_exceeded |
| T09_budget_unverified | 0.0 | 0 | 0.0 | 100 | unverified |
| T10_unknown_bound_trips | 20.0 | 0 | 40.0 | 80 | unverified |
| T11_known_price_over_budget | - | - | - | 100 | 无行程可判（INFEASIBLE） |

## 不能下的结论

- 本 spike 不调用任何模型，约束是结构化输入，因此**约束抽取正确性不在测试范围内**。
- 与固定排期基线的差异只能说明：在这 10 个任务、这套估算数据下的差异；样本很小，不能外推。
- `estimated_exceeded` 不能读成「实际不可行」；`unknown` 也不等于「超支」，只等于「无法核实」。
- 通勤是 haversine 直线估算，不是真实路网时间。
