"""Explicit intent routing, durable slots and versioned planning; facts never come from a summary."""
import hashlib
import json
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from ..models.dialog import ClarificationQuestion, IntentDecision, TurnInput
from ..models.schemas import TripRequest, TripPlanResponse
from . import dialog_store as db, llm, partial_edit, planning, store
from .request_validation import validate_request
from .state_codec import restore_state_types

MEMORY_KEYS = {"transportation", "accommodation", "preferences", "budget_limit"}
REQUIRED = {"city": "目的地", "start_date": "出发日期（YYYY-MM-DD）", "travel_days": "旅行天数",
            "transportation": "交通方式", "accommodation": "住宿偏好"}


def defaults(user_id):
    try:
        return {item["key"]: item["value"] for item in db.preferences(user_id) if item["active"]}
    except Exception:
        # Stable memory is optional; explicit input must remain usable.
        return {}


def extract(message, current):
    from ..agents.prompts import INTENT_AGENT_PROMPT
    prompt = (INTENT_AGENT_PROMPT + "返回 JSON，只做旅行意图分类和用户明确说出的槽位增量，不生成行程、不猜测缺失字段。"
              "intent 只能为 new_plan/modify_plan/edit_module/explain_plan/knowledge/clarify。"
              "new_plan 表示明确换一个新行程；补充当前请求用 modify_plan。"
              "已生成行程中只换酒店、某天某餐/景点或调整某一天，必须选 edit_module，不能重做全程。"
              "edit.module 为 hotel/meals/attractions/day；day_index/item_index 都从0计数。"
              "只填用户能明确定位的日期和条目；不确定不要猜，instruction保留具体要求。"
              "修改城市、出行日期、天数、总预算等整体约束仍选 modify_plan。"
              "slot_updates 必须符合给定 JSON Schema，未提及的键不要输出，不用 null 表示未提及。"
              "‘不自驾’应写 transportation=公共交通；‘这次’是本次约束，不是长期记忆。"
              "用户每个环节的要求均应保留：requirements 下 food/attractions/hotel/transport/pace/weather/budget。"
              "模块instructions保存该模块的原话，明确可执行的数值和选项同时写结构字段；不要把原话只留在总结。"
              "food.priority=food_first表示围绕美食安排，travel_scope=citywide表示取消就近优先；仅说自驾不能推断不限距离。"
              "只填写明确给出的增量，不能用模块默认值覆盖之前偏好。"
              "以下情况必须clarify并给clarifications，禁止自行选：指代对象不清、要求冲突、关键取舍不清、"
              "所需事实无法核实（过敏安全、无障碍设施、营业预约等不能靠POI名称保证）。"
              "每次最多问3个关键问题，各给2到4个有不同影响的短选项；需要具体名称/日期时可无选项。"
              "每个选项slot_updates只填写用户选择后才生效的字段；不得预先写入尚未选择的假设。"
              "无法核实的硬性要求，要解释缺少何种证据，让用户补资料或明确放宽，不能用模型猜测满足。"
              "这些待核实硬要求也写入相应模块unverified，不能只放instructions后声称满足。"
              "不能把‘轻松点’擅自换算为分钟，把‘自驾’换算成不限距离。"
              "选择放宽硬性要求时，选项必须明确清除对应unverified并在instructions保留仅作偏好的说明。"
              "pending_questions是尚未解决的问题。用户明确回答或撤回的问题，在resolved_question_ids填对应question_id；"
              "未回答的问题由服务器继续保留，不要重复生成。clarifications只填写新增问题。"
              "不得用补充了无关字段当作问题已解决；其他可选字段没提供不必追问。"
              "预约、退改、开放规则等资料问题选 knowledge；为什么选择某酒店或景点选 explain_plan。"
              + json.dumps(IntentDecision.model_json_schema(), ensure_ascii=False))
    history = [{"message": h["message"][:500], "reply": h["reply"][:500]} for h in current["history"][-6:]]
    data = ((current.get("result") or {}).get("data") or {})
    summary = {"hotel": (data.get("hotel") or {}).get("name"),
               "days": [{"day_index": day["day_index"], "date": day["date"],
                         "attractions": [a["name"] for a in day.get("attractions", [])],
                         "meals": [{"type": m["type"], "name": m["name"]} for m in day.get("meals", [])]}
                        for day in data.get("days", [])]}
    text, usage = llm.chat_json(prompt, json.dumps({"slots": current["slots"], "recent": history,
                                                   "current_plan": summary, "pending_questions": current.get("clarifications", []),
                                                   "message": message}, ensure_ascii=False))
    return IntentDecision.model_validate_json(text), usage


def merge(slots, updates):
    merged = dict(slots)
    # Derived values from a prior turn must not contradict a changed date/day count.
    if "start_date" in updates or "travel_days" in updates:
        for key in ("end_date", "nights"):
            if key not in updates:
                merged.pop(key, None)
    if "city" in updates and updates["city"] != slots.get("city"):
        for key in ("must_visit", "fixed_hotel", "fixed_hotel_address"):
            if key not in updates:
                merged.pop(key, None)
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        elif key == "requirements":
            from .requirements import deep_merge
            merged[key] = deep_merge(merged.get(key) or {}, value)
        else:
            merged[key] = value
    return merged


def ready(slots):
    missing = [label for key, label in REQUIRED.items() if not slots.get(key)]
    if missing:
        return None, "请补充：" + "、".join(missing)
    try:
        req = TripRequest.model_validate(slots)
    except ValidationError:
        return None, "请求字段类型或范围不合法，请检查日期、天数、预算和住宿晚数。"
    invalid = validate_request(req)
    return (None, invalid[1]) if invalid else (req, "请确认以上要求，确认后开始生成。")


def reusable_seed(previous, req):
    if not previous or not previous.get("thread_id"):
        return None
    with store._connect() as conn:
        row = conn.execute("SELECT state_json,recorded_at FROM run_snapshot WHERE run_id=?", (previous["thread_id"],)).fetchone()
    if not row or time.time() - row["recorded_at"] > 300:
        return None
    state = restore_state_types(json.loads(row["state_json"]))
    old = state["request"].model_dump()
    identity = ("city", "start_date", "travel_days", "preferences", "must_visit", "fixed_hotel", "fixed_hotel_address", "requirements", "free_text_input")
    if any(old.get(k) != req.model_dump().get(k) for k in identity):
        return None
    if not state.get("adcode") or not state.get("attractions") or not state.get("weather"):
        return None
    if any(v == "degraded" for k, v in state.get("sources", {}).items() if k in ("attractions", "weather")):
        return None
    return {**{key: state[key] for key in ("adcode", "city_center", "must_visit_resolved", "hotel_anchor", "attractions", "weather", "community_hot", "restaurants", "route_cache") if state.get(key) is not None},
            "sources": {k: v for k, v in state.get("sources", {}).items() if k != "hotel"}}


def explain(result):
    data = (result or {}).get("data") or {}
    if not data:
        return "当前还没有可解释的行程，请先完成规划。"
    reason = data.get("hotel_reason")
    if reason:
        return "住宿选择依据：" + reason
    return "当前行程未保存具体住宿选择理由。景点安排和费用请查看行程详情，不能补造当时的决策依据。"


def _questions(items):
    return [{"question_id": uuid4().hex, **item.model_dump(exclude_unset=True),
             "options": [o.model_dump(exclude_unset=True) for o in item.options]} for item in items]


def _question_reply(questions):
    return "请先确认：" + "；".join(q["question"] for q in questions)


def turn(identifier: str, user_id: str, command: TurnInput):
    from .execution_lease import hold, Busy
    try:
        with hold('conversation:' + identifier) as lease:
            return _turn(identifier, user_id, command, lease)
    except Busy as exc:
        raise HTTPException(409, '会话正在处理，请使用原 request_id 重试。') from exc


def _turn(identifier: str, user_id: str, command: TurnInput, lease):
    db.load_conversation(identifier, user_id)
    digest = hashlib.sha256(command.model_dump_json(exclude={"expected_revision"}).encode()).hexdigest()
    cached = db.claim(identifier, user_id, command.expected_revision, command.request_id, digest)
    if cached:
        return cached
    current = db.load_conversation(identifier, user_id)
    slots = dict(current["slots"])
    committed = dict(current["committed"])
    result = current["result"]
    pending = current.get("_pending_edit")
    questions = current.get("_clarifications") or []
    usage = {}
    citations = []
    candidate = None
    run_id = None
    finished = False
    try:
        if command.action != "answer" and command.answer is not None:
            raise HTTPException(422, "选项回答请使用 action=answer。")
        if command.action == "reject":
            slots = committed or defaults(user_id)
            pending = None
            questions = []
            intent, reply = "reject", "已撤销尚未确认的修改，保留原行程。"
        elif command.action == "answer":
            if command.answer is None or command.message.strip() or command.edit is not None or command.slot_updates.model_fields_set:
                raise HTTPException(422, "选项回答只能提交 answer，不可夹带其他要求。")
            question = next((q for q in questions if q["question_id"] == command.answer.question_id), None)
            if question is None or command.answer.option_index >= len(question["options"]):
                raise HTTPException(409, "这个问题或选项已经变化，请刷新后重新选择。")
            selected = question["options"][command.answer.option_index]
            slots = merge(slots, selected.get("slot_updates") or {})
            # Re-validate stored choices as they cross the durable boundary.
            from ..models.dialog import SlotPatch
            SlotPatch.model_validate(selected.get("slot_updates") or {})
            questions = [q for q in questions if q["question_id"] != question["question_id"]]
            pending = None
            intent = "clarify"
            _, reply = ready(slots)
            reply = "已选择「" + selected["label"] + "」。" + (_question_reply(questions) if questions else reply)
        elif command.action == "confirm":
            if command.slot_updates.model_fields_set or command.message.strip() or command.edit is not None:
                raise HTTPException(422, "确认操作不能同时修改要求，请先提交修改")
            req, reply = (None, _question_reply(questions)) if questions else ready(slots)
            intent = "edit_module" if pending else "plan"
            if req:
                run_id, saved = db.operation(identifier, command.request_id, digest)
                if saved:
                    candidate = TripPlanResponse.model_validate(saved)
                else:
                    with store._connect() as conn:
                        conn.execute("INSERT OR IGNORE INTO owned_run VALUES(?,?,?)", (run_id, user_id, identifier))
                    candidate = partial_edit.apply(current, pending, run_id) if pending else planning.plan(
                        req, run_id=run_id, seed=reusable_seed(result, req), cache_scope=user_id)
                    lease.check()
                    db.save_candidate(identifier, command.request_id, run_id, req.model_dump(), candidate.model_dump(mode="json"))
                payload = candidate.model_dump(mode="json")
                if candidate.success:
                    result, committed = payload, req.model_dump()
                    slots = dict(committed)
                    reply = "局部修改已完成，其他模块已保留。" if pending else "行程已生成，可继续提出修改。"
                    pending = None
                else:
                    reply = candidate.message or "本次规划未完成，已保留原行程。"
                    if (not result or not result.get('success')) and candidate.data and candidate.data.days:
                        result, committed = payload, req.model_dump()
                        slots = dict(committed)
                        pending = None
                        reply += ' 已保留可修复草稿，可选择具体模块修改。'
        elif command.action == "edit":
            if questions:
                raise HTTPException(409, "请先回答待确认的问题，原行程仍保留。")
            if command.edit is None or command.slot_updates.model_fields_set or command.message.strip():
                raise HTTPException(422, "局部修改请只提交 edit 字段；自然语言要求请填写 edit.instruction。")
            pending, usage = partial_edit.prepare(current, command.edit)
            intent, reply = "edit_module", pending["summary"] + "请确认本次局部修改。"
        else:
            if command.edit is not None:
                raise HTTPException(422, "结构化局部修改请使用 action=edit。")
            if command.slot_updates.model_fields_set:
                decision = IntentDecision(intent="modify_plan", slot_updates=command.slot_updates)
            elif command.message.strip():
                decision, usage = extract(command.message, current)
            else:
                raise HTTPException(422, "请输入要求或提供结构化字段")
            intent = decision.intent
            if intent == "new_plan":
                slots = defaults(user_id)
                questions = []
            elif decision.resolved_question_ids:
                valid_ids = {q["question_id"] for q in questions}
                if not set(decision.resolved_question_ids) <= valid_ids:
                    raise ValueError("模型引用了不存在的追问编号")
                questions = [q for q in questions if q["question_id"] not in decision.resolved_question_ids]
            if decision.clarifications:
                # Preserve all explicit, already certain input, but never apply
                # any proposed option before the user selects it.
                slots = merge(slots, decision.slot_updates.model_dump(exclude_unset=True))
                seen = {(q["module"], q["question"]) for q in questions}
                questions = [*questions, *_questions([q for q in decision.clarifications if (q.module, q.question) not in seen])]
                pending = None
                intent, reply = "clarify", _question_reply(questions)
            elif intent in ("modify_plan", "new_plan", "clarify"):
                slots = merge(slots, decision.slot_updates.model_dump(exclude_unset=True))
                if not command.slot_updates.model_fields_set:
                    if intent == "clarify":
                        questions = questions or _questions([ClarificationQuestion(module="general", question="你希望调整哪个环节，以及希望怎样调整？")])
                if decision.slot_updates.model_fields_set or intent == "new_plan":
                    pending = None
                _, reply = ready(slots)
                if questions:
                    reply = _question_reply(questions)
            elif intent == "edit_module":
                if questions:
                    reply = _question_reply(questions)
                elif decision.slot_updates.model_fields_set:
                    reply = "局部模块修改不能同时更改城市、日期或总预算等整体要求，请分开提交，原行程已保留。"
                elif decision.edit is None:
                    reply = "请明确要修改酒店、哪一天的景点或哪一餐，原行程会保留。"
                else:
                    try:
                        pending, edit_usage = partial_edit.prepare(current, decision.edit)
                        usage = {key: usage.get(key, 0) + edit_usage.get(key, 0) for key in set(usage) | set(edit_usage)}
                        reply = pending["summary"] + "请确认本次局部修改。"
                    except HTTPException as exc:
                        if exc.status_code not in (409, 422):
                            raise
                        reply = str(exc.detail)
            elif intent == "explain_plan":
                reply = explain(result)
            else:
                from .knowledge import answer
                reply, citations = answer(user_id, command.message)
        response = {"conversation_id": identifier, "revision": current["revision"] + 1,
                    "intent": intent, "reply": reply, "slots": slots, "result": result,
                    "candidate_result": candidate.model_dump(mode="json") if candidate else None,
                    "run_id": run_id, "ready": not questions and ready(slots)[0] is not None, "usage": usage,
                    "clarifications": db.public_questions(questions),
                    "pending_edit": db.public_edit(pending), "edit_preview": pending["preview"] if pending else None,
                    "citations": citations}
        lease.check()
        db.finish(identifier, current["revision"], command.request_id, command.message, slots, committed, result, response, digest,
                  pending_edit=pending, clarifications=questions)
        finished = True
        return response
    except (llm.LlmError, llm.LlmUnreachable, ValidationError, ValueError) as exc:
        raise HTTPException(502, "意图解析失败，已保留原要求，请重试或使用结构化输入") from exc
    finally:
        if not finished:
            db.release(identifier, current["revision"])
