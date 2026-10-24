"""Eat-only identity and credit reservations; legacy SSO source remains read-only.

The central JWT proves identity only. Every action reloads the user and product
permission inside the same SQLite transaction that changes the credit ledger.
"""

import hashlib
import importlib
import inspect
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

APP_ID = "eat"
DEFAULT_DAILY_CREDITS = 5
RESERVATION_TTL = 900


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class TokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=8192)


class QuotaRequest(TokenRequest):
    request_id: UUID


class SettlementRequest(BaseModel):
    subject: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    request_id: UUID


class EatBridge:
    def __init__(
        self,
        server_module: Any,
        bridge_key: str,
        *,
        engine: Any | None = None,
        now: Callable[[], datetime] | None = None,
        reservation_ttl: int = RESERVATION_TTL,
        token_lifetime_seconds: int = 3600,
    ):
        if len(bridge_key) < 32:
            raise ValueError("Eat bridge key must contain at least 32 characters")
        if not 1 <= reservation_ttl <= RESERVATION_TTL or not 1 <= token_lifetime_seconds <= 86400:
            raise ValueError("Invalid bounded bridge lifetime")
        clock = now or (lambda: datetime.now(UTC))
        engine = engine if engine is not None else server_module.engine

        self.server_module = server_module
        self.bridge_key = bridge_key
        self.engine = engine
        self.clock = clock
        self.reservation_ttl = reservation_ttl
        self.token_lifetime_seconds = token_lifetime_seconds
        self.attempt_lock = threading.Lock()
        self.login_attempts: dict[str, deque[float]] = {}
        self.global_attempts: deque[float] = deque()

        with engine.begin() as connection:
            connection.execute(
                text("""
                CREATE TABLE IF NOT EXISTS eat_bridge_write_guard (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    authorized INTEGER NOT NULL CHECK(authorized IN (0,1))
                )
            """)
            )
            connection.execute(text("INSERT OR IGNORE INTO eat_bridge_write_guard VALUES (1,0)"))
            connection.execute(text("UPDATE eat_bridge_write_guard SET authorized=0 WHERE singleton=1"))

            connection.execute(
                text("""
                CREATE TABLE IF NOT EXISTS eat_bridge_accounts (
                    user_id TEXT PRIMARY KEY, first_granted_at REAL NOT NULL,
                    identity_created_at TEXT NOT NULL
                )
            """)
            )
            connection.execute(
                text("""
                CREATE TABLE IF NOT EXISTS eat_bridge_reservations (
                    user_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','committed','refunded','expired')),
                    charged_date TEXT NOT NULL, expires_at REAL NOT NULL,
                    PRIMARY KEY (user_id, request_id)
                )
            """)
            )
            connection.execute(
                text("""
                CREATE INDEX IF NOT EXISTS eat_bridge_expiry
                ON eat_bridge_reservations(user_id, state, expires_at)
            """)
            )

            connection.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS eat_bridge_admin_daily_reset
                AFTER UPDATE OF ai_credits_balance ON user_app_permissions
                WHEN NEW.app_id='eat' AND NEW.last_reset_date IS OLD.last_reset_date
                BEGIN
                    UPDATE user_app_permissions SET last_reset_date=date('now') WHERE rowid=NEW.rowid;
                END
            """)
            )
            connection.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS eat_bridge_revoke_reservations
                AFTER DELETE ON user_app_permissions WHEN OLD.app_id='eat'
                BEGIN
                    UPDATE eat_bridge_reservations SET state='expired'
                    WHERE user_id=CAST(OLD.user_id AS TEXT) AND state='reserved';
                END
            """)
            )

            connection.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS eat_bridge_guard_legacy_grants
                BEFORE INSERT ON user_app_permissions
                WHEN NEW.app_id='eat' AND NEW.last_reset_date IS NOT NULL
                     AND COALESCE((SELECT authorized FROM eat_bridge_write_guard WHERE singleton=1),0) != 1
                BEGIN
                    SELECT RAISE(ABORT,'Eat grants require the private bridge');
                END
            """)
            )
            connection.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS eat_bridge_guard_legacy_consumption
                BEFORE UPDATE OF last_reset_date ON user_app_permissions
                WHEN NEW.app_id='eat' AND NEW.ai_credits_balance IS NOT OLD.ai_credits_balance
                     AND COALESCE((SELECT authorized FROM eat_bridge_write_guard WHERE singleton=1),0) != 1
                BEGIN
                    SELECT RAISE(ABORT,'Eat credits require the private bridge');
                END
            """)
            )

    def require_key(self, x_eat_bridge_key: str | None = Header(default=None)) -> None:
        if x_eat_bridge_key is None or not secrets.compare_digest(x_eat_bridge_key.encode(), self.bridge_key.encode()):
            raise HTTPException(403, "Invalid eat bridge credential")

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.engine.connect() as connection:
            try:
                connection.exec_driver_sql("PRAGMA busy_timeout=2000")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                # SQLite serializes writers. Other processes only ever observe the
                # committed zero; a crash or exception rolls this private one back.
                connection.exec_driver_sql("UPDATE eat_bridge_write_guard SET authorized=1 WHERE singleton=1")
                yield connection
                connection.exec_driver_sql("UPDATE eat_bridge_write_guard SET authorized=0 WHERE singleton=1")
                connection.commit()
            except OperationalError as error:
                connection.rollback()
                raise HTTPException(503, "Identity credit service is busy") from error
            except BaseException:
                connection.rollback()
                raise

    def execute(self, connection: Any, sql: str, **parameters: Any) -> Any:
        return connection.execute(text(sql), parameters)

    def timestamp(self) -> datetime:
        value = self.clock()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def fresh_user(self, connection: Any, subject: Any, instant: datetime) -> dict[str, Any]:
        user = self.execute(connection, "SELECT * FROM users WHERE id=:id", id=subject).mappings().first()
        if user is None:
            raise HTTPException(401, "Central identity is unavailable")
        result = dict(user)
        created_at = result.get("created_at")
        if created_at is None:
            raise HTTPException(403, "Central identity has no stable creation record")
        marker = (
            self.execute(
                connection, "SELECT identity_created_at FROM eat_bridge_accounts WHERE user_id=:uid", uid=str(subject)
            )
            .mappings()
            .first()
        )
        if marker is not None and marker["identity_created_at"] != str(created_at):
            # SQLite may reuse a deleted integer ID; never adopt the replacement user.
            raise HTTPException(403, "Central identity generation has changed")
        expires = result.get("expires_at")
        if expires:
            expiry = expires if isinstance(expires, datetime) else datetime.fromisoformat(str(expires))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry <= instant:
                raise HTTPException(401, "Central identity has expired")
        return result

    async def authenticate(self, token: str) -> tuple[Any, int]:
        try:
            payload = self.server_module.get_current_user(token)
            if inspect.isawaitable(payload):
                payload = await payload
            if not isinstance(payload, dict) or payload.get("user_id") is None:
                raise ValueError("Invalid identity")
            expiry = int(payload["exp"])
            if expiry <= self.timestamp().timestamp():
                raise ValueError("Expired identity")
            return payload["user_id"], expiry
        except Exception as error:
            raise HTTPException(401, "Invalid central session") from error

    def check_session_expiry(self, expiry: int, instant: datetime) -> None:
        if expiry <= instant.timestamp():
            raise HTTPException(401, "Central session expired while awaiting the credit transaction")

    def configured_daily_quota(self, connection: Any, user_id: Any) -> int:
        present = self.execute(
            connection,
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_app_quota_policies'",
        ).first()
        if present is None:
            return DEFAULT_DAILY_CREDITS
        row = self.execute(
            connection,
            "SELECT daily_quota FROM user_app_quota_policies WHERE user_id=:uid AND app_id=:app",
            uid=user_id,
            app=APP_ID,
        ).mappings().first()
        if row is None:
            return DEFAULT_DAILY_CREDITS
        value = row["daily_quota"]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100000:
            raise HTTPException(503, "Invalid eat daily quota requires administrator repair")
        return value

    def permission(
        self, connection: Any, user_id: Any, instant: datetime, *, provision: bool = False
    ) -> dict[str, Any]:
        row = (
            self.execute(
                connection,
                "SELECT * FROM user_app_permissions WHERE user_id=:uid AND app_id=:app",
                uid=user_id,
                app=APP_ID,
            )
            .mappings()
            .all()
        )
        if len(row) > 1:
            # The legacy table has no uniqueness constraint. Ambiguous grants fail closed.
            raise HTTPException(503, "Duplicate eat permission records require administrator repair")
        row = row[0] if row else None
        marker = self.execute(
            connection, "SELECT 1 FROM eat_bridge_accounts WHERE user_id=:uid", uid=str(user_id)
        ).first()
        day = instant.date().isoformat()
        daily_quota = self.configured_daily_quota(connection, user_id)
        if row is None:
            if not provision or marker is not None:
                raise HTTPException(403, "Eat access has been revoked or is unavailable")
            self.execute(
                connection,
                """INSERT INTO user_app_permissions (user_id,app_id,ai_credits_balance,last_reset_date)
                   VALUES (:uid,:app,:balance,:day)""",
                uid=user_id,
                app=APP_ID,
                balance=daily_quota,
                day=day,
            )
            balance = daily_quota
        else:
            balance = int(row["ai_credits_balance"])
            if str(row["last_reset_date"]) != day:
                balance = daily_quota
                self.execute(
                    connection,
                    """UPDATE user_app_permissions SET ai_credits_balance=:balance,last_reset_date=:day
                       WHERE user_id=:uid AND app_id=:app""",
                    balance=balance,
                    day=day,
                    uid=user_id,
                    app=APP_ID,
                )
        # Recording existing grants too prevents login from resurrecting later revocations.
        if marker is None:
            self.execute(
                connection,
                """INSERT INTO eat_bridge_accounts(user_id,first_granted_at,identity_created_at)
                   VALUES (:uid,:stamp,:created)""",
                uid=str(user_id),
                stamp=instant.timestamp(),
                created=str(self.fresh_user(connection, user_id, instant)["created_at"]),
            )
        expired = (
            self.execute(
                connection,
                """SELECT request_id,charged_date FROM eat_bridge_reservations
               WHERE user_id=:uid AND state='reserved' AND expires_at<=:stamp""",
                uid=str(user_id),
                stamp=instant.timestamp(),
            )
            .mappings()
            .all()
        )
        refund = sum(row["charged_date"] == day for row in expired)
        if expired:
            self.execute(
                connection,
                """UPDATE eat_bridge_reservations SET state='expired'
                   WHERE user_id=:uid AND state='reserved' AND expires_at<=:stamp""",
                uid=str(user_id),
                stamp=instant.timestamp(),
            )
        if refund:
            balance += refund
            self.set_balance(connection, user_id, balance, day)
        return {
            "remaining_credits": balance,
            "reset_date": day,
            # Keep the existing response contract: this is the product default,
            # while per-user policy only funds first grants and UTC resets.
            "default_daily_credits": DEFAULT_DAILY_CREDITS,
            "app_id": APP_ID,
        }

    def set_balance(self, connection: Any, user_id: Any, balance: int, day: str) -> None:
        # The legacy admin edits only the balance. Its scoped trigger stamps today.
        # Mark bridge updates explicitly so injected clocks and daily resets stay atomic.
        self.execute(
            connection,
            "UPDATE user_app_permissions SET last_reset_date=NULL WHERE user_id=:uid AND app_id=:app",
            uid=user_id,
            app=APP_ID,
        )
        self.execute(
            connection,
            """UPDATE user_app_permissions SET ai_credits_balance=:balance,last_reset_date=:day
               WHERE user_id=:uid AND app_id=:app""",
            balance=balance,
            day=day,
            uid=user_id,
            app=APP_ID,
        )

    def limit_login(self, username: str) -> None:
        instant = time.monotonic()
        key = hashlib.sha256(username.encode()).hexdigest()
        with self.attempt_lock:
            while self.global_attempts and self.global_attempts[0] <= instant - 60:
                self.global_attempts.popleft()
            for name, previous_attempts in list(self.login_attempts.items()):
                while previous_attempts and previous_attempts[0] <= instant - 60:
                    previous_attempts.popleft()
                if not previous_attempts:
                    del self.login_attempts[name]
            attempts = self.login_attempts.get(key)
            if len(self.global_attempts) >= 100 or (attempts is not None and len(attempts) >= 10):
                raise HTTPException(429, "Too many central login attempts; retry later")
            if attempts is None:
                attempts = self.login_attempts[key] = deque()
            attempts.append(instant)
            self.global_attempts.append(instant)

    def check_password(self, body: LoginRequest) -> dict[str, Any]:
        self.limit_login(body.username)
        with self.engine.connect() as connection:
            row = (
                self.execute(connection, "SELECT * FROM users WHERE username=:username", username=body.username)
                .mappings()
                .first()
            )
        try:
            verified = row is not None and self.server_module.verify_password(body.password, row["password_hash"])
        except (ValueError, TypeError):
            verified = False
        if not verified:
            raise HTTPException(401, "Invalid central credentials")
        return dict(row)

    def identity(self, user: dict[str, Any], expiry: int) -> dict[str, Any]:
        return {"subject": str(user["id"]), "username": user["username"], "expires_at": expiry, "app_id": APP_ID}

    async def login(self, body: LoginRequest) -> dict[str, Any]:
        checked = await run_in_threadpool(self.check_password, body)
        with self.transaction() as connection:
            instant = self.timestamp()
            user = self.fresh_user(connection, checked["id"], instant)
            if any(user[field] != checked[field] for field in ("password_hash", "username", "created_at")):
                raise HTTPException(401, "Central credentials changed; retry login")
            self.permission(connection, user["id"], instant, provision=True)
        token = self.server_module.create_access_token(
            {"user_id": user["id"], "sub": user["username"]},
            expires_delta=timedelta(seconds=self.token_lifetime_seconds),
        )
        subject, expiry = await self.authenticate(token)
        if subject != user["id"]:
            raise HTTPException(503, "Central session could not be issued")
        return {"token": token, **self.identity(user, expiry)}

    async def verify(self, body: TokenRequest) -> dict[str, Any]:
        subject, expiry = await self.authenticate(body.token)
        with self.transaction() as connection:
            instant = self.timestamp()
            self.check_session_expiry(expiry, instant)
            user = self.fresh_user(connection, subject, instant)
            self.permission(connection, subject, instant)
        return self.identity(user, expiry)

    async def status(self, body: TokenRequest) -> dict[str, Any]:
        subject, expiry = await self.authenticate(body.token)
        with self.transaction() as connection:
            instant = self.timestamp()
            self.check_session_expiry(expiry, instant)
            self.fresh_user(connection, subject, instant)
            return self.permission(connection, subject, instant)

    def record(self, connection: Any, subject: str, request_id: str) -> Any:
        return (
            self.execute(
                connection,
                "SELECT * FROM eat_bridge_reservations WHERE user_id=:uid AND request_id=:rid",
                uid=subject,
                rid=request_id,
            )
            .mappings()
            .first()
        )

    def reservation(self, connection: Any, subject: Any, request_id: str, instant: datetime) -> dict[str, Any]:
        quota = self.permission(connection, subject, instant)
        record = self.record(connection, str(subject), request_id)
        balance = quota["remaining_credits"]
        if record is not None:
            return self.result(record["state"], balance, request_id)
        if balance <= 0:
            raise HTTPException(402, "No eat AI credits remaining today")
        self.set_balance(connection, subject, balance - 1, quota["reset_date"])
        self.execute(
            connection,
            """INSERT INTO eat_bridge_reservations(user_id,request_id,state,charged_date,expires_at)
               VALUES (:uid,:rid,'reserved',:day,:expiry)""",
            uid=str(subject),
            rid=request_id,
            day=quota["reset_date"],
            expiry=(instant + timedelta(seconds=self.reservation_ttl)).timestamp(),
        )
        return self.result("reserved", balance - 1, request_id)

    def refundable_quota(self, connection: Any, subject: str, instant: datetime) -> dict[str, Any] | None:
        try:
            self.fresh_user(connection, int(subject), instant)
        except HTTPException:
            return None
        row = self.execute(
            connection,
            "SELECT 1 FROM user_app_permissions WHERE user_id=:uid AND app_id=:app",
            uid=subject,
            app=APP_ID,
        ).first()
        if row is None:
            return None
        return self.permission(connection, int(subject), instant)

    def settlement(self, connection: Any, body: SettlementRequest, action: str, instant: datetime) -> dict[str, Any]:
        request_id = str(body.request_id)
        record = self.record(connection, body.subject, request_id)
        if record is None:
            raise HTTPException(404, "Credit reservation does not exist")
        quota = self.refundable_quota(connection, body.subject, instant)
        # Lazy expiration can have closed the record while collecting the balance.
        record = self.record(connection, body.subject, request_id)
        balance = quota["remaining_credits"] if quota else 0
        state = record["state"]
        if state == "reserved":
            state = (
                "expired"
                if record["expires_at"] <= instant.timestamp()
                else ("committed" if action == "commit" else "refunded")
            )
            if state in ("refunded", "expired") and quota and record["charged_date"] == quota["reset_date"]:
                balance += 1
                self.set_balance(connection, int(body.subject), balance, quota["reset_date"])
            self.execute(
                connection,
                "UPDATE eat_bridge_reservations SET state=:state WHERE user_id=:uid AND request_id=:rid",
                state=state,
                uid=body.subject,
                rid=request_id,
            )
        return self.result(state, balance, request_id)

    def result(self, state: str, balance: int, request_id: str) -> dict[str, Any]:
        return {"state": state, "remaining_credits": balance, "request_id": request_id, "app_id": APP_ID}

    async def reserve(self, body: QuotaRequest) -> dict[str, Any]:
        subject, expiry = await self.authenticate(body.token)
        with self.transaction() as connection:
            instant = self.timestamp()
            self.check_session_expiry(expiry, instant)
            self.fresh_user(connection, subject, instant)
            return self.reservation(connection, subject, str(body.request_id), instant)

    def settlement_endpoint(self, operation: str) -> Callable[..., Any]:
        async def handle(body: SettlementRequest) -> dict[str, Any]:
            with self.transaction() as connection:
                return self.settlement(connection, body, operation, self.timestamp())

        return handle


def create_app(
    server_module: Any,
    bridge_key: str,
    *,
    engine: Any | None = None,
    now: Callable[[], datetime] | None = None,
    reservation_ttl: int = RESERVATION_TTL,
    token_lifetime_seconds: int = 3600,
) -> FastAPI:
    bridge = EatBridge(
        server_module,
        bridge_key,
        engine=engine,
        now=now,
        reservation_ttl=reservation_ttl,
        token_lifetime_seconds=token_lifetime_seconds,
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Any, _error: RequestValidationError) -> JSONResponse:
        # Pydantic's default error response echoes invalid tokens and passwords.
        return JSONResponse({"detail": "Invalid identity or quota request"}, status_code=422)

    protected = [Depends(bridge.require_key)]
    app.add_api_route("/login", bridge.login, methods=["POST"], dependencies=protected)
    app.add_api_route("/verify", bridge.verify, methods=["POST"], dependencies=protected)
    app.add_api_route("/quota/status", bridge.status, methods=["POST"], dependencies=protected)
    app.add_api_route("/quota/reserve", bridge.reserve, methods=["POST"], dependencies=protected)
    for action in ("commit", "refund"):
        app.add_api_route(
            f"/quota/{action}", bridge.settlement_endpoint(action), methods=["POST"], dependencies=protected
        )
    return app


def load_app() -> FastAPI:
    """Uvicorn factory; launch with legacy SSO directory as cwd and its venv."""
    return create_app(
        importlib.import_module("main"),
        os.environ["EAT_SSO_BRIDGE_KEY"],
        engine=importlib.import_module("database").engine,
    )
