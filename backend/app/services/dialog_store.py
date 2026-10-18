"""User-owned conversation/memory storage. One DB transaction per state transition."""
import json
from uuid import uuid4

from fastapi import HTTPException

from . import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
 user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_session (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_expiry ON auth_session(expires_at);
CREATE TABLE IF NOT EXISTS conversation (
 conversation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 slots_json TEXT NOT NULL DEFAULT '{}', committed_json TEXT NOT NULL DEFAULT '{}',
 result_json TEXT, busy INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_turn (
 conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, revision INTEGER NOT NULL,
 message TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL, input_hash TEXT NOT NULL,
 PRIMARY KEY (conversation_id, request_id)
);
CREATE TABLE IF NOT EXISTS conversation_version (
 conversation_id TEXT NOT NULL, run_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owned_run (run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preference (
 user_id TEXT NOT NULL, key TEXT NOT NULL, value_json TEXT, version INTEGER NOT NULL,
 active INTEGER NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS preference_event (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, key TEXT NOT NULL,
 value_json TEXT, version INTEGER NOT NULL, active INTEGER NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_document (
 document_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
 content TEXT NOT NULL, valid_until TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_run (
 run_id TEXT PRIMARY KEY, report_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS completed_run (run_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS run_snapshot (run_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_operation (
 conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, input_hash TEXT NOT NULL,
 run_id TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
 PRIMARY KEY (conversation_id, request_id)
);
CREATE TABLE IF NOT EXISTS conversation_edit (
 conversation_id TEXT PRIMARY KEY, edit_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_clarification (
 conversation_id TEXT PRIMARY KEY, questions_json TEXT NOT NULL
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=store._json_default)


def create_conversation(user_id: str, defaults: dict) -> dict:
    identifier = uuid4().hex
    with store._connect() as db:
        db.execute("INSERT INTO conversation(conversation_id,user_id,slots_json,updated_at) VALUES(?,?,?,?)",
                   (identifier, user_id, encode(defaults), store._now()))
    return load_conversation(identifier, user_id)


def import_run(user_id, run_id, request, result):
    """Attach an owned form run once; ownership is rechecked inside the write."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        owner = db.execute("SELECT user_id,conversation_id FROM owned_run WHERE run_id=?", (run_id,)).fetchone()
        if not owner or owner["user_id"] != user_id:
            raise HTTPException(404, "找不到属于当前用户的行程。")
        if owner["conversation_id"]:
            identifier = owner["conversation_id"]
        else:
            identifier = uuid4().hex
            db.execute("INSERT INTO conversation(conversation_id,user_id,slots_json,committed_json,result_json,updated_at) VALUES(?,?,?,?,?,?)",
                       (identifier, user_id, encode(request), encode(request), encode(result), store._now()))
            db.execute("INSERT INTO conversation_version VALUES(?,?,?,?,?)",
                       (identifier, run_id, encode(request), encode(result), store._now()))
            db.execute("UPDATE owned_run SET conversation_id=? WHERE run_id=? AND user_id=?",
                       (identifier, run_id, user_id))
    return load_conversation(identifier, user_id)


def load_conversation(identifier: str, user_id: str) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT * FROM conversation WHERE conversation_id=? AND user_id=?", (identifier, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "会话不存在")
        turns = db.execute("SELECT message,response_json,revision FROM conversation_turn WHERE conversation_id=? ORDER BY revision DESC LIMIT 12",
                           (identifier,)).fetchall()
        pending = db.execute("SELECT edit_json FROM conversation_edit WHERE conversation_id=?", (identifier,)).fetchone()
        questions = db.execute("SELECT questions_json FROM conversation_clarification WHERE conversation_id=?", (identifier,)).fetchone()
    edit = json.loads(pending[0]) if pending else None
    questions = json.loads(questions[0]) if questions else []
    return {"conversation_id": identifier, "revision": row["revision"], "busy": bool(row["busy"]),
            "slots": json.loads(row["slots_json"]), "committed": json.loads(row["committed_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "pending_edit": public_edit(edit), "edit_preview": edit.get("preview") if edit else None,
            "_pending_edit": edit,
            "clarifications": public_questions(questions), "_clarifications": questions,
            "history": [{"message": r["message"], "reply": json.loads(r["response_json"]).get("reply", ""),
                         "revision": r["revision"]} for r in reversed(turns)]}


def claim(identifier: str, user_id: str, revision: int, request_id: str, input_hash: str):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT response_json,input_hash FROM conversation_turn WHERE conversation_id=? AND request_id=?",
                         (identifier, request_id)).fetchone()
        if row:
            if row["input_hash"] != input_hash:
                raise HTTPException(409, "同一个 request_id 不能提交不同内容")
            return json.loads(row["response_json"])
        pending = db.execute("SELECT input_hash,status FROM conversation_operation WHERE conversation_id=? AND request_id=?",
                             (identifier, request_id)).fetchone()
        if pending and (pending["input_hash"] != input_hash or pending["status"] == "interrupted"):
            raise HTTPException(409, "这次操作已中断或内容已改变，请查看已有版本后明确重新提交")
        updated = db.execute("UPDATE conversation SET busy=1 WHERE conversation_id=? AND user_id=? AND revision=? AND busy=0",
                             (identifier, user_id, revision))
        if updated.rowcount != 1:
            raise HTTPException(409, "会话版本已变化或正在处理，请刷新后重试")
    return None


def recover_interrupted():
    """Run once during single-worker application startup, never per request."""
    with store._connect() as db:
        db.execute("UPDATE conversation_operation SET status='interrupted' WHERE status='running'")
        db.execute("UPDATE conversation SET busy=0")


def operation(identifier, request_id, digest):
    with store._connect() as db:
        row = db.execute("SELECT * FROM conversation_operation WHERE conversation_id=? AND request_id=?",
                         (identifier, request_id)).fetchone()
        if row:
            if row["status"] == "running":
                raise HTTPException(409, "上一次生成尚未确认结束，请检查版本后再提交")
            return row["run_id"], json.loads(row["result_json"])
        run_id = uuid4().hex
        db.execute("INSERT INTO conversation_operation VALUES(?,?,?,?,?,NULL)",
                   (identifier, request_id, digest, run_id, "running"))
    return run_id, None


def save_candidate(identifier, request_id, run_id, req, result):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE conversation_operation SET status='complete',result_json=? WHERE conversation_id=? AND request_id=?",
                   (encode(result), identifier, request_id))
        db.execute("INSERT OR IGNORE INTO conversation_version VALUES(?,?,?,?,?)",
                   (identifier, run_id, encode(req), encode(result), store._now()))


def public_edit(pending):
    if not pending:
        return None
    return {**pending["edit"], "base_thread_id": pending["base_thread_id"], "summary": pending["summary"]}


def public_questions(questions):
    return [{"question_id": q["question_id"], "module": q["module"], "question": q["question"],
             "options": [{"label": option["label"]} for option in q["options"]]} for q in questions[:3]]


def finish(identifier, revision, request_id, message, slots, committed, result, response, input_hash, pending_edit=None,
           clarifications=None):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute("UPDATE conversation SET slots_json=?,committed_json=?,result_json=?,revision=revision+1,busy=0,updated_at=? WHERE conversation_id=? AND revision=? AND busy=1",
                             (encode(slots), encode(committed), encode(result) if result else None, store._now(), identifier, revision))
        if updated.rowcount != 1:
            raise HTTPException(409, "会话版本冲突")
        if pending_edit:
            db.execute("INSERT INTO conversation_edit VALUES(?,?) ON CONFLICT(conversation_id) DO UPDATE SET edit_json=excluded.edit_json",
                       (identifier, encode(pending_edit)))
        else:
            db.execute("DELETE FROM conversation_edit WHERE conversation_id=?", (identifier,))
        if clarifications:
            db.execute("INSERT INTO conversation_clarification VALUES(?,?) ON CONFLICT(conversation_id) DO UPDATE SET questions_json=excluded.questions_json",
                       (identifier, encode(clarifications)))
        else:
            db.execute("DELETE FROM conversation_clarification WHERE conversation_id=?", (identifier,))
        db.execute("INSERT INTO conversation_turn VALUES(?,?,?,?,?,?,?)",
                   (identifier, request_id, revision + 1, message, encode(response), store._now(), input_hash))


def release(identifier, revision):
    with store._connect() as db:
        db.execute("UPDATE conversation SET busy=0 WHERE conversation_id=? AND revision=?", (identifier, revision))


def preferences(user_id):
    with store._connect() as db:
        rows = db.execute("SELECT * FROM preference WHERE user_id=?", (user_id,)).fetchall()
    return [{"key": r["key"], "value": json.loads(r["value_json"]) if r["value_json"] else None,
             "version": r["version"], "active": bool(r["active"]), "source": r["source"],
             "updated_at": r["updated_at"]} for r in rows]


def write_preference(user_id, key, value, expected_version, source, *, active=True):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT version FROM preference WHERE user_id=? AND key=?", (user_id, key)).fetchone()
        version = row["version"] if row else 0
        if version != expected_version:
            raise HTTPException(409, "偏好版本已变化，请刷新")
        args = (user_id, key, encode(value), version + 1, int(active), source, store._now())
        db.execute("INSERT INTO preference VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value_json=excluded.value_json,version=excluded.version,active=excluded.active,source=excluded.source,updated_at=excluded.updated_at", args)
        db.execute("INSERT INTO preference_event(user_id,key,value_json,version,active,source,created_at) VALUES(?,?,?,?,?,?,?)", args)
    return version + 1
