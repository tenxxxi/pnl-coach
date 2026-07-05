"""PnL Coach — 멀티거래소 선물 손익 코치 웹앱.

실행: ./run.sh  (기본 포트 8777)
키는 전부 웹 UI로 입력받아 Fernet 암호화 후 data/에 저장. 코드에 키 없음.
"""
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

import advisor
import security
from db import (get_db, now_ms, set_sync_log, upsert_cashflows,
                upsert_positions, upsert_transfers)
from exchanges import SUPPORTED, fetch_closed
from stats import compute_stats, diagnose

app = FastAPI(title="PnL Coach")

KST = timezone(timedelta(hours=9))
SESSION_TTL_MS = 30 * 86_400_000
SYNC_INTERVAL_SEC = 30 * 60
SYNC_LOOKBACK_DAYS = 365


# ---------------------------------------------------------------- auth
def require_user(session: str | None) -> int:
    if not session:
        raise HTTPException(401, "로그인 필요")
    row = get_db().execute(
        "SELECT user_id, expires_at FROM sessions WHERE token=?", (session,)
    ).fetchone()
    if not row or row["expires_at"] < now_ms():
        raise HTTPException(401, "세션 만료")
    return row["user_id"]


class Credentials(BaseModel):
    username: str
    password: str


def _issue_session(resp: Response, user_id: int):
    token = security.new_session_token()
    get_db().execute(
        "INSERT INTO sessions(token, user_id, expires_at) VALUES (?,?,?)",
        (token, user_id, now_ms() + SESSION_TTL_MS),
    )
    get_db().commit()
    resp.set_cookie("session", token, httponly=True, samesite="lax",
                    max_age=SESSION_TTL_MS // 1000)


@app.post("/api/register")
def register(body: Credentials, resp: Response):
    if len(body.username) < 2 or len(body.password) < 6:
        raise HTTPException(400, "아이디 2자+, 비밀번호 6자+ 필요")
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users(username, pw_hash, created_at) VALUES (?,?,?)",
            (body.username, security.hash_password(body.password), now_ms()),
        )
        db.commit()
    except Exception:
        raise HTTPException(409, "이미 존재하는 아이디")
    _issue_session(resp, cur.lastrowid)
    return {"ok": True, "username": body.username}


@app.post("/api/login")
def login(body: Credentials, resp: Response):
    row = get_db().execute(
        "SELECT id, pw_hash FROM users WHERE username=?", (body.username,)
    ).fetchone()
    if not row or not security.verify_password(body.password, row["pw_hash"]):
        raise HTTPException(401, "아이디 또는 비밀번호 불일치")
    _issue_session(resp, row["id"])
    return {"ok": True, "username": body.username}


@app.post("/api/logout")
def logout(resp: Response, session: str | None = Cookie(None)):
    if session:
        get_db().execute("DELETE FROM sessions WHERE token=?", (session,))
        get_db().commit()
    resp.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(session: str | None = Cookie(None)):
    uid = require_user(session)
    row = get_db().execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    return {"username": row["username"]}


# ---------------------------------------------------------------- keys
class ExchangeKey(BaseModel):
    exchange: str
    api_key: str
    secret: str
    passphrase: str | None = None


@app.post("/api/exchange-keys")
def add_exchange_key(body: ExchangeKey, session: str | None = Cookie(None)):
    uid = require_user(session)
    if body.exchange not in SUPPORTED:
        raise HTTPException(400, f"지원 거래소: {SUPPORTED}")
    db = get_db()
    db.execute(
        """INSERT INTO exchange_keys(user_id, exchange, api_key_enc, secret_enc,
           passphrase_enc, created_at) VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id, exchange) DO UPDATE SET
           api_key_enc=excluded.api_key_enc, secret_enc=excluded.secret_enc,
           passphrase_enc=excluded.passphrase_enc""",
        (uid, body.exchange, security.encrypt(body.api_key),
         security.encrypt(body.secret),
         security.encrypt(body.passphrase) if body.passphrase else None,
         now_ms()),
    )
    db.commit()
    return {"ok": True}


@app.get("/api/exchange-keys")
def list_exchange_keys(session: str | None = Cookie(None)):
    uid = require_user(session)
    rows = get_db().execute(
        "SELECT exchange, api_key_enc, created_at FROM exchange_keys WHERE user_id=?",
        (uid,),
    ).fetchall()
    return [{"exchange": r["exchange"],
             "api_key": security.mask(security.decrypt(r["api_key_enc"])),
             "created_at": r["created_at"]} for r in rows]


@app.delete("/api/exchange-keys/{exchange}")
def delete_exchange_key(exchange: str, session: str | None = Cookie(None)):
    uid = require_user(session)
    db = get_db()
    db.execute("DELETE FROM exchange_keys WHERE user_id=? AND exchange=?",
               (uid, exchange))
    db.execute("DELETE FROM sync_log WHERE user_id=? AND exchange=?",
               (uid, exchange))
    db.commit()
    return {"ok": True}


class LlmKey(BaseModel):
    provider: str
    key: str
    model: str | None = None


@app.post("/api/llm-key")
def set_llm_key(body: LlmKey, session: str | None = Cookie(None)):
    uid = require_user(session)
    if body.provider not in ("anthropic", "openai"):
        raise HTTPException(400, "provider: anthropic | openai")
    db = get_db()
    db.execute(
        """INSERT INTO llm_keys(user_id, provider, key_enc, model) VALUES (?,?,?,?)
           ON CONFLICT(user_id, provider) DO UPDATE SET
           key_enc=excluded.key_enc, model=excluded.model""",
        (uid, body.provider, security.encrypt(body.key), body.model),
    )
    db.commit()
    return {"ok": True}


@app.get("/api/llm-key")
def get_llm_keys(session: str | None = Cookie(None)):
    uid = require_user(session)
    rows = get_db().execute(
        "SELECT provider, key_enc, model FROM llm_keys WHERE user_id=?", (uid,)
    ).fetchall()
    return [{"provider": r["provider"],
             "key": security.mask(security.decrypt(r["key_enc"])),
             "model": r["model"]} for r in rows]


# ---------------------------------------------------------------- sync
def _sync_one(uid: int, exchange: str, key_enc: str, secret_enc: str,
              passphrase_enc: str | None) -> dict:
    since_ms = now_ms() - SYNC_LOOKBACK_DAYS * 86_400_000
    try:
        positions, flows, transfers = fetch_closed(
            exchange,
            security.decrypt(key_enc),
            security.decrypt(secret_enc),
            security.decrypt(passphrase_enc) if passphrase_enc else None,
            since_ms,
        )
        new_p = upsert_positions(uid, exchange, positions)
        new_f = upsert_cashflows(uid, exchange, flows)
        new_t = upsert_transfers(uid, exchange, transfers)
        set_sync_log(uid, exchange, "ok",
                     f"+{new_p} positions, +{new_f} flows, +{new_t} transfers")
        return {"exchange": exchange, "ok": True, "new_positions": new_p}
    except Exception as e:
        set_sync_log(uid, exchange, "error", str(e))
        return {"exchange": exchange, "ok": False, "error": str(e)[:300]}


@app.post("/api/sync")
def sync_now(session: str | None = Cookie(None)):
    uid = require_user(session)
    rows = get_db().execute(
        "SELECT exchange, api_key_enc, secret_enc, passphrase_enc FROM exchange_keys WHERE user_id=?",
        (uid,),
    ).fetchall()
    if not rows:
        raise HTTPException(400, "등록된 거래소 키 없음")
    return [_sync_one(uid, r["exchange"], r["api_key_enc"], r["secret_enc"],
                      r["passphrase_enc"]) for r in rows]


def _background_sync_loop():
    while True:
        time.sleep(SYNC_INTERVAL_SEC)
        try:
            rows = get_db().execute(
                "SELECT user_id, exchange, api_key_enc, secret_enc, passphrase_enc FROM exchange_keys"
            ).fetchall()
            for r in rows:
                _sync_one(r["user_id"], r["exchange"], r["api_key_enc"],
                          r["secret_enc"], r["passphrase_enc"])
                time.sleep(2)
        except Exception:
            traceback.print_exc()


threading.Thread(target=_background_sync_loop, daemon=True).start()


# ---------------------------------------------------------------- stats
def _since_ms(since: str | None) -> int:
    if not since or since == "all":
        return 0
    if since.endswith("d"):
        return now_ms() - int(since[:-1]) * 86_400_000
    return int(datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=KST).timestamp() * 1000)


@app.get("/api/stats")
def stats(since: str | None = None, session: str | None = Cookie(None)):
    uid = require_user(session)
    s = compute_stats(uid, _since_ms(since))
    findings = diagnose(uid, _since_ms(since), s)
    return {"stats": s, "findings": findings}


# ---------------------------------------------------------------- advice
class AdviceReq(BaseModel):
    since: str | None = None
    provider: str | None = None


@app.post("/api/advice")
def make_advice(body: AdviceReq, session: str | None = Cookie(None)):
    uid = require_user(session)
    since_ms = _since_ms(body.since)
    s = compute_stats(uid, since_ms)
    findings = diagnose(uid, since_ms, s)
    if s["n"] == 0:
        raise HTTPException(400, "거래 데이터 없음 — 먼저 동기화하세요")

    q = "SELECT provider, key_enc, model FROM llm_keys WHERE user_id=?"
    args = [uid]
    if body.provider:
        q += " AND provider=?"
        args.append(body.provider)
    row = get_db().execute(q, args).fetchone()
    if not row:
        raise HTTPException(400, "LLM 키 미등록 — Claude 또는 OpenAI 토큰을 등록하세요")

    try:
        content = advisor.generate(row["provider"],
                                   security.decrypt(row["key_enc"]),
                                   row["model"], s, findings)
    except Exception as e:
        raise HTTPException(502, f"LLM 호출 실패: {str(e)[:300]}")

    db = get_db()
    db.execute(
        "INSERT INTO advice(user_id, created_at, provider, model, content) VALUES (?,?,?,?,?)",
        (uid, now_ms(), row["provider"], row["model"], content),
    )
    db.commit()
    return {"content": content, "provider": row["provider"]}


@app.get("/api/advice")
def list_advice(session: str | None = Cookie(None)):
    uid = require_user(session)
    rows = get_db().execute(
        "SELECT created_at, provider, model, content FROM advice WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (uid,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- static
@app.get("/")
def index():
    return FileResponse("static/index.html")
