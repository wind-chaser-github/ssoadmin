"""Exercise public API and actual SQLite/bridge contracts without real accounts."""

import importlib.util
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from deploy.quota_service import DEFAULTS, QuotaStore, create_app


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


ROOT = Path(__file__).parents[1]
chess = module("quota_test_chess", ROOT / "deploy/adapters/chess_bridge.py")
eat = module("quota_test_eat", ROOT / "deploy/adapters/eat_bridge.py")


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sso.db"
        self.now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc).timestamp()
        self.today = "2026-10-23"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,is_admin INTEGER,role TEXT,created_at TEXT);
                INSERT INTO users VALUES(1,'root',0,'parent','2026-01-01'),
                    (2,'test-user',0,'parent','2026-01-01'),(3,'test-admin',1,'parent','2026-01-01');
                CREATE TABLE user_app_permissions(user_id INTEGER,app_id TEXT,ai_credits_balance INTEGER,last_reset_date TEXT);
            """)
            db.executemany(
                "INSERT INTO user_app_permissions VALUES(?,?,?,?)",
                [(2, app_id, 0, "2026-10-22") for app_id in DEFAULTS]
                + [(2, "seth-english", 3, "2026-10-21")],
            )
        self.store = QuotaStore(self.path, lambda: self.now)
        self.store.initialize()
        self.chess = chess.QuotaStore(str(self.path), lambda: self.now)
        self.chess.initialize()
        self.engine = create_engine(f"sqlite:///{self.path}", connect_args={"check_same_thread": False})
        self.kitchen = eat.EatBridge(
            SimpleNamespace(engine=self.engine), "test-key-never-used-on-server-123456789",
            now=lambda: datetime.fromtimestamp(self.now, timezone.utc),
        )
        self.admin = self.claims(3, "test-admin")
        self.user = self.claims(2, "test-user")
        central = SimpleNamespace(get_current_user=lambda token: {"admin": self.admin, "user": self.user}[token])
        self.client = TestClient(create_app(central=central, store=self.store))
        self.headers = {"Authorization": "Bearer admin", "Origin": "https://admin.lazylady.cc"}

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.directory.cleanup()

    def claims(self, uid, name):
        return {"user_id": uid, "sub": name, "exp": self.now + 10 * 86400, "is_admin": True}

    def update(self, app_id, action, value):
        return self.store.update(self.admin, 2, app_id, action, value)

    def balance(self, app_id):
        users = self.store.users(self.admin)
        return next(a for u in users if u["id"] == 2 for a in u["apps"] if a["app_id"] == app_id)

    def query(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute(sql, args).fetchall()

    def test_defaults_and_english_are_separate(self):
        for app_id, default in DEFAULTS.items():
            with self.subTest(app=app_id):
                view = self.balance(app_id)
                self.assertEqual((view["quota"], view["daily_quota"]), (default, default))
                self.assertIsNone(view["daily_quota_override"])
        english = self.balance("seth-english")
        self.assertFalse(english["quota_managed"])
        self.assertEqual((english["quota"], english["quota_reset_date"]), (3, "2026-10-21"))

    def test_daily_setting_preserves_today_and_survives_next_day_for_all_products(self):
        for app_id in DEFAULTS:
            self.update(app_id, "remaining", 2)
            result = self.update(app_id, "daily", 100)
            self.assertEqual(result["effective_from"], "2026-10-24")
            self.assertEqual(result["app"]["quota"], 2)
        self.now += 86400
        for app_id in DEFAULTS:
            with self.subTest(app=app_id):
                self.assertEqual(self.balance(app_id)["quota"], 100)

    def test_zero_means_zero_and_null_restores_product_default(self):
        for app_id in DEFAULTS:
            self.update(app_id, "daily", 0)
        self.now += 86400
        for app_id in DEFAULTS:
            self.assertEqual(self.balance(app_id)["quota"], 0)
            self.update(app_id, "daily", None)
            self.assertEqual(self.balance(app_id)["quota"], 0)
        self.now += 86400
        for app_id, default in DEFAULTS.items():
            self.assertEqual(self.balance(app_id)["quota"], default)

    def test_top_up_before_first_visit_is_not_overwritten(self):
        for app_id in DEFAULTS:
            self.update(app_id, "remaining", 300)
            self.assertEqual(self.balance(app_id)["quota"], 300)
        self.assertEqual(self.store.consume(self.user, "baby-profile", 1)["remaining_credits"], 299)
        self.assertEqual(self.chess.status(2)["remaining_credits"], 300)
        with self.kitchen.transaction() as db:
            result = self.kitchen.permission(db, 2, datetime.fromtimestamp(self.now, timezone.utc))
        self.assertEqual(result["remaining_credits"], 300)

    def test_today_set_is_absolute_and_does_not_change_daily_policy(self):
        self.update("baby-profile", "daily", 25)
        self.update("baby-profile", "remaining", 17)
        self.update("baby-profile", "remaining", 17)
        self.assertEqual(self.balance("baby-profile")["quota"], 17)
        self.now += 86400
        self.assertEqual(self.balance("baby-profile")["quota"], 25)

    def test_old_setter_has_same_safe_semantics(self):
        response = self.client.post("/sso/auth/users/2/apps/baby-profile/quota", json={"quota": 60}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["new_quota"], 60)
        self.assertEqual(self.store.consume(self.user, "baby-profile", 1)["remaining_credits"], 59)

    def test_concurrent_generic_consumption_cannot_overspend(self):
        self.update("little-story-press", "remaining", 5)

        def consume(_):
            try:
                self.store.consume(self.user, "little-story-press", 1)
                return True
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 402)
                return False

        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(consume, range(25))), 5)
        self.assertEqual(self.balance("little-story-press")["quota"], 0)

    def test_insufficient_consumption_still_settles_new_day(self):
        self.update("baby-profile", "daily", 0)
        self.now += 86400
        with self.assertRaises(HTTPException) as caught:
            self.store.consume(self.user, "baby-profile", 1)
        self.assertEqual(caught.exception.status_code, 402)
        self.assertEqual(self.query("SELECT ai_credits_balance,last_reset_date FROM user_app_permissions WHERE app_id='baby-profile'"), [(0, "2026-10-24")])

    def test_chess_keeps_today_authorization_when_changing_future_grant(self):
        self.update("chess-atelier", "remaining", 120)
        request_id = str(uuid.uuid4())
        self.chess.mutate(2, "reserve", request_id)
        self.chess.mutate(2, "commit", request_id)
        self.update("chess-atelier", "daily", 1)
        self.assertEqual(self.chess.status(2)["remaining_credits"], 119)
        self.assertEqual(self.chess.status(2)["daily_limit"], 120)
        self.now += 86400
        self.assertEqual(self.chess.status(2)["remaining_credits"], 1)
        self.assertEqual(self.chess.status(2)["daily_limit"], 1)

    def test_pending_chess_refund_does_not_inflate_next_day(self):
        request_id = str(uuid.uuid4())
        self.chess.mutate(2, "reserve", request_id)
        self.update("chess-atelier", "daily", 8)
        self.now += 86400
        self.assertEqual(self.balance("chess-atelier")["quota"], 8)
        self.assertEqual(self.chess.mutate(2, "refund", request_id)["remaining_credits"], 8)

    def test_kitchen_guard_stays_closed_and_legacy_default_response_is_preserved(self):
        self.update("eat", "daily", 40)
        self.update("eat", "remaining", 11)
        self.assertEqual(self.query("SELECT authorized FROM eat_bridge_write_guard"), [(0,)])
        with self.kitchen.transaction() as db:
            result = self.kitchen.permission(db, 2, datetime.fromtimestamp(self.now, timezone.utc))
        self.assertEqual(result["remaining_credits"], 11)
        self.assertEqual(result["default_daily_credits"], 5)
        self.assertEqual(set(result), {"remaining_credits", "reset_date", "default_daily_credits", "app_id"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.query("UPDATE user_app_permissions SET ai_credits_balance=99,last_reset_date='2099-01-01' WHERE app_id='eat'")

    def test_expired_kitchen_reservations_are_refunded_once(self):
        self.update("eat", "remaining", 2)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO eat_bridge_reservations VALUES('2','test','reserved','2026-10-23',?)", (self.now - 1,))
        self.assertEqual(self.balance("eat")["quota"], 3)
        self.assertEqual(self.balance("eat")["quota"], 3)

    def test_kitchen_old_day_refunds_do_not_inflate_today(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO eat_bridge_reservations VALUES('2','old','reserved','2026-10-22',?)", (self.now - 1,))
        self.assertEqual(self.balance("eat")["quota"], 5)

    def test_generic_products_do_not_change_other_balances_or_english_rows(self):
        before = self.query("SELECT * FROM user_app_permissions WHERE app_id!='baby-profile'")
        self.update("baby-profile", "daily", 27)
        self.update("baby-profile", "remaining", 31)
        self.store.consume(self.user, "baby-profile", 1)
        self.assertEqual(before, self.query("SELECT * FROM user_app_permissions WHERE app_id!='baby-profile'"))

    def test_english_management_is_rejected_without_writes(self):
        before = self.query("SELECT * FROM user_app_permissions WHERE app_id='seth-english'")
        for action, payload in [("daily", {"daily_quota": 100}), ("remaining", {"quota": 100})]:
            response = self.client.post(f"/admin/quotas/users/2/apps/seth-english/{action}", json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 409)
        self.assertEqual(before, self.query("SELECT * FROM user_app_permissions WHERE app_id='seth-english'"))

    def test_admin_api_rejects_missing_expired_forged_or_revoked_identity(self):
        route = "/admin/quotas/users"
        self.assertEqual(self.client.get(route).status_code, 401)
        self.assertEqual(self.client.get(route, headers={"Authorization": "Bearer bad"}).status_code, 401)
        self.assertEqual(self.client.get(route, headers={"Authorization": "Bearer user"}).status_code, 403)
        self.admin["exp"] = self.now - 1
        self.assertEqual(self.client.get(route, headers=self.headers).status_code, 401)
        self.admin["exp"] = self.now + 1000
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE users SET is_admin=0 WHERE id=3")
        self.assertEqual(self.client.get(route, headers=self.headers).status_code, 403)

    def test_invalid_admin_origin_does_not_write(self):
        response = self.client.post(
            "/admin/quotas/users/2/apps/baby-profile/remaining", json={"quota": 50},
            headers={"Authorization": "Bearer admin", "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.query("SELECT * FROM quota_admin_audit"), [])

    def test_input_is_strict_and_bounded_for_both_controls(self):
        for action, key in [("daily", "daily_quota"), ("remaining", "quota")]:
            for invalid in [-1, 100001, 1.5, True, "12", "sensitive-input"]:
                with self.subTest(action=action, value=invalid):
                    response = self.client.post(f"/admin/quotas/users/2/apps/baby-profile/{action}", json={key: invalid}, headers=self.headers)
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn("sensitive-input", response.text)
        self.assertEqual(self.query("SELECT * FROM quota_admin_audit"), [])

    def test_unknown_user_ungranted_product_and_duplicate_rows_fail_closed(self):
        for user_id, app_id, expected in [(999, "baby-profile", 404), (3, "baby-profile", 404), (2, "unknown", 404)]:
            response = self.client.post(f"/admin/quotas/users/{user_id}/apps/{app_id}/daily", json={"daily_quota": 10}, headers=self.headers)
            self.assertEqual(response.status_code, expected)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO user_app_permissions VALUES(2,'baby-profile',5,'2026-10-23')")
        response = self.client.post("/admin/quotas/users/2/apps/baby-profile/remaining", json={"quota": 5}, headers=self.headers)
        self.assertEqual(response.status_code, 503)

    def test_generic_api_disallows_negative_cost_and_dedicated_bridge_bypass(self):
        headers = {"Authorization": "Bearer user"}
        for invalid in [-1, True, 1.5, "1"]:
            response = self.client.post("/api/quota/consume", json={"appId": "baby-profile", "cost": invalid}, headers=headers)
            self.assertEqual(response.status_code, 422)
        for app_id in ["eat", "chess-atelier", "seth-english"]:
            response = self.client.post("/api/quota/consume", json={"appId": app_id, "cost": 1}, headers=headers)
            self.assertEqual(response.status_code, 403)

    def test_admin_audit_records_decisions_and_responses_disable_caching(self):
        response = self.client.post("/admin/quotas/users/2/apps/baby-profile/daily", json={"daily_quota": 20}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.query("SELECT actor_id,user_id,app_id,action,previous_value,new_value FROM quota_admin_audit"), [(3, 2, "baby-profile", "daily", None, 20)])


if __name__ == "__main__":
    unittest.main()
