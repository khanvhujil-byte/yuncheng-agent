"""One requirement contract; code audits measurable rules independently of prompts."""
import copy
import json

from ..models.requirements import Requirements


def deep_merge(original, patch):
    result = copy.deepcopy(original)
    for key, value in patch.items():
        result[key] = deep_merge(result.get(key) or {}, value) if isinstance(value, dict) else copy.deepcopy(value)
    return result


def policy(request):
    return getattr(request, "requirements", None) or Requirements()


def context(request, *modules):
    needs = policy(request)
    selected = {key: getattr(needs, key).model_dump() for key in modules}
    return ("\n【用户分环节要求】\n" + json.dumps(selected, ensure_ascii=False)
            + "\n文字instructions是偏好，不能宣称已核实设施、过敏安全、营业或预约事实。"
              "必须服从明确的排除项、数值限制和已指定地点；冲突不得静默放宽。"
            + "\n用户原始补充：" + (request.free_text_input or "（无）"))


def excluded(name, needs):
    return any(item.strip() and item.strip().casefold() in name.casefold() for item in needs.excluded_names)


def restaurant_allowed(restaurant, needs):
    return not excluded(restaurant.name, needs) and (
        needs.max_cost_per_meal is None or
        (restaurant.avg_cost is not None and restaurant.avg_cost_status != "unknown"
         and restaurant.avg_cost <= needs.max_cost_per_meal))


def hotel_allowed(hotel, needs):
    return not excluded(hotel.name, needs) and (
        needs.max_price_per_night is None or
        (hotel.cost_status != "unknown" and (hotel.estimated_cost > 0 or hotel.cost_status == "known")
         and hotel.estimated_cost <= needs.max_price_per_night))


def audit_requirements(plan, request):
    """Cache and local edits use this same independent audit."""
    needs = policy(request)
    errors = []
    for module in type(needs).model_fields:
        if getattr(needs, module).unverified:
            errors.append("尚未核实的硬性要求：" + "、".join(getattr(needs, module).unverified))
    for day in plan.get("days", []):
        label = day.get("date", "本日")
        if len(day.get("attractions", [])) > needs.attractions.max_per_day:
            errors.append(f"{label} 超过用户要求的每天最多{needs.attractions.max_per_day}个景点。")
        for spot in day.get("attractions", []):
            if excluded(spot["name"], needs.attractions):
                errors.append(f"{label} 包含用户排除的景点：{spot['name']}。")
        for meal in day.get("meals", []):
            if excluded(meal["name"], needs.food):
                errors.append(f"{label} 包含用户排除的餐厅：{meal['name']}。")
            cap = needs.food.max_cost_per_meal
            if cap is not None and (meal.get("estimated_cost") is None or meal.get("cost_status") == "unknown"
                                    or meal.get("cost_source") in (None, "static_estimate", "unknown")
                                    or meal["estimated_cost"] > cap):
                errors.append(f"{label} 的{meal['name']}无法满足每餐参考价不超过{cap}元的要求。")
        if needs.pace.max_day_minutes < 660:
            if not day.get("schedule"):
                errors.append(f"{label} 缺少时间表，无法核验用户指定的每日时长。")
            elif sum(s.get("duration_minutes", 0) for s in day["schedule"]) > needs.pace.max_day_minutes:
                errors.append(f"{label} 超过用户指定的每日{needs.pace.max_day_minutes}分钟上限。")
        if needs.pace.max_activity_minutes is not None:
            schedule = day.get("schedule") or []
            if not schedule or sum(s.get("duration_minutes", 0) for s in schedule
                                   if s.get("kind") in ("attraction", "travel")) > needs.pace.max_activity_minutes:
                errors.append(f"{label} 未满足用户要求的游玩加通勤最多{needs.pace.max_activity_minutes}分钟。")
        if needs.transport.max_leg_minutes is not None or not needs.transport.allow_estimated_routes:
            legs = day.get("route_legs") or []
            located = [x for x in [*day.get("attractions", []), *day.get("meals", [])] if x.get("location")]
            if len(located) > 1 and not legs:
                errors.append(f"{label} 缺少路线，无法核验用户的交通限制。")
            for leg in legs:
                if needs.transport.max_leg_minutes is not None and (leg.get("status") != "known" or
                        leg.get("duration_minutes") is None or leg["duration_minutes"] > needs.transport.max_leg_minutes):
                    errors.append(f"{label} 的{leg.get('from_name')}至{leg.get('to_name')}无法满足单段{needs.transport.max_leg_minutes}分钟限制。")
                elif not needs.transport.allow_estimated_routes and leg.get("status") != "known":
                    errors.append(f"{label} 有未核实路线，用户要求使用真实路线时间。")
        if needs.weather.avoid_rain_outdoors:
            weather = next((w for w in plan.get("weather_info", []) if w.get("date") == day.get("date")), None)
            outdoors = [a for a in day.get("attractions", []) if not any(
                word in (a.get("category", "") or "") + a["name"] for word in ("博物馆", "美术馆", "展览", "科技馆", "室内"))]
            if outdoors:
                if not weather or weather.get("beyond_window") or not weather.get("day_weather"):
                    errors.append(f"{label} 天气未知，无法确认户外安排符合避雨要求。")
                elif "雨" in weather["day_weather"]:
                    errors.append(f"{label} 预报有雨，但仍安排户外景点，不符合用户避雨要求。")
    if plan.get("hotel"):
        from ..models.schemas import Hotel
        if not hotel_allowed(Hotel.model_validate(plan["hotel"]), needs.hotel):
            errors.append("所选酒店不符合排除名单或每晚参考价上限。")
    return list(dict.fromkeys(errors))
