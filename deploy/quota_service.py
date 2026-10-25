"""Additive quota API; the existing central SSO and English service stay running.

Policies describe the next daily grant. Balances and reservation ledgers describe
today. Never infer a permanent grant from a temporarily increased balance.
"""

import importlib
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
if __package__:
    from .english_names import EnglishNames
else:
    from english_names import EnglishNames


DEFAULTS = {
    "baby-profile": 5,
    "c-learn": 5,
    "ai-volunteer-planner": 5,
    "little-story-press": 5,
    "chess-atelier": 50,
    "eat": 5,
    "seth-english": 5,
}
GENERIC_APPS = frozenset(DEFAULTS) - {"chess-atelier", "eat", "seth-english"}
ENGLISH_APP = "seth-english"
ADMIN_ORIGINS = {"https://admin.lazylady.cc", "https://lazylady.cc"}
MAX_QUOTA = 100000


class QuotaStore:
    def __init__(self, path, clock=time.time, names=None):
        self.path, self.clock = str(path), clock
        self.names = names or (lambda: {})

    def initialize(self):
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS user_app_quota_policies (
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    app_id TEXT NOT NULL,
                    daily_quota INTEGER NOT NULL CHECK(daily_quota BETWEEN 0 AND 100000),
                    PRIMARY KEY(user_id, app_id)
                );
                CREATE TABLE IF NOT EXISTS quota_admin_audit (
                    id INTEGER PRIMARY KEY,
                    actor_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    app_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    previous_value INTEGER,
                    new_value INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_user_labels (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id),
                    label TEXT NOT NULL,
                    actor_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            # Sample the accounting day only after acquiring the writer lock.
            instant = datetime.fromtimestamp(self.clock(), timezone.utc)
            yield db, instant
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def actor(db, claims, instant, *, admin=False):
        uid = claims.get("user_id")
        expiry = claims.get("exp")
        if (type(uid) is not int or uid < 1 or type(expiry) not in (int, float)
                or expiry <= instant.timestamp()):
            raise HTTPException(401, "登录已失效，请重新登录。")
        row = db.execute("SELECT id,username,is_admin FROM users WHERE id=?", (uid,)).fetchone()
        if row is None or row["username"] != claims.get("sub"):
            raise HTTPException(401, "账号已失效，请重新登录。")
        # JWT administrator flags can outlive a revocation; trust the live row.
        if admin and row["id"] != 1 and not row["is_admin"]:
            raise HTTPException(403, "仅管理员可以管理额度。")
        return uid

    @staticmethod
    def managed(app_id):
        if app_id not in DEFAULTS:
            raise HTTPException(404, "该应用尚未接入额度管理。")

    @staticmethod
    def permission(db, user_id, app_id):
        rows = db.execute(
            "SELECT rowid,* FROM user_app_permissions WHERE user_id=? AND app_id=?",
            (user_id, app_id),
        ).fetchall()
        if not rows:
            raise HTTPException(404, "用户未获得该应用的使用权限。")
        if len(rows) != 1:
            raise HTTPException(503, "应用权限记录重复，请联系管理员处理。")
        return rows[0]

    @staticmethod
    def policy(db, user_id, app_id):
        row = db.execute(
            "SELECT daily_quota FROM user_app_quota_policies WHERE user_id=? AND app_id=?",
            (user_id, app_id),
        ).fetchone()
        return row[0] if row else None

    def daily(self, db, user_id, app_id):
        value = self.policy(db, user_id, app_id)
        return DEFAULTS[app_id] if value is None else value

    @staticmethod
    def _write_balance(db, user_id, app_id, balance, day):
        if app_id == "eat":
            # The kitchen owns guarded reservation writes. Follow its transaction
            # contract rather than bypassing or removing its protective triggers.
            db.execute("UPDATE eat_bridge_write_guard SET authorized=1 WHERE singleton=1")
            db.execute(
                "UPDATE user_app_permissions SET last_reset_date=NULL WHERE user_id=? AND app_id=?",
                (user_id, app_id),
            )
        db.execute(
            "UPDATE user_app_permissions SET ai_credits_balance=?,last_reset_date=? "
            "WHERE user_id=? AND app_id=?",
            (balance, day, user_id, app_id),
        )
        if app_id == "eat":
            db.execute("UPDATE eat_bridge_write_guard SET authorized=0 WHERE singleton=1")

    def normalize(self, db, user_id, app_id, instant):
        self.managed(app_id)
        row = self.permission(db, user_id, app_id)
        day = instant.date().isoformat()
        daily = self.daily(db, user_id, app_id)
        reset = str(row["last_reset_date"]) != day
        balance = daily if reset else row["ai_credits_balance"]
        if type(balance) is not int or balance < 0:
            raise HTTPException(503, "应用余额异常，请联系管理员处理。")
        if reset:
            self._write_balance(db, user_id, app_id, balance, day)
        if app_id == "chess-atelier":
            snapshot = db.execute(
                "SELECT reset_date FROM chess_daily_quotas WHERE user_id=?", (user_id,)
            ).fetchone()
            if reset or not snapshot or snapshot[0] != day:
                db.execute(
                    "INSERT INTO chess_daily_quotas(user_id,reset_date,daily_limit) VALUES(?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET reset_date=excluded.reset_date,"
                    "daily_limit=excluded.daily_limit", (user_id, day, daily),
                )
            expired = db.execute(
                "SELECT reset_date FROM chess_quota_requests "
                "WHERE user_id=? AND state='reserved' AND expires_at<=?",
                (user_id, instant.timestamp()),
            ).fetchall()
            db.execute(
                "UPDATE chess_quota_requests SET state='refunded',closed_at=? "
                "WHERE user_id=? AND state='reserved' AND expires_at<=?",
                (int(instant.timestamp()), user_id, instant.timestamp()),
            )
            used = self.chess_used(db, user_id, day)
            limit = db.execute(
                "SELECT daily_limit FROM chess_daily_quotas WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            balance = min(balance + sum(r[0] == day for r in expired), max(0, limit - used))
            # Report spendable credits, including the independent trusted cap.
            self._write_balance(db, user_id, app_id, balance, day)
        elif app_id == ENGLISH_APP:
            # Match the English reserve/commit/refund contract under the same
            # SQLite writer lock. Yesterday's reservations never refill today.
            expired = db.execute(
                "SELECT reset_date FROM english_quota_ledger WHERE central_user_id=? "
                "AND app_id=? AND state='reserved' AND expires_at<=?",
                (user_id, app_id, instant.timestamp()),
            ).fetchall()
            db.execute(
                "UPDATE english_quota_ledger SET state='refunded',closed_at=? "
                "WHERE central_user_id=? AND app_id=? AND state='reserved' AND expires_at<=?",
                (int(instant.timestamp()), user_id, app_id, instant.timestamp()),
            )
            refund = sum(r[0] == day for r in expired)
            if refund:
                balance += refund
                self._write_balance(db, user_id, app_id, balance, day)
        elif app_id == "eat":
            expired = db.execute(
                "SELECT charged_date FROM eat_bridge_reservations "
                "WHERE user_id=? AND state='reserved' AND expires_at<=?",
                (str(user_id), instant.timestamp()),
            ).fetchall()
            db.execute(
                "UPDATE eat_bridge_reservations SET state='expired' "
                "WHERE user_id=? AND state='reserved' AND expires_at<=?",
                (str(user_id), instant.timestamp()),
            )
            refund = sum(r[0] == day for r in expired)
            if refund:
                balance += refund
                self._write_balance(db, user_id, app_id, balance, day)
        return balance

    @staticmethod
    def chess_used(db, user_id, day):
        return db.execute(
            "SELECT COUNT(*) FROM chess_quota_requests WHERE user_id=? AND reset_date=? "
            "AND state IN ('reserved','committed')", (user_id, day),
        ).fetchone()[0]

    def describe(self, db, user_id, app_id, balance):
        row = self.permission(db, user_id, app_id)
        managed = app_id in DEFAULTS
        override = self.policy(db, user_id, app_id) if managed else None
        default = DEFAULTS.get(app_id, 5)
        return {
            "app_id": app_id,
            "quota": balance,
            "daily_quota": default if override is None else override,
            "daily_quota_override": override,
            "default_daily_quota": default,
            "quota_reset_date": row["last_reset_date"],
            "quota_managed": managed,
        }

    def users(self, claims):
        with self.transaction() as (db, instant):
            self.actor(db, claims, instant, admin=True)
            names = self.names()
            profiles = getattr(self.names, "account_profiles", lambda: {})()
            result = []
            for user in db.execute("SELECT id,username,role,is_admin,created_at FROM users ORDER BY id"):
                apps = []
                for row in db.execute(
                    "SELECT app_id,ai_credits_balance FROM user_app_permissions WHERE user_id=? ORDER BY app_id",
                    (user["id"],),
                ).fetchall():
                    app_id = row["app_id"]
                    balance = (self.normalize(db, user["id"], app_id, instant)
                               if app_id in DEFAULTS else row["ai_credits_balance"])
                    apps.append(self.describe(db, user["id"], app_id, balance))
                label = db.execute("SELECT label FROM admin_user_labels WHERE user_id=?", (user["id"],)).fetchone()
                result.append({**dict(user), "is_admin": bool(user["is_admin"]), "apps": apps,
                               "admin_label": label[0] if label else "",
                               "learner_names": names.get(user["id"], []),
                               "account_profile": profiles.get(user["id"], {"nickname": "", "avatar": ""})})
            return result

    def label(self, claims, user_id, label):
        if not isinstance(label, str) or len(label.strip()) > 48 or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise HTTPException(422, "备注须为不超过48字的单行文字。")
        with self.transaction() as (db, instant):
            actor_id = self.actor(db, claims, instant, admin=True)
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise HTTPException(404, "用户不存在。")
            db.execute(
                "INSERT INTO admin_user_labels(user_id,label,actor_id,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET label=excluded.label,actor_id=excluded.actor_id,updated_at=excluded.updated_at",
                (user_id, label.strip(), actor_id, instant.isoformat()),
            )
        return {"status": "success", "admin_label": label.strip()}

    def update(self, claims, user_id, app_id, action, value):
        self.managed(app_id)
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_QUOTA):
            raise HTTPException(422, "额度须为0–100000的整数。")
        if action not in {"daily", "remaining"} or (action == "remaining" and value is None):
            raise HTTPException(422, "额度操作无效。")
        with self.transaction() as (db, instant):
            actor_id = self.actor(db, claims, instant, admin=True)
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise HTTPException(404, "用户不存在。")
            # Settle today's OLD policy first. A new policy must not erase usage,
            # reduce today's chess authorization, or reset a just-added balance.
            balance = self.normalize(db, user_id, app_id, instant)
            old = self.policy(db, user_id, app_id) if action == "daily" else balance
            if action == "daily":
                if value is None:
                    db.execute("DELETE FROM user_app_quota_policies WHERE user_id=? AND app_id=?", (user_id, app_id))
                else:
                    db.execute(
                        "INSERT INTO user_app_quota_policies(user_id,app_id,daily_quota) VALUES(?,?,?) "
                        "ON CONFLICT(user_id,app_id) DO UPDATE SET daily_quota=excluded.daily_quota",
                        (user_id, app_id, value),
                    )
            else:
                balance = value
                day = instant.date().isoformat()
                if app_id == "chess-atelier":
                    db.execute(
                        "UPDATE chess_daily_quotas SET daily_limit=? WHERE user_id=?",
                        (self.chess_used(db, user_id, day) + value, user_id),
                    )
                self._write_balance(db, user_id, app_id, value, day)
            db.execute(
                "INSERT INTO quota_admin_audit(actor_id,user_id,app_id,action,previous_value,new_value,created_at) "
                "VALUES(?,?,?,?,?,?,?)", (actor_id, user_id, app_id, action, old, value, instant.isoformat()),
            )
            return {
                "status": "success", "user_id": user_id, "app_id": app_id, "new_quota": balance,
                "app": self.describe(db, user_id, app_id, balance),
                "effective_from": (instant.date() + timedelta(days=1)).isoformat() if action == "daily" else instant.date().isoformat(),
            }

    def consume(self, claims, app_id, cost):
        if app_id == ENGLISH_APP:
            raise HTTPException(403, "请使用英语专用额度流程。")
        if app_id not in GENERIC_APPS:
            raise HTTPException(403, "请使用该应用专用的额度流程。")
        if type(cost) is not int or not 0 <= cost <= MAX_QUOTA:
            raise HTTPException(422, "扣费次数须为非负整数。")
        with self.transaction() as (db, instant):
            user_id = self.actor(db, claims, instant)
            exists = db.execute(
                "SELECT 1 FROM user_app_permissions WHERE user_id=? AND app_id=?", (user_id, app_id)
            ).fetchone()
            if not exists:
                # Retain first-use provisioning for the four legacy products only.
                db.execute(
                    "INSERT INTO user_app_permissions(user_id,app_id,ai_credits_balance,last_reset_date) VALUES(?,?,?,?)",
                    (user_id, app_id, self.daily(db, user_id, app_id), instant.date().isoformat()),
                )
            balance = self.normalize(db, user_id, app_id, instant)
            insufficient = balance < cost
            if not insufficient and cost:
                balance -= cost
                self._write_balance(db, user_id, app_id, balance, instant.date().isoformat())
        # Even a denied request commits the daily reset, never an old-day balance.
        if insufficient:
            raise HTTPException(402, "Insufficient AI credits for this product")
        return {"status": "success", "remaining_credits": balance}


class RemainingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quota: int = Field(strict=True, ge=0, le=MAX_QUOTA)


class DailyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_quota: int | None = Field(strict=True, ge=0, le=MAX_QUOTA)


class ConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appId: str = Field(min_length=1, max_length=128)
    cost: int = Field(default=1, strict=True, ge=0, le=MAX_QUOTA)


class LabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(strict=True, max_length=48)


def create_app(central=None, store=None):
    central = central if central is not None else importlib.import_module("main")
    store = store or QuotaStore(os.environ.get("QUOTA_DB_PATH", "/opt/baby-profile/sso-service/sso.db"), names=EnglishNames())
    store.initialize()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(
        CORSMiddleware, allow_origin_regex=r"https://(?:[a-z0-9-]+\.)?lazylady\.cc",
        allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"],
    )
    bearer = HTTPBearer(auto_error=False)

    def claims(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(401, "请先登录。")
        try:
            value = central.get_current_user(token=credentials.credentials)
        except Exception:
            raise HTTPException(401, "登录已失效，请重新登录。") from None
        if not isinstance(value, dict):
            raise HTTPException(401, "登录凭证无效。")
        return value

    def admin_claims(value: dict = Depends(claims), origin: str | None = Header(default=None)):
        if origin is not None and origin not in ADMIN_ORIGINS:
            raise HTTPException(403, "请求来源未获授权。")
        return value

    @app.middleware("http")
    async def private_response(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(sqlite3.Error)
    async def unavailable(_request, _error):
        return JSONResponse({"detail": "额度服务暂时不可用，请稍后重试。"}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        # Do not echo input or bearer credentials in validation diagnostics.
        return JSONResponse({"detail": "额度须为0–100000的整数，请检查请求。"}, status_code=422)

    @app.get("/health")
    def health():
        return {"status": "ok", "managed_apps": sorted(DEFAULTS), "english_managed": True}

    @app.get("/admin/quotas/users")
    def users(value: dict = Depends(admin_claims)):
        return store.users(value)

    @app.post("/admin/quotas/users/{user_id}/label")
    def label(user_id: int, payload: LabelRequest, value: dict = Depends(admin_claims)):
        return store.label(value, user_id, payload.label)

    @app.post("/admin/quotas/users/{user_id}/apps/{app_id}/daily")
    def daily(user_id: int, app_id: str, payload: DailyRequest, value: dict = Depends(admin_claims)):
        return store.update(value, user_id, app_id, "daily", payload.daily_quota)

    @app.post("/admin/quotas/users/{user_id}/apps/{app_id}/remaining")
    @app.post("/sso/auth/users/{user_id}/apps/{app_id}/quota")
    def remaining(user_id: int, app_id: str, payload: RemainingRequest, value: dict = Depends(admin_claims)):
        return store.update(value, user_id, app_id, "remaining", payload.quota)

    @app.post("/api/quota/consume")
    def consume(payload: ConsumeRequest, value: dict = Depends(claims)):
        return store.consume(value, payload.appId, payload.cost)

    return app
