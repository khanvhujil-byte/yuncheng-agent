"""Authenticated dialogue, explicit preference memory and private document knowledge."""
import hashlib
import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError

from ...models.dialog import Credentials, DocumentInput, PreferenceInput, RunImport, SlotPatch, TurnInput
from ...services import conversation, dialog_store, identity, knowledge, monitoring, store
from ..security import rate_limit, require_api_key

router = APIRouter(tags=["对话与记忆"], dependencies=[Depends(require_api_key), Depends(rate_limit)])


def auth_limit(request: Request):
    from ..security import _hits, _lock, client_key
    now = time.time()
    with _lock:
        window = _hits["auth:" + client_key(request)]
        while window and now-window[0] >= 60:
            window.popleft()
        if len(window) >= 10:
            raise HTTPException(429, "登录请求过于频繁，请稍后重试", headers={"Retry-After": "60"})
        window.append(now)


@router.post("/auth/register", dependencies=[Depends(auth_limit)])
def register(body: Credentials):
    return identity.register(body.username, body.password)


@router.post("/auth/login", dependencies=[Depends(auth_limit)])
def login(body: Credentials):
    return identity.login(body.username, body.password)


@router.post("/auth/logout")
def logout(user_id: str = Depends(identity.current_user), x_session_token: str = Header()):
    with store._connect() as db:
        db.execute("DELETE FROM auth_session WHERE token_hash=? AND user_id=?",
                   (hashlib.sha256(x_session_token.encode()).hexdigest(), user_id))
    return {"success": True}


@router.get("/auth/me")
def me(user_id: str = Depends(identity.current_user)):
    with store._connect() as db:
        row = db.execute("SELECT username FROM account WHERE user_id=?", (user_id,)).fetchone()
    return {"user_id": user_id, "username": row[0]}


@router.post("/conversations")
def create(user_id: str = Depends(identity.current_user)):
    current = dialog_store.create_conversation(user_id, conversation.defaults(user_id))
    return {key: value for key, value in current.items() if key not in ("committed", "_pending_edit", "_clarifications")}


@router.get("/conversations")
def listing(user_id: str = Depends(identity.current_user)):
    with store._connect() as db:
        rows = db.execute("SELECT conversation_id,revision,busy,updated_at FROM conversation WHERE user_id=? ORDER BY updated_at DESC LIMIT 100", (user_id,)).fetchall()
    return [dict(r) for r in rows]


@router.post("/conversations/from-run")
def from_run(body: RunImport, user_id: str = Depends(identity.current_user)):
    from ...models.schemas import TripPlanResponse, TripRequest
    from ...services import runner
    with store._connect() as db:
        row = db.execute("SELECT conversation_id FROM owned_run WHERE run_id=? AND user_id=?", (body.thread_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "找不到属于当前用户的行程。匿名历史行程不能认领。")
    if row["conversation_id"]:
        current = dialog_store.load_conversation(row["conversation_id"], user_id)
    else:
        job = runner.get_job(body.thread_id)
        saved = store.load_run(body.thread_id)
        if job is None or job.status != "done" or not saved or not job.result:
            raise HTTPException(409, "行程尚未完成或结果不可恢复，请先检查任务状态。")
        try:
            response = TripPlanResponse.model_validate(job.result)
            req = TripRequest.model_validate(saved["request"])
        except (ValidationError, TypeError, ValueError) as exc:
            raise HTTPException(409, "保存的行程格式不兼容，原任务仍保留；请重新规划后再进入局部修改。") from exc
        if not response.success or response.data is None or response.thread_id != body.thread_id:
            raise HTTPException(422, "当前结果不是完整有效行程，暂不能进入局部修改。")
        current = dialog_store.import_run(user_id, body.thread_id, req.model_dump(), response.model_dump(mode="json"))
    return {key: value for key, value in current.items() if key not in ("committed", "_pending_edit", "_clarifications")}


@router.get("/conversations/{identifier}")
def get(identifier: str, user_id: str = Depends(identity.current_user)):
    result = dialog_store.load_conversation(identifier, user_id)
    return {key: value for key, value in result.items() if key not in ("committed", "_pending_edit", "_clarifications")}


@router.get("/conversations/{identifier}/edit-candidates")
def edit_candidates(identifier: str, module: str, day_index: int | None = None,
                    user_id: str = Depends(identity.current_user)):
    from ...services import partial_edit
    current = dialog_store.load_conversation(identifier, user_id)
    return partial_edit.candidates(current, module, day_index)


@router.post("/conversations/{identifier}/turns")
def turn(identifier: str, body: TurnInput, user_id: str = Depends(identity.current_user)):
    return conversation.turn(identifier, user_id, body)


@router.get("/conversations/{identifier}/versions")
def versions(identifier: str, user_id: str = Depends(identity.current_user)):
    dialog_store.load_conversation(identifier, user_id)
    with store._connect() as db:
        rows = db.execute("SELECT run_id,request_json,result_json,created_at FROM conversation_version WHERE conversation_id=? ORDER BY rowid", (identifier,)).fetchall()
    return [{"run_id": r["run_id"], "request": json.loads(r["request_json"]),
             "result": json.loads(r["result_json"]), "created_at": r["created_at"]} for r in rows]


@router.get("/preferences")
def preferences(user_id: str = Depends(identity.current_user)):
    return dialog_store.preferences(user_id)


@router.put("/preferences/{key}")
def update_preference(key: str, body: PreferenceInput, user_id: str = Depends(identity.current_user)):
    if key not in conversation.MEMORY_KEYS:
        raise HTTPException(422, "该字段不支持跨会话偏好")
    try:
        value = SlotPatch.model_validate({key: body.value}).model_dump(exclude_unset=True)[key]
    except ValueError as exc:
        raise HTTPException(422, "偏好类型或范围不合法") from exc
    if value is None:
        raise HTTPException(422, "撤回偏好请使用删除操作")
    version = dialog_store.write_preference(user_id, key, value, body.expected_version, body.source)
    return {"success": True, "version": version}


@router.delete("/preferences/{key}")
def delete_preference(key: str, expected_version: int, user_id: str = Depends(identity.current_user)):
    if key not in conversation.MEMORY_KEYS or expected_version < 1:
        raise HTTPException(422, "偏好或版本不合法")
    return {"version": dialog_store.write_preference(user_id, key, None, expected_version, "用户明确撤回", active=False)}


@router.get("/preferences/history")
def preference_history(user_id: str = Depends(identity.current_user)):
    with store._connect() as db:
        rows = db.execute("SELECT key,value_json,version,active,source,created_at FROM preference_event WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)).fetchall()
    return [{**dict(r), "value_json": json.loads(r["value_json"])} for r in rows]


@router.post("/knowledge/documents")
def add_document(body: DocumentInput, user_id: str = Depends(identity.current_user)):
    return {"document_id": knowledge.add_document(user_id, body)}


@router.get("/knowledge/documents")
def documents(user_id: str = Depends(identity.current_user)):
    with store._connect() as db:
        rows = db.execute("SELECT document_id,title,source,valid_until,updated_at FROM knowledge_document WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
    return [dict(row) for row in rows]


@router.delete("/knowledge/documents/{identifier}")
def delete_document(identifier: str, user_id: str = Depends(identity.current_user)):
    with store._connect() as db:
        deleted = db.execute("DELETE FROM knowledge_document WHERE document_id=? AND user_id=?", (identifier, user_id))
        if deleted.rowcount != 1:
            raise HTTPException(404, "资料不存在")
    return {"success": True}


@router.get("/knowledge/search")
def search(query: str, user_id: str = Depends(identity.current_user)):
    if not 1 <= len(query.strip()) <= 1000:
        raise HTTPException(422, "查询长度应为 1～1000")
    results, stats = knowledge.retrieve(user_id, query)
    return {"results": results, "stats": stats}


@router.get("/observability/quality")
def quality():
    return monitoring.summary()


@router.get("/observability/runs/{thread_id}", dependencies=[Depends(identity.authorize_run)])
def report(thread_id: str):
    with store._connect() as db:
        row = db.execute("SELECT report_json FROM quality_run WHERE run_id=?", (thread_id,)).fetchone()
    if not row:
        raise HTTPException(404, "运行记录不存在")
    return json.loads(row[0])
