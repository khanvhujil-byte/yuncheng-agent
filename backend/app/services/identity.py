"""Local login identity for conversation and explicit cross-session preferences."""
import hashlib
import secrets
import sqlite3
import time
from uuid import uuid4

from fastapi import Header, HTTPException

from ..config import get_settings
from . import store


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()


def register(username, password):
    salt = secrets.token_bytes(16)
    user_id = uuid4().hex
    try:
        with store._connect() as db:
            db.execute("INSERT INTO account VALUES(?,?,?,?)", (user_id, username.lower(),
                       salt.hex() + ":" + _hash(password, salt), store._now()))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "用户名已存在") from exc
    return {"user_id": user_id, "username": username.lower()}


def login(username, password):
    with store._connect() as db:
        row = db.execute("SELECT * FROM account WHERE username=?", (username.lower(),)).fetchone()
        stored = row["password_hash"] if row else "00" * 16 + ":" + "00" * 32
        salt, expected = stored.split(":")
        valid = secrets.compare_digest(_hash(password, bytes.fromhex(salt)), expected)
        if row is None or not valid:
            raise HTTPException(401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        expires = time.time() + get_settings().session_ttl_seconds
        db.execute("DELETE FROM auth_session WHERE expires_at<?", (time.time(),))
        db.execute("INSERT INTO auth_session VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), row["user_id"], expires))
    return {"token": token, "expires_at": expires, "user_id": row["user_id"], "username": row["username"]}


def current_user(x_session_token: str | None = Header(default=None)) -> str:
    if not isinstance(x_session_token, str) or not x_session_token:
        raise HTTPException(401, "请先登录")
    with store._connect() as db:
        row = db.execute("SELECT user_id FROM auth_session WHERE token_hash=? AND expires_at>?",
                         (hashlib.sha256(x_session_token.encode()).hexdigest(), time.time())).fetchone()
    if row is None:
        raise HTTPException(401, "登录已失效，请重新登录")
    return row["user_id"]


def authorize_run(thread_id: str, x_session_token: str | None = Header(default=None)):
    with store._connect() as db:
        owner = db.execute("SELECT user_id FROM owned_run WHERE run_id=?", (thread_id,)).fetchone()
    if owner and owner["user_id"] != current_user(x_session_token):
        raise HTTPException(404, "任务不存在")


def optional_user(x_session_token: str | None = Header(default=None)) -> str | None:
    """Anonymous forms remain usable; supplied sessions must be valid."""
    return current_user(x_session_token) if isinstance(x_session_token, str) and x_session_token else None
