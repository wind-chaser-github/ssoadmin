"""Chess-only central identity/quota bridge; never changes another product."""
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager, asynccontextmanager, closing
from datetime import datetime, timezone

APP_ID = "chess-atelier"
DAILY_LIMIT = 50
MAX_REMAINING = 100000
TTL = 180
ADMIN_ORIGINS = ("https://admin.lazylady.cc", "https://lazylady.cc")


class QuotaError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


class QuotaStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock

    def initialize(self):
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS chess_quota_requests (
              user_id INTEGER NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL,
              reset_date TEXT NOT NULL, created_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL, closed_at INTEGER,
              PRIMARY KEY (user_id, request_id), FOREIGN KEY(user_id) REFERENCES users(id))""")
            db.execute("CREATE INDEX IF NOT EXISTS chess_quota_expiry ON chess_quota_requests(user_id,state,expires_at)")
            db.execute("CREATE INDEX IF NOT EXISTS chess_quota_closed ON chess_quota_requests(closed_at)")
            # Persist today's trusted authorization independently of the policy
            # used for the next reset. Legacy balance edits cannot grant budget.
            db.execute("""CREATE TABLE IF NOT EXISTS chess_daily_quotas (
              user_id INTEGER PRIMARY KEY, reset_date TEXT NOT NULL,
              daily_limit INTEGER NOT NULL CHECK(daily_limit>=0),
              FOREIGN KEY(user_id) REFERENCES users(id))""")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _configured_daily_quota(self, db, user_id):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_app_quota_policies'").fetchone():
            return DAILY_LIMIT
        row = db.execute("SELECT daily_quota FROM user_app_quota_policies WHERE user_id=? AND app_id=?", (user_id, APP_ID)).fetchone()
        if row is None:
            return DAILY_LIMIT
        value = row["daily_quota"]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_REMAINING:
            raise QuotaError(503, "棋牌每日额度配置异常，请联系管理员。")
        return value

    def _permission(self, db, user_id, day):
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1 or not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise QuotaError(401, "统一账号已失效，请重新登录。")
        rows = db.execute("SELECT rowid,ai_credits_balance,last_reset_date FROM user_app_permissions WHERE user_id=? AND app_id=?", (user_id, APP_ID)).fetchall()
        if len(rows) > 1:
            raise QuotaError(503, "棋牌额度配置异常，请联系管理员。")
        daily_quota = self._configured_daily_quota(db, user_id)
        new_day = not rows or str(rows[0]["last_reset_date"]) != day
        if not rows:
            db.execute("INSERT INTO user_app_permissions(user_id,app_id,ai_credits_balance,last_reset_date) VALUES(?,?,?,?)", (user_id, APP_ID, daily_quota, day))
        elif new_day:
            db.execute("UPDATE user_app_permissions SET ai_credits_balance=?,last_reset_date=? WHERE rowid=?", (daily_quota, day, rows[0]["rowid"]))
        snapshot = db.execute("SELECT reset_date FROM chess_daily_quotas WHERE user_id=?", (user_id,)).fetchone()
        if new_day or snapshot is None or snapshot["reset_date"] != day:
            # Freeze the baseline before policy edits; only the administrator's
            # remaining setter may alter today's total authorization afterward.
            db.execute("""INSERT INTO chess_daily_quotas(user_id,reset_date,daily_limit)
              VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
              reset_date=excluded.reset_date,daily_limit=excluded.daily_limit""", (user_id, day, daily_quota))
        balance = db.execute("SELECT ai_credits_balance FROM user_app_permissions WHERE user_id=? AND app_id=?", (user_id, APP_ID)).fetchone()[0]
        if not isinstance(balance, int) or balance < 0:
            raise QuotaError(503, "棋牌额度配置异常，请联系管理员。")
        return balance

    def _prepare(self, db, user_id, now, day):
        balance = self._permission(db, user_id, day)
        rows = db.execute("SELECT request_id,reset_date FROM chess_quota_requests WHERE user_id=? AND state='reserved' AND expires_at<=?", (user_id, now)).fetchall()
        refund = sum(row["reset_date"] == day for row in rows)
        db.execute("UPDATE chess_quota_requests SET state='refunded',closed_at=? WHERE user_id=? AND state='reserved' AND expires_at<=?", (now, user_id, now))
        if refund:
            balance = min(balance + refund, max(0, self._daily_limit(db, user_id, day) - self._used(db, user_id, day)))
            db.execute("UPDATE user_app_permissions SET ai_credits_balance=? WHERE user_id=? AND app_id=?", (balance, user_id, APP_ID))
        # Bounded cleanup touches only this bridge's closed accounting records.
        db.execute("DELETE FROM chess_quota_requests WHERE rowid IN (SELECT rowid FROM chess_quota_requests WHERE closed_at<? LIMIT 100)", (now - 30 * 86400,))
        return balance

    def _daily_limit(self, db, user_id, day):
        row = db.execute("SELECT reset_date,daily_limit FROM chess_daily_quotas WHERE user_id=?", (user_id,)).fetchone()
        value = row["daily_limit"] if row and row["reset_date"] == day else DAILY_LIMIT
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise QuotaError(503, "棋牌额度配置异常，请联系管理员。")
        return value

    def _used(self, db, user_id, day):
        return db.execute("SELECT COUNT(*) FROM chess_quota_requests WHERE user_id=? AND reset_date=? AND state IN ('reserved','committed')", (user_id, day)).fetchone()[0]

    def _available(self, db, user_id, day, balance):
        # The older central consume endpoint accepts arbitrary product/cost.
        # Its balance edits must never bypass this product's daily ceiling.
        return min(balance, max(0, self._daily_limit(db, user_id, day) - self._used(db, user_id, day)))

    def set_remaining(self, actor_id, user_id, quota):
        if not isinstance(quota, int) or isinstance(quota, bool) or not 0 <= quota <= MAX_REMAINING:
            raise QuotaError(400, "当日剩余额度须为 0–100000 的整数。")
        with self.transaction() as db:
            if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id < 1:
                raise QuotaError(401, "统一账号已失效，请重新登录。")
            actor = db.execute("SELECT id,is_admin FROM users WHERE id=?", (actor_id,)).fetchone()
            if not actor:
                raise QuotaError(401, "统一账号已失效，请重新登录。")
            # Recheck permissions under the write lock; JWT admin claims may be
            # stale or forged even when their account identity remains valid.
            if actor["id"] != 1 and not actor["is_admin"]:
                raise QuotaError(403, "仅管理员可设置棋牌额度。")
            if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1 or not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise QuotaError(404, "指定统一账号不存在。")
            now = int(self.clock())
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            self._prepare(db, user_id, now, day)
            daily_limit = self._used(db, user_id, day) + quota
            db.execute("""INSERT INTO chess_daily_quotas(user_id,reset_date,daily_limit)
              VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
              reset_date=excluded.reset_date,daily_limit=excluded.daily_limit""", (user_id, day, daily_limit))
            db.execute("UPDATE user_app_permissions SET ai_credits_balance=?,last_reset_date=? WHERE user_id=? AND app_id=?", (quota, day, user_id, APP_ID))
        return {"remaining_credits": quota, "reset_date": day, "daily_limit": daily_limit}

    def status(self, user_id):
        with self.transaction() as db:
            # Use the day at lock acquisition, not before waiting for another
            # writer across midnight (which could undo a new-day admin grant).
            now = int(self.clock())
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            balance = self._prepare(db, user_id, now, day)
            remaining = self._available(db, user_id, day, balance)
            daily_limit = self._daily_limit(db, user_id, day)
        return {"remaining_credits": remaining, "reset_date": day, "daily_limit": daily_limit}

    def mutate(self, user_id, action, request_id):
        try:
            parsed = uuid.UUID(request_id)
            if str(parsed) != request_id or parsed.version != 4:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise QuotaError(400, "额度请求编号无效。")
        if action not in {"reserve", "commit", "refund"}:
            raise QuotaError(400, "额度操作无效。")
        # Expiry reconciliation must commit even when a later commit is rejected.
        self.status(user_id)
        with self.transaction() as db:
            now = int(self.clock())
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            balance = self._prepare(db, user_id, now, day)
            row = db.execute("SELECT * FROM chess_quota_requests WHERE user_id=? AND request_id=?", (user_id, request_id)).fetchone()
            if action == "reserve":
                if row:
                    state = row["state"]
                else:
                    if self._available(db, user_id, day, balance) < 1:
                        raise QuotaError(402, "今天的 AI 额度已用完，明天再来玩吧。")
                    balance -= 1
                    db.execute("UPDATE user_app_permissions SET ai_credits_balance=? WHERE user_id=? AND app_id=?", (balance, user_id, APP_ID))
                    db.execute("INSERT INTO chess_quota_requests VALUES(?,?, 'reserved',?,?,?,NULL)", (user_id, request_id, day, now, now + TTL))
                    state = "reserved"
            else:
                if not row:
                    raise QuotaError(409, "找不到这次棋牌额度请求。")
                expected = "committed" if action == "commit" else "refunded"
                state = row["state"]
                if state != expected:
                    if state != "reserved":
                        raise QuotaError(409, "这次棋牌额度请求已经关闭。")
                    state = expected
                    db.execute("UPDATE chess_quota_requests SET state=?,closed_at=? WHERE user_id=? AND request_id=?", (state, now, user_id, request_id))
                    if action == "refund" and row["reset_date"] == day:
                        balance = min(balance + 1, max(0, self._daily_limit(db, user_id, day) - self._used(db, user_id, day)))
                        db.execute("UPDATE user_app_permissions SET ai_credits_balance=? WHERE user_id=? AND app_id=?", (balance, user_id, APP_ID))
            remaining = self._available(db, user_id, day, balance)
            daily_limit = self._daily_limit(db, user_id, day)
        return {"status": state, "request_id": request_id, "remaining_credits": remaining, "daily_limit": daily_limit, "reset_date": day}


def build_app(central=None, store=None, bridge_key=None):
    # Reuse the installed central validator inside this isolated process. No
    # central code/config is replaced and its existing ASGI process is untouched.
    from fastapi import FastAPI, Body, Header, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    if central is None:
        import main as central
    key = bridge_key or os.environ.get("CHESS_SSO_BRIDGE_KEY", "")
    if len(key) < 32:
        raise RuntimeError("Missing private chess bridge key")
    store = store or QuotaStore(os.environ.get("CHESS_SSO_DATABASE", "/opt/baby-profile/sso-service/sso.db"))

    @asynccontextmanager
    async def lifespan(_):
        store.initialize()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=list(ADMIN_ORIGINS),
                       allow_methods=["POST"], allow_headers=["Authorization", "Content-Type"])

    def trusted(value):
        if not isinstance(value, str) or not secrets.compare_digest(key, value):
            raise HTTPException(403, "请求来源未获授权。")

    def identity(token):
        if not isinstance(token, str) or not 20 <= len(token) <= 8192:
            raise HTTPException(401, "统一登录凭据无效。")
        try:
            payload = central.jwt.decode(token, central.SECRET_KEY, algorithms=[central.ALGORITHM])
            user_id = payload.get("user_id")
            if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1 or not isinstance(payload.get("exp"), int) or payload["exp"] <= time.time():
                raise ValueError()
            with closing(sqlite3.connect(store.path, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                user = db.execute("SELECT id,username,role,is_admin FROM users WHERE id=?", (user_id,)).fetchone()
                if not user or user["username"] != payload.get("sub"):
                    raise ValueError()
                return {"user_id": user_id, "sub": user["username"], "role": user["role"], "is_admin": bool(user["is_admin"]), "exp": payload["exp"], "apps": [APP_ID]}
        except sqlite3.Error:
            raise HTTPException(503, "统一账号服务暂时不可用。")
        except Exception:
            raise HTTPException(401, "统一账号已失效，请重新登录。")

    def bearer(value):
        if not isinstance(value, str) or not value.startswith("Bearer "):
            raise HTTPException(401, "请先登录。")
        return identity(value[7:])

    def execute(operation):
        try:
            return operation()
        except QuotaError as cause:
            raise HTTPException(cause.status, cause.detail)
        except sqlite3.Error:
            raise HTTPException(503, "棋牌额度服务暂时不可用。")

    @app.get("/health")
    def health(x_chess_bridge: str | None = Header(default=None)):
        trusted(x_chess_bridge)
        return {"status": "ok", "appId": APP_ID, "daily_limit": DAILY_LIMIT}

    @app.post("/session/verify")
    def verify(payload: dict = Body(...), x_chess_bridge: str | None = Header(default=None)):
        trusted(x_chess_bridge)
        user = identity(payload.get("token"))
        execute(lambda: store.status(user["user_id"]))
        return {"valid": True, "payload": user}

    @app.get("/quota/status")
    def status(authorization: str | None = Header(default=None), x_chess_bridge: str | None = Header(default=None)):
        trusted(x_chess_bridge)
        user = bearer(authorization)
        return execute(lambda: store.status(user["user_id"]))

    @app.post("/quota/{action}")
    def mutate(action: str, payload: dict = Body(...), authorization: str | None = Header(default=None), x_chess_bridge: str | None = Header(default=None)):
        trusted(x_chess_bridge)
        user = bearer(authorization)
        if payload.get("appId") != APP_ID:
            raise HTTPException(400, "棋牌产品标识无效。")
        return execute(lambda: store.mutate(user["user_id"], action, payload.get("requestId")))

    @app.post("/sso/auth/users/{user_id}/apps/chess-atelier/quota")
    def admin_quota(user_id: int, payload: dict = Body(...), authorization: str | None = Header(default=None), origin: str | None = Header(default=None)):
        if origin is not None and origin not in ADMIN_ORIGINS:
            raise HTTPException(403, "请求来源未获授权。")
        user = bearer(authorization)
        result = execute(lambda: store.set_remaining(user["user_id"], user_id, payload.get("quota")))
        return {"status": "success", "user_id": user_id, "app_id": APP_ID,
                "new_quota": result["remaining_credits"], **result}

    return app
