"""Explicit thematic day requirements: preserve counts, retrieve and audit facts.

The deterministic text adapter covers common day-count phrasing. Conversation
extraction also exposes the generic schema. Descriptions written by an LLM are
never evidence that a POI belongs to a theme or that an actual snowfall exists.
"""
import re

from ..models.requirements import DayGoal

THEMES = {
    '雪山': ('雪山', '四姑娘山', '达古冰川', '贡嘎', '冰川公园', '云杉坪', '牦牛坪', '蓝月谷'),
    '博物馆': ('博物馆', '科技馆', '美术馆'),
    '海边': ('海滩', '沙滩', '海滨', '海岸', '海水浴场', '海湾'),
    '古镇': ('古镇', '古城'),
    '主题乐园': ('主题乐园', '游乐园', '欢乐谷', '迪士尼', '方特'),
}
NUMBERS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
SERVICE_ONLY = ('公交站', '停车场', '停车点', '售票处', '售票厅', '游客中心', '客运站', '入口', '检票口', '出入口', '服务中心')


def _number(word):
    if word.isdigit():
        return int(word)
    if word in NUMBERS:
        return NUMBERS[word]
    if '十' in word:
        a, b = word.split('十', 1)
        return NUMBERS.get(a, 1) * 10 + NUMBERS.get(b, 0)
    return 0


def text_goals(text):
    """Only explicit counts and themes in the same clause; never infer a count."""
    found = {}
    for clause in re.split(r'[，,。；;！!？?\n]', text or ''):
        if re.search(r'不想|不要|不去|不用|取消|不安排|不需要|放弃', clause):
            continue
        for theme in THEMES:
            if theme not in clause:
                continue
            # "两天想去周边雪山" and "雪山玩两天". No matching a day
            # ordinal (第二天), weather forecast, or an overall trip duration.
            before = re.search(r'(?<![第0-9一二两三四五六七八九十])([0-9一二两三四五六七八九十]{1,3})天(?:想|要|准备|计划|安排|专门|留|都|可以|去|在|到|游|玩|看|逛|附近|周边|的|有|能|给|用于|用来|走|上|下|\s){0,18}' + re.escape(theme), clause)
            after = re.search(re.escape(theme) + r'(?:游玩|安排|玩|逛|游览|待|去|用|留|至少|共|总共|\s){0,8}([0-9一二两三四五六七八九十]{1,3})天', clause)
            match = before or after
            if not match:
                continue
            days = _number(match.group(1))
            if 1 <= days <= 30:
                found[theme] = DayGoal(theme=theme, days=days,
                                       scope='surrounding' if re.search(r'周边|附近|近郊|郊外|周围', clause) else 'city')
    return list(found.values())


def effective_goals(request):
    goals = {g.theme: g for g in request.requirements.attractions.day_goals}
    for goal in text_goals(request.free_text_input):
        goals.setdefault(goal.theme, goal)
    for goal in text_goals(request.requirements.attractions.instructions):
        goals.setdefault(goal.theme, goal)
    return list(goals.values())


def matches(spot, goal):
    raw = spot.model_dump() if hasattr(spot, 'model_dump') else spot
    name = raw.get('name') or ''
    category = raw.get('category') or ''
    if any(word in name for word in SERVICE_ONLY):
        return False
    allowed = ('11', '19') if goal.theme == '雪山' else ('11', '14', '19')
    if raw.get('typecode') and not any(code.startswith(allowed) for code in raw['typecode'].split('|')):
        return False
    # A named parent scenic area in provider address is usable geographic
    # evidence, unlike a generated description. A city district alone is not.
    text = name + ' ' + category + (' ' + (raw.get('address') or '') if goal.theme == '雪山' else '')
    return any(word in text for word in THEMES.get(goal.theme, (goal.theme,)))


def search_keyword(goal):
    return {'海边': '海滨', '主题乐园': '游乐园'}.get(goal.theme, goal.theme)


def search_queries(goals, city):
    """Small editorial query expansion, never a source of candidate facts."""
    mountain_names = {'成都': ('西岭雪山', '四姑娘山', '达古冰川'),
                      '丽江': ('玉龙雪山', '哈巴雪山'),
                      '迪庆': ('梅里雪山', '石卡雪山')}
    queries = []
    for goal in goals:
        words = [search_keyword(goal)]
        if goal.theme == '雪山' and goal.scope == 'surrounding':
            words.extend(mountain_names.get(city.removesuffix('市'), ()))
        queries.extend((word, goal.scope == 'surrounding') for word in words)
    return list(dict.fromkeys(queries))[:12]


def audit_days(plan, request):
    errors = []
    days = plan.get('days') or []
    for goal in effective_goals(request):
        matched = set()
        for day in days:
            spots = day.get('attractions') or []
            relevant = [s for s in spots if matches(s, goal)]
            if not relevant:
                continue
            minutes = sum(max(30, int(s.get('visit_duration') or 120)) for s in relevant)
            total = sum(max(30, int(s.get('visit_duration') or 120)) for s in spots)
            if minutes * 2 >= total:
                matched.add(day.get('day_index'))
        missing_indices = sorted(set(goal.day_indices) - matched)
        if len(matched) < goal.days or missing_indices:
            suffix = ('；未满足指定第' + '、'.join(str(i + 1) for i in missing_indices) + '天') if missing_indices else ''
            errors.append(f'用户要求「{goal.theme}」至少安排{goal.days}天，实际仅{len(matched)}天以真实相关景点为主要活动{suffix}；不能用一句建议或同一天多个景点代替。')
    return errors


def prompt_goals(request, candidates):
    return [{**goal.model_dump(), 'eligible_poi_ids': [a.poi_id or a.name for a in candidates if matches(a, goal)],
             'rule': '按不同day_index计数，相关景点游玩时长至少占该日景点游玩时长一半；必须保留真实路线、天气及预算校验。'}
            for goal in effective_goals(request)]
