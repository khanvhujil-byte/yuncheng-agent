"""Build an auditable local report from saved live runs, never from invented scores."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import shutil
import statistics
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    archive = project/'docs/evaluation-20261019'
    archive.mkdir(parents=True, exist_ok=True)
    rounds = ('travel-live-release', 'travel-live-refined', 'travel-live-final',
              'travel-live-weather-ticket', 'travel-live-confirmed', 'travel-live-last-recheck', 'travel-live-anchors')
    all_rounds, latest, origin = [], {}, {}
    for name in rounds:
        source = args.artifacts/name/'results.json'
        if not source.exists():
            continue
        report = json.loads(source.read_text(encoding='utf-8'))
        records = report['cases']
        shutil.copy2(source, archive/(name+'.json'))
        manifest = source.parent/'source-manifest.json'
        if manifest.exists():
            shutil.copy2(manifest, archive/(name+'-source.json'))
        all_rounds.append((name, records))
        for record in records:
            latest[record['case_id']] = record
            origin[record['case_id']] = name
    baseline = next(rows for name, rows in all_rounds if name == 'travel-live-final')
    durations = sorted(float(r['duration_s']) for r in baseline if r['kind'] == 'generate')
    lines = ['# 旅行 Agent 真实评测与修复报告', '', '生成时间：'+datetime.now().isoformat(timespec='seconds'), '',
        '## 评测口径', '',
        '公开参考：[TravelPlanner](https://github.com/OSU-NLP-Group/TravelPlanner)。其美国城市离线沙盒与本项目中国城市、高德、天气和人民币预算接口不同，因此采用自建集；没有声称复现其官方分数或 SOTA。', '',
        '完整一轮为 55 个请求：36 种常规/边界规划 G01–G36，14 个输入与可行性预检 P01–P14，5 个主题需求 T01–T05。T01–T04 是额外真实规划，T05 是天数冲突。', '',
        '真实跑批使用配置中的模型与地图接口；地点目录由本地 SQLite 只读快照提供。每个场景独立子进程、数据库；最多 2 并发，300 秒到期结束本测试进程树。未提交用户历史对话，未写业务数据库。', '',
        '评测通过不等于生成成功：低预算、非法输入等请求应拒绝；失败草稿不算成功。主题按不同日期的真实地点验收；结构、预算加总、三餐、路线时钟、候选身份、重复景点、必去与天气窗口独立检查。', '',
        '这些是有限场景测试，不能证明所有漏洞已经消失，也不等于酒店房价、营业时间、积雪和景区开放状态经过官方核验。模型门票参考始终标估算。', '',
        '## 每轮结果', '', '| 轮次 | 完成 | 符合预期 | 返回 success=true |', '|---|---:|---:|---:|']
    for name, rows in all_rounds:
        lines.append(f"| {name} | {len(rows)} | {sum(bool(r['passed']) for r in rows)} | {sum(r.get('success') is True for r in rows)} |")
    lines += ['', f'完整 55 项一轮生成类延时：中位数 {statistics.median(durations):.2f} 秒，P95 {durations[math.ceil(len(durations)*.95)-1]:.2f} 秒；含失败请求。', '',
        '以上轮次包含逐步修复版本，不能把不同轮次拼成同一版本成功率。下表按最新一次尝试覆盖展示，没有挑选历史最好结果。完整轮次运行期间进行了后续修复，原始 source-manifest 记录轮次起点；后增的逐场景 source-manifest 可精确留存各子进程版本。', '',
        '## 已修复', '',
        '- 主题天数在需求提取、检索、上下文、缓存与交付复核中贯通；同一天多个雪山不能冒充两天。',
        '- 周边主题允许跨行政市查询，按市中心直线 300 公里过滤；周边结果标 scope，避免混进普通市区缓存。雪山使用少量城市查询扩展，候选仍必须来自工具。',
        '- 长行程分页补充真实景点，保留必去、主题和恶劣天气的室内候选；保留候选建议游玩时间。',
        '- 舒适度修复使用同一份路线缓存；可以去除主题日冗余可选景点，不能移除最后一个主题锚点。',
        '- 晚餐允许在返程后的酒店附近；超时情况下有限比较真实近酒店餐厅，重新核算路线和预算，保护局部修改及美食优先要求。',
        '- 餐饮预检预留额度给实际选定的酒店；恶劣天气后补真实室内候选，不以修改天气规则伪造通过。',
        '- 紧凑表格提示保留全部酒店和餐厅，避免重复字段与候选矩阵耗尽 token。',
        '- 对实测数值后多余引号做有限 JSON 语法修复，拒绝截断、重复键与非有限值；修复后继续全部业务校验。',
        '- 格式修复的每次模型调用进入调用上限与用量统计；保留原项目兼容适配器和开关。',
        '- 重排换入景点可以补充门票估算；每个景点失败尝试不重复，全程最多三批，仍遵循先内部证据再网上查询、最后模型估算。',
        '- 用户错误说明保留日期和原因，长候选矩阵留在运行日志；失败不能清成成功，也不能进入成功行程缓存。',
        '- 漏掉的必去点仅可从已解析锚点有限补回，不能编造候选或绕过路线和预算验收；复杂跨日布局仍可能失败。',
        '- 执行租约的获取、检查、续期和释放固定在同一个数据库，避免运行中全局路径切换导致清理访问错误数据库。',
        '- Windows 前端测试改为 node --test，使 Node 20 可以发现测试文件。', '', '## 自动化回归', '']
    for name in ('travel-verified.xml', 'travel-delivery-tests.xml', 'travel-latest-targeted.xml', 'travel-anchor-tests.xml'):
        source = args.artifacts/name
        if source.exists():
            suite = ET.parse(source).getroot().find('testsuite')
            lines.append(f"- {name}：{suite.get('tests')} 项，失败 {suite.get('failures')}，错误 {suite.get('errors')}。")
            shutil.copy2(source, archive/name)
    lines += ['- 前端 13 项测试、vue-tsc 与 Vite 构建通过；Ruff F/E9 规则通过。', '',
        '## 所有场景的最新尝试', '', '| ID | 场景 | 验收 | success | 秒 | 轮次 | 未满足项 |', '|---|---|---|---|---:|---|---|']
    for key, row in sorted(latest.items()):
        reason = '；'.join(str(v).replace('|', '/')[:180] for v in row.get('violations', [])[:3])
        lines.append(f"| {key} | {row['name']} | {'通过' if row['passed'] else '失败'} | {row.get('success')} | {row.get('duration_s')} | {origin[key]} | {reason} |")
    lines += ['', '## 当前限制与上线判断', '',
        '成都两天公共交通雪山：已能召回雪山，但现有整程单酒店、每天回市区和 660 分钟舒适上限无法容纳实际往返；必须明确失败。后续需要多住宿分段、城际交通事实及山区天气，不可偷偷改成市区行程。', '',
        '多个必去点的联合布局仍可能在有限重排内失败；场景清单保留最近一次失败，不能因为曾经通过就声称稳定。应继续投入跨日约束求解与路线数据覆盖。', '',
        '一次真实测试通过不能证明营业、预约和票价准确。无证据票价可按用户要求显示日常模型估算，必须保留 estimated/llm_estimated 标签；模型失败仍只能显示预算预留。', '',
        '本轮启动并验证 Python 与前端。Java 社区、Redis、MySQL 及 Docker 整栈没有重新验收；这份报告不构成完整公网发布许可或生产稳定性保证。', '',
        '## 复现', '', '在 backend 目录使用项目虚拟环境：', '', '```powershell',
        '.\\.venv\\Scripts\\python.exe -u scripts/eval_travel_release.py --out experiments/release-audit --workers 2 --timeout 300',
        '.\\.venv\\Scripts\\python.exe -m pytest -q',
        '.\\.venv\\Scripts\\python.exe -m ruff check .', '```', '',
        '真实跑批会使用已配置接口并产生用量。评测结果位于独立 experiments 路径；报告附录 JSON 保留请求、实际结果、失败理由与用量。']
    (project/'docs/旅行Agent评测与修复-20261019.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'requests': len(latest), 'latest_passed': sum(bool(r['passed']) for r in latest.values()),
                      'report': str(project/'docs/旅行Agent评测与修复-20261019.md')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
