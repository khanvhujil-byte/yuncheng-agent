"""Separate ordinary shopping access, consumption, and admission tickets.

Category rules describe the scope of an activity; they are never an official
ticket quote. A sourced positive price wins over a general category rule.
"""

PAID_SUBVENUES = ('展览', '展馆', '体验馆', '游乐', '乐园', '剧场', '演出', '水族', '海洋馆',
                  '滑雪', '滑冰', '剧院', '电影院', '影城', '观光', '摩天轮')


def apply_admission_policy(spot):
    result = dict(spot)
    if (result.get('ticket_price') is not None and result.get('ticket_price_status') != 'unknown'
            and result.get('ticket_price_source') in {'imported', 'amap'}):
        if result['ticket_price'] > 0:
            result['admission_policy'] = 'unknown'
        return result
    if result.get('admission_policy') not in (None, 'unknown'):
        result['admission_policy'] = 'unknown'
    codes = [code.strip() for code in (result.get('typecode') or '').split('|') if code.strip()]
    category = result.get('category') or ''
    name = result.get('name') or ''
    # Mixed scenic/culture/leisure codes are intentionally not inferred free.
    shopping = bool(codes) and all(code.startswith('06') for code in codes)
    if not codes:
        shopping = category in {'购物中心', '普通商场', '商场', '步行街', '特色商业街'}
    if shopping and not any(word in name for word in PAID_SUBVENUES):
        result.update(admission_policy='ordinary_access', ticket_price=0,
                      ticket_price_status='estimated', ticket_price_source='venue_policy',
                      ticket_price_evidence_url=None,
                      ticket_price_note='按购物类目规划普通入场，不设门票预算；购物、餐饮和付费展览等消费另计。这是类目规则，非官网免费票价核验。')
    elif any(code in {'080304', '080302', '080303'} for code in codes) or category in {'酒吧', '夜总会', 'KTV'}:
        result.update(admission_policy='consumption', ticket_price=None,
                      ticket_price_status='unknown', ticket_price_source='unknown', ticket_price_evidence_url=None,
                      ticket_price_note='这是消费型场所；酒水、最低消费及可能的入场收费尚未核实，不能套用景点统一门票预留。')
    elif result.get('ticket_price_source') == 'venue_policy':
        # An old/changed category must not carry an obsolete zero-price rule.
        result.update(admission_policy='unknown', ticket_price=None,
                      ticket_price_status='unknown', ticket_price_source='unknown',
                      ticket_price_evidence_url=None, ticket_price_note='当前类目不足以确认普通入场无需门票。')
    return result
