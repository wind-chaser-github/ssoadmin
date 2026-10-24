"""Isolated bridge regressions; never connect to production or a model provider."""
import importlib.util
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError


def load_adapter(name):
    path = Path(__file__).resolve().parents[1] / "deploy" / "adapters" / f"{name}_bridge.py"
    spec = importlib.util.spec_from_file_location(f"quota_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chess = load_adapter("chess")
eat = load_adapter("eat")


class AdapterFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "sso.db")
        self.now = datetime(2026, 10, 23, 12, tzinfo=UTC)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT,
                    expires_at TEXT, created_at TEXT, is_admin INTEGER DEFAULT 0);
                INSERT INTO users VALUES(1,'admin','hash',NULL,'2026-01-01',1),
                    (2,'user','hash',NULL,'2026-01-02',0);
                CREATE TABLE user_app_permissions(user_id INTEGER,app_id TEXT,
                    ai_credits_balance INTEGER,last_reset_date TEXT);
                INSERT INTO user_app_permissions VALUES(2,'seth-english',17,'2026-01-01');
            """)

    def policy(self, app, quota):
        with closing(sqlite3.connect(self.path)) as db, db:
            # No column coercion/check here: tests also exercise corrupted legacy data.
            db.execute("CREATE TABLE IF NOT EXISTS user_app_quota_policies (user_id INTEGER,app_id TEXT,daily_quota,PRIMARY KEY(user_id,app_id))")
            db.execute("INSERT INTO user_app_quota_policies VALUES(2,?,?) ON CONFLICT(user_id,app_id) DO UPDATE SET daily_quota=excluded.daily_quota", (app, quota))

    def assert_english_untouched(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT ai_credits_balance,last_reset_date FROM user_app_permissions WHERE app_id='seth-english'").fetchone(), (17, "2026-01-01"))


class ChessDailyQuotaTests(AdapterFixture):
    def setUp(self):
        super().setUp()
        self.store = chess.QuotaStore(self.path, lambda: self.now.timestamp())
        self.store.initialize()

    def test_default_with_missing_table_or_row_and_other_apps_unchanged(self):
        self.assertEqual(self.store.status(2)["remaining_credits"], 50)
        self.policy("eat", 90)
        self.now += timedelta(days=1)
        self.assertEqual(self.store.status(2)["daily_limit"], 50)
        self.assert_english_untouched()

    def test_first_use_and_next_day_honor_policy_including_zero(self):
        self.policy(chess.APP_ID, 8)
        self.assertEqual(self.store.status(2)["remaining_credits"], 8)
        self.policy(chess.APP_ID, 0)
        self.assertEqual(self.store.status(2)["remaining_credits"], 8)
        self.assertEqual(self.store.status(2)["daily_limit"], 8)
        self.now += timedelta(days=1)
        self.assertEqual(self.store.status(2)["remaining_credits"], 0)
        self.assertEqual(self.store.status(2)["daily_limit"], 0)
        with self.assertRaises(chess.QuotaError) as error:
            self.store.mutate(2, "reserve", str(uuid4()))
        self.assertEqual(error.exception.status, 402)

    def test_lowering_policy_does_not_change_today_authorization_or_refunds(self):
        self.policy(chess.APP_ID, 3)
        request = str(uuid4())
        self.store.mutate(2, "reserve", request)
        self.policy(chess.APP_ID, 0)
        self.assertEqual(self.store.status(2)["remaining_credits"], 2)
        self.assertEqual(self.store.mutate(2, "refund", request)["remaining_credits"], 3)
        self.assertEqual(self.store.mutate(2, "refund", request)["remaining_credits"], 3)

    def test_admin_refill_before_first_use_survives_and_only_lasts_today(self):
        self.policy(chess.APP_ID, 4)
        self.assertEqual(self.store.set_remaining(1, 2, 70)["daily_limit"], 70)
        self.policy(chess.APP_ID, 6)
        self.assertEqual(self.store.status(2)["remaining_credits"], 70)
        request = str(uuid4())
        self.store.mutate(2, "reserve", request)
        self.now += timedelta(days=1)
        self.assertEqual(self.store.mutate(2, "refund", request)["remaining_credits"], 6)
        self.assertEqual(self.store.status(2)["daily_limit"], 6)
        self.assert_english_untouched()

    def test_concurrent_reservations_obey_configured_daily_limit(self):
        self.policy(chess.APP_ID, 3)

        def reserve(_):
            try:
                self.store.mutate(2, "reserve", str(uuid4()))
                return True
            except chess.QuotaError as error:
                self.assertEqual(error.status, 402)
                return False

        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(reserve, range(14))), 3)
        self.assertEqual(self.store.status(2)["remaining_credits"], 0)

    def test_restoring_default_keeps_today_then_restores_next_day(self):
        self.policy(chess.APP_ID, 7)
        self.assertEqual(self.store.status(2)["remaining_credits"], 7)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM user_app_quota_policies WHERE app_id=?", (chess.APP_ID,))
        self.assertEqual(self.store.status(2)["daily_limit"], 7)
        self.now += timedelta(days=1)
        self.assertEqual(self.store.status(2)["remaining_credits"], 50)

    def test_invalid_policy_fails_closed_without_grant(self):
        for value in (-1, 100001, 1.5, "bad", None):
            with self.subTest(value=value):
                self.policy(chess.APP_ID, value)
                with self.assertRaises(chess.QuotaError) as error:
                    self.store.status(2)
                self.assertEqual(error.exception.status, 503)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM user_app_permissions WHERE app_id=?", (chess.APP_ID,)).fetchone()[0], 0)


class EatDailyQuotaTests(AdapterFixture):
    def setUp(self):
        super().setUp()
        self.engine = create_engine(f"sqlite:///{self.path}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        self.bridge = eat.EatBridge(SimpleNamespace(engine=self.engine), "test-key-" + "x" * 32, now=lambda: self.now)

    def status(self, provision=True):
        with self.bridge.transaction() as db:
            self.bridge.fresh_user(db, 2, self.now)
            return self.bridge.permission(db, 2, self.now, provision=provision)

    def reserve(self, request=None):
        with self.bridge.transaction() as db:
            self.bridge.fresh_user(db, 2, self.now)
            return self.bridge.reservation(db, 2, str(request or uuid4()), self.now)

    def settle(self, request):
        with self.bridge.transaction() as db:
            return self.bridge.settlement(db, eat.SettlementRequest(subject="2", request_id=request), "refund", self.now)

    def test_default_missing_table_or_row_and_legacy_response_contract(self):
        result = self.status()
        self.assertEqual(result["remaining_credits"], 5)
        self.assertEqual(result["default_daily_credits"], 5)
        self.assertEqual(result, {"remaining_credits": 5, "reset_date": "2026-10-23", "default_daily_credits": 5, "app_id": "eat"})
        self.policy(chess.APP_ID, 90)
        self.now += timedelta(days=1)
        self.assertEqual(self.status()["remaining_credits"], 5)
        self.assert_english_untouched()

    def test_first_use_configured_quota_and_zero_next_day_keep_default_field(self):
        self.policy(eat.APP_ID, 8)
        result = self.status()
        self.assertEqual(result["remaining_credits"], 8)
        self.assertEqual(result["default_daily_credits"], 5)
        self.policy(eat.APP_ID, 0)
        self.assertEqual(self.status()["remaining_credits"], 8)
        self.now += timedelta(days=1)
        self.assertEqual(self.status()["remaining_credits"], 0)
        with self.assertRaises(HTTPException) as error:
            self.reserve()
        self.assertEqual(error.exception.status_code, 402)

    def test_admin_refill_before_first_access_is_atomic_and_does_not_reset(self):
        self.policy(eat.APP_ID, 3)
        with self.bridge.transaction() as db:
            # Mirrors central admin settlement + absolute remaining update under the guard.
            self.bridge.permission(db, 2, self.now, provision=True)
            self.bridge.set_balance(db, 2, 24, self.now.date().isoformat())
        self.assertEqual(self.status()["remaining_credits"], 24)
        self.assertEqual(self.reserve()["remaining_credits"], 23)
        self.now += timedelta(days=1)
        self.assertEqual(self.status()["remaining_credits"], 3)

    def test_concurrent_reservations_obey_policy_and_duplicate_is_once_only(self):
        self.policy(eat.APP_ID, 3)
        self.status()
        request = uuid4()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.reserve(request), range(8)))
        self.assertTrue(all(result["remaining_credits"] == 2 for result in results))

        def reserve(_):
            try:
                self.reserve()
                return True
            except HTTPException as error:
                self.assertEqual(error.status_code, 402)
                return False

        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(reserve, range(12))), 2)
        self.assertEqual(self.status()["remaining_credits"], 0)

    def test_old_day_refunds_never_inflate_new_configured_allowance(self):
        self.policy(eat.APP_ID, 3)
        self.status()
        request = uuid4()
        self.reserve(request)
        self.policy(eat.APP_ID, 9)
        self.now += timedelta(days=1)
        self.assertEqual(self.settle(request)["remaining_credits"], 9)
        self.assertEqual(self.settle(request)["remaining_credits"], 9)
        self.assert_english_untouched()

    def test_same_day_refund_is_idempotent_after_policy_change(self):
        self.policy(eat.APP_ID, 3)
        self.status()
        request = uuid4()
        self.reserve(request)
        self.policy(eat.APP_ID, 0)
        self.assertEqual(self.settle(request)["remaining_credits"], 3)
        self.assertEqual(self.settle(request)["remaining_credits"], 3)

    def test_invalid_policy_fails_closed_without_grant(self):
        for value in (-1, 100001, 1.5, "bad", None):
            with self.subTest(value=value):
                self.policy(eat.APP_ID, value)
                with self.assertRaises(HTTPException) as error:
                    self.status()
                self.assertEqual(error.exception.status_code, 503)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM user_app_permissions WHERE app_id='eat'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT authorized FROM eat_bridge_write_guard").fetchone()[0], 0)

    def test_restoring_default_applies_only_next_day(self):
        self.policy(eat.APP_ID, 7)
        self.assertEqual(self.status()["remaining_credits"], 7)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM user_app_quota_policies WHERE app_id='eat'")
        self.assertEqual(self.status()["remaining_credits"], 7)
        self.now += timedelta(days=1)
        self.assertEqual(self.status()["remaining_credits"], 5)

    def test_revocation_and_identity_expiry_still_deny_access(self):
        self.policy(eat.APP_ID, 9)
        self.status()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM user_app_permissions WHERE app_id='eat'")
        with self.assertRaises(HTTPException) as error:
            self.status()
        self.assertEqual(error.exception.status_code, 403)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE users SET expires_at='2026-01-01' WHERE id=2")
        with self.assertRaises(HTTPException) as error:
            self.status()
        self.assertEqual(error.exception.status_code, 401)

    def test_write_guard_still_blocks_untrusted_legacy_consumption(self):
        self.policy(eat.APP_ID, 9)
        self.status()
        with self.assertRaises(IntegrityError), self.engine.begin() as db:
            db.exec_driver_sql("UPDATE user_app_permissions SET ai_credits_balance=100,last_reset_date='2026-10-23' WHERE app_id='eat'")
        self.assertEqual(self.status()["remaining_credits"], 9)


if __name__ == "__main__":
    unittest.main()
