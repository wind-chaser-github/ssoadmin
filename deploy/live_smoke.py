#!/usr/bin/env python3
"""Production smoke using disposable identities only; no model calls or user list.

Run with the SSO venv from its cwd. Tokens/bridge keys remain in process memory.
The optional report contains check names/counts, never identity data or secrets.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import http.client
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import sys
import uuid

DB = Path('/opt/baby-profile/sso-service/sso.db')
APPS = {'baby-profile': 5, 'c-learn': 5, 'ai-volunteer-planner': 5,
        'little-story-press': 5, 'chess-atelier': 50, 'eat': 5}


class LocalHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # Keep the real hostname for both TLS SNI and certificate verification,
        # while connecting only to the host's own nginx listener.
        raw = socket.create_connection(('127.0.0.1', 443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def request(path, token=None, payload=None, *, direct=False):
    connection = (http.client.HTTPConnection('127.0.0.1', 5191, timeout=10) if direct else
                  LocalHTTPS('lazylady.cc', timeout=10, context=ssl.create_default_context()))
    headers = {'Content-Type': 'application/json', 'Origin': 'https://admin.lazylady.cc'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        connection.request('POST' if payload is not None else 'GET', path,
                           json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        body = response.read()
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            data = None
        return response.status, data
    finally:
        connection.close()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def secret(path, name):
    for line in Path(path).read_text().splitlines():
        if line.startswith(name + '='):
            return line.split('=', 1)[1].strip().strip('\"\'')
    raise RuntimeError('Private bridge credential missing')


def kitchen_status(token):
    key = secret('/opt/mealie/.sso-bridge/bridge.env', 'EAT_SSO_BRIDGE_KEY')
    connection = http.client.HTTPConnection('localhost', timeout=10)
    transport = socket.socket(socket.AF_UNIX)
    transport.settimeout(10)
    transport.connect('/opt/mealie/.sso-bridge/runtime/eat.sock')
    connection.sock = transport
    try:
        connection.request('POST', '/quota/status', json.dumps({'token': token}),
                           {'Content-Type': 'application/json', 'X-Eat-Bridge-Key': key})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or Path.cwd() != DB.parent:
        parser.error('run as root in /opt/baby-profile/sso-service using its venv')
    os.umask(0o077)
    sys.path.insert(0, str(DB.parent))
    central = importlib.import_module('main')
    checks, identities = [], []
    report = {'ok': False, 'checks': checks, 'cleanup_ok': False}

    def check(name, condition):
        if not condition:
            report['failed_check'] = name
            raise RuntimeError('Smoke assertion failed: ' + name)
        checks.append(name)

    try:
        with closing(sqlite3.connect(DB, timeout=10)) as db, db:
            db.execute('PRAGMA foreign_keys=ON')
            for admin in (True, False):
                name = '__quota_smoke_' + uuid.uuid4().hex
                user_id = db.execute(
                    'INSERT INTO users(username,password_hash,role,is_admin,created_at) VALUES(?,?,?,?,?)',
                    (name, '!disabled-smoke-login!', 'user', int(admin),
                     datetime.now(timezone.utc).replace(tzinfo=None).isoformat()),
                ).lastrowid
                identities.append((user_id, name, admin))
            subject = identities[1][0]
            for app in (*APPS, 'seth-english'):
                # NULL avoids the kitchen's intentional legacy insert guard;
                # the API under test must settle this stale day before granting.
                db.execute('INSERT INTO user_app_permissions '
                           '(user_id,app_id,ai_credits_balance,last_reset_date) VALUES(?,?,?,NULL)',
                           (subject, app, 2))
        tokens = [central.create_access_token(
            {'sub': name, 'user_id': uid, 'is_admin': admin}, expires_delta=timedelta(minutes=10)
        ) for uid, name, admin in identities]
        admin_token, user_token = tokens
        check('sidecar_health', request('/health', direct=True)[0] == 200)
        check('https_unauthenticated_rejected', request('/sso/admin/quotas/users')[0] == 401)
        check('normal_user_list_rejected', request('/sso/admin/quotas/users', user_token)[0] == 403)
        for app, default in APPS.items():
            base = f'/sso/admin/quotas/users/{subject}/apps/{app}'
            code, body = request(base + '/daily', admin_token, {'daily_quota': 100})
            check(app + ':daily_keeps_today', code == 200 and body['app']['daily_quota'] == 100
                  and body['app']['quota'] == default)
            code, body = request(base + '/remaining', admin_token, {'quota': 17})
            check(app + ':today_grant', code == 200 and body['app']['quota'] == 17
                  and body['app']['daily_quota'] == 100)
            code, body = request(f'/sso/auth/users/{subject}/apps/{app}/quota',
                                 admin_token, {'quota': 17})
            check(app + ':legacy_setter_routed', code == 200 and body['new_quota'] == 17
                  and body.get('app', {}).get('daily_quota') == 100)
            for action, field in (('daily', 'daily_quota'), ('remaining', 'quota')):
                check(app + ':' + action + '_ordinary_forbidden',
                      request(base + '/' + action, user_token, {field: 999})[0] == 403)
                for invalid in (-1, 1.5):
                    check(app + ':' + action + '_reject_' + str(invalid),
                          request(base + '/' + action, admin_token, {field: invalid})[0] == 422)
            if app not in ('chess-atelier', 'eat'):
                code, body = request('/sso/api/quota/consume', user_token, {'appId': app, 'cost': 1})
                check(app + ':consume_keeps_grant', code == 200 and body['remaining_credits'] == 16)
        for app in ('chess-atelier', 'eat', 'seth-english'):
            check(app + ':generic_bypass_rejected', request('/sso/api/quota/consume', user_token,
                  {'appId': app, 'cost': 1})[0] == 403)
        for invalid in (-1, 1.5):
            check('generic_reject_' + str(invalid), request('/sso/api/quota/consume', user_token,
                  {'appId': 'baby-profile', 'cost': invalid})[0] == 422)
        for action, field in (('daily', 'daily_quota'), ('remaining', 'quota')):
            check('english_' + action + '_excluded', request(
                f'/sso/admin/quotas/users/{subject}/apps/seth-english/' + action,
                admin_token, {field: 100})[0] == 409)
        # Invoke the deployed chess accounting primitives directly. Its status()
        # also performs global old-ledger cleanup; avoid touching real users' rows.
        chess = load_module('smoke_current_chess', '/opt/chess-sso-bridge/current/chess_bridge.py')
        store = chess.QuotaStore(str(DB))
        with store.transaction() as db:
            day = datetime.now(timezone.utc).date().isoformat()
            balance = store._permission(db, subject, day)
            spendable = store._available(db, subject, day, balance)
        check('deployed_chess_spendable_grant', spendable == 17)
        code, body = kitchen_status(user_token)
        check('deployed_kitchen_spendable_grant', code == 200 and body['remaining_credits'] == 17)
        with closing(sqlite3.connect(DB, timeout=10)) as db:
            row = db.execute('SELECT ai_credits_balance,last_reset_date FROM user_app_permissions '
                             'WHERE user_id=? AND app_id=?', (subject, 'seth-english')).fetchone()
            check('english_row_unchanged', row == (2, None))
            check('synthetic_policies_persisted', db.execute(
                'SELECT count(*) FROM user_app_quota_policies WHERE user_id=? AND daily_quota=100',
                (subject,)).fetchone()[0] == len(APPS))
            check('kitchen_guard_closed', db.execute(
                'SELECT authorized FROM eat_bridge_write_guard WHERE singleton=1').fetchone()[0] == 0)
        report['ok'] = True
    except Exception as error:
        # No response body, tokens, user records, or raw exception are exported.
        report['error_type'] = type(error).__name__
    finally:
        try:
            with closing(sqlite3.connect(DB, timeout=10)) as db, db:
                db.execute('PRAGMA foreign_keys=ON')
                for uid, name, _admin in identities:
                    # Prove these still are our exact ephemeral identities before deletion.
                    found = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
                    if found is None:
                        continue  # Creation may have rolled back before commit.
                    if found != (name,):
                        raise RuntimeError('Synthetic identity changed before cleanup')
                    for table in ('chess_quota_requests', 'chess_daily_quotas', 'eat_bridge_reservations',
                                  'eat_bridge_accounts', 'user_app_quota_policies'):
                        db.execute('DELETE FROM ' + table + ' WHERE user_id=?', (uid,))
                    db.execute('DELETE FROM quota_admin_audit WHERE user_id=? OR actor_id=?', (uid, uid))
                    db.execute('DELETE FROM user_app_permissions WHERE user_id=?', (uid,))
                    db.execute('DELETE FROM users WHERE id=? AND username=?', (uid, name))
            report['cleanup_ok'] = True
        except Exception:
            report['cleanup_ok'] = False
            report['ok'] = False
        report['passed_count'] = len(checks)
        if args.report:
            args.report.write_text(json.dumps(report, indent=2))
            args.report.chmod(0o600)
        print(json.dumps({'ok': report['ok'], 'passed_count': len(checks),
                          'cleanup_ok': report['cleanup_ok'], 'error_type': report.get('error_type')}))
    return 0 if report['ok'] and report['cleanup_ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
