"""Extract venue-scoped admission evidence, never unrelated prices or snippets."""
from datetime import date, datetime, timedelta, timezone
import re


def aliases(name, city=''):
    """Map hierarchical map labels to the named child, not its parent venue."""
    normalized = re.sub(r'\s+', '', name).replace('（', '(').replace('）', ')')
    values = [normalized]
    leaf = re.split(r'[-—–>]', normalized)[-1]
    if len(leaf) >= 3 and not any(x in leaf for x in ('停车场', '游客中心', '售票处', '入口', '出口')):
        values.append(leaf)
    city = city.removesuffix('市')
    if city and normalized.startswith(city):
        shortened = normalized[len(city):]
        if len(shortened) >= 4:
            values.append(shortened)
    for value in list(values):
        if value.endswith('景区') and len(value[:-2]) >= 3:
            values.append(value[:-2])
    return list(dict.fromkeys(values))


def _text(value):
    return re.sub(r'[ \t\r\f\v]+', ' ', value).replace('（', '(').replace('）', ')').strip()


def _season_applies(fragment, target):
    if not any(w in fragment for w in ('旺季', '淡季')):
        return True
    span = re.search(r'(\d{1,2})月(\d{1,2})日?\s*(?:至|到|[-—~～])\s*(\d{1,2})月(\d{1,2})日?', fragment)
    if not span:
        raise ValueError('DATED_OR_SEASONAL_POLICY_UNRESOLVED')
    a, b, c, d = map(int, span.groups())
    date(2000, a, b), date(2000, c, d)
    current, start, end = (target.month, target.day), (a, b), (c, d)
    return start <= current <= end if start <= end else current >= start or current <= end


def extract(page, name, city, target):
    today = datetime.now(timezone(timedelta(hours=8))).date()
    target = date.fromisoformat(target)
    if not today <= target <= today + timedelta(days=90):
        raise ValueError('OUTSIDE_POLICY_WINDOW')
    text = _text(''.join(page.parts))
    if len(text) < 200 and re.search(r'(?:id=["\'](?:app|root)["\']|__NEXT_DATA__)', page.raw):
        raise ValueError('SOURCE_REQUIRES_RENDERING')
    if len(text) < 1000 and re.search('人机验证|安全验证|请输入验证码|访问过于频繁', text):
        raise ValueError('SOURCE_ACCESS_CHALLENGE')
    names = aliases(name, city)
    contains_name = lambda value: any(n in re.sub(r'\s+', '', value) for n in names)
    if not names or not contains_name(text) or city.removesuffix('市') not in text:
        raise ValueError('VENUE_OR_CITY_MISMATCH')
    published = re.search(r'(?:发布时间|发布日期|更新时间|更新日期)[：:\s]*(20\d{2})[-年/]', text)
    # Historical notices require explicit updated policy rather than pretending
    # that a successful fetch makes their tariff current. Footer years aren't dates.
    if published and int(published.group(1)) < today.year:
        raise ValueError('HISTORICAL_POLICY')
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    scoped = []
    for i, paragraph in enumerate(paragraphs):
        if contains_name(paragraph):
            scoped.append(paragraph)
            if len(paragraph) < 100:
                for following in paragraphs[i+1:i+13]:
                    # A directory may place another venue immediately before a
                    # tariff line. Its price must never leak into this venue.
                    if (not contains_name(following) and len(following) < 80
                            and re.search(r'(?:景区|博物馆|公园|景点|古镇|寺|祠|阁|街区|书院)(?:\s*\d+)?$', following)):
                        break
                    scoped.append(following)
    quotes = []
    for paragraph in dict.fromkeys(scoped):
        if len(paragraph) > 1600:
            continue
        # Commas separate adult and child rates in ordinary prose; HTML table
        # rows stay intact, so a child-only row cannot inherit the adult label.
        for fragment in re.split(r'[。；;，,\n]', paragraph):
            fragment = fragment.strip(' |\t')
            if not fragment:
                continue
            if not (contains_name(fragment) or re.match(
                    r'^(?:\d+[、.)]\s*)?(?:成人|全价|普通票|门票|票价|入园|入馆|入场|收费标准|免费|本馆|本园|本景区|旺季|淡季)', fragment)):
                continue
            if any(w in fragment for w in ('儿童', '老人', '学生', '优惠', '起售', '套票', '联票', '年卡', '夜场', '起/', '残疾', '军人', '市民', '限时', '活动期间', '仅限')):
                continue
            if re.search(r'20\d{2}年', fragment) and str(today.year) not in fragment:
                raise ValueError('HISTORICAL_POLICY')
            if any(w in fragment for w in ('有效期', '截至', '试行期', '节假日价格')):
                raise ValueError('DATED_OR_SEASONAL_POLICY_UNRESOLVED')
            if not _season_applies(fragment, target):
                continue
            for match in re.finditer(
                    r'(?:成人(?:门)?票|成人大门票|成人入园票|成人全价票|全价(?:门)?票|普通门票|门票(?:价格)?|票价|入园票|入馆票)'
                    r'[^\d。；]{0,16}(\d{1,4}(?:\.\d{1,2})?)\s*元(?!\s*(?:起|[-~～至到]))', fragment):
                amount = float(match.group(1))
                if amount.is_integer() and 0 <= amount <= 10000:
                    quotes.append((int(amount), fragment))
            if (re.search(r'(?:免门票|免费(?:开放|参观|入园|入馆)|(?:门票(?:价格)?|票价)[：:为\s|]*免费)', fragment)
                    and not re.search(r'(?:不|未|非|取消).{0,4}(?:免费|免门票)', fragment)):
                quotes.append((0, fragment))
    if not quotes or len({a for a, _ in quotes}) != 1:
        raise ValueError('ADULT_ADMISSION_MISSING_OR_AMBIGUOUS')
    return {'amount': quotes[0][0], 'quote': quotes[0][1][:600]}
