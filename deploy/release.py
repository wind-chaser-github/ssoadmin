#!/usr/bin/env python3
"""Server-local, fail-closed release. Rollback never restores the live database.

Run as root: python3 deploy/release.py --payload /path/to/payload --release TAG
Rollback: python3 deploy/release.py --rollback TAG
Only code/configuration are reverted; additive accounting data remain intact.
"""
import argparse
import ctypes
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

SSO = Path('/opt/baby-profile/sso-service')
DB = SSO / 'sso.db'
CHESS = Path('/opt/chess-sso-bridge/current')
EAT = Path('/opt/mealie/deploy/lazylady_sso_bridge/app.py')
NGINX = Path('/etc/nginx/sites-enabled/baby-profile.conf').resolve()
UNIT = Path('/etc/systemd/system/sso-quota.service')
QUOTA = Path('/opt/sso-quota/current')
ADMIN = Path('/var/www/sso-admin')
ARCHIVE = Path('/root/sso-quota-releases')
EXPECTED = {
    'chess': '122e33bae1b89c32fbb42dd60c0432537e5abc1a44e8b898a0df50bb7481c717',
    'eat': '77282087812220ae18b2f65e198cc59c14f08b0d5d8dcd6808298bd41c4fc391',
    'nginx': '923d5c49c843d27cfb212c29d37b4ee87a18231cdbfbb379c933e2c035f8a81c',
}


def run(*args):
    # Do not surface service logs or EnvironmentFile contents in release output.
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('command failed: ' + ' '.join(args[:2]))
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_file(path, data, mode=0o644):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.quota-new')
    temporary.write_bytes(data)
    temporary.chmod(mode)
    os.replace(temporary, path)


def atomic_link(path, target):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.quota-link')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, path)


def pid(unit):
    return run('systemctl', 'show', unit, '--property=MainPID', '--value')


def baseline():
    return {'core_pid': pid('baby-sso.service'), 'english_pid': pid('seth-english.service'),
            'chess_business_pid': pid('chess-atelier.service'), 'files': {
        name: digest(SSO / name) for name in
        ('main.py', 'database.py', 'english_entry.py', 'english_sso_extension.py')
    }}


def unchanged(before):
    if baseline() != before:
        raise RuntimeError('Core SSO/English baseline changed; refusing success')


def schema_preflight():
    required = {
        'users': {'id', 'username', 'is_admin'},
        'user_app_permissions': {'user_id', 'app_id', 'ai_credits_balance', 'last_reset_date'},
        'chess_daily_quotas': {'user_id', 'reset_date', 'daily_limit'},
        'chess_quota_requests': {'user_id', 'request_id', 'state', 'reset_date', 'expires_at', 'closed_at'},
        'eat_bridge_accounts': {'user_id', 'first_granted_at', 'identity_created_at'},
        'eat_bridge_reservations': {'user_id', 'request_id', 'state', 'charged_date', 'expires_at'},
        'eat_bridge_write_guard': {'singleton', 'authorized'},
    }
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10) as db:
        for table, columns in required.items():
            actual = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
            if not columns <= actual:
                raise RuntimeError('Required quota schema is missing: ' + table)
        if db.execute('SELECT singleton,authorized FROM eat_bridge_write_guard').fetchall() != [(1, 0)]:
            raise RuntimeError('Kitchen bridge write guard is not in its safe state')


def env_secret(path, key):
    # Values are used in-memory only and never serialized or passed in argv.
    for line in Path(path).read_text().splitlines():
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip().strip('\"\'')
    raise RuntimeError('Missing bridge health credential')


def health(kind):
    if kind == 'quota':
        with urllib.request.urlopen('http://127.0.0.1:5191/health', timeout=3) as response:
            body = json.load(response)
            if body.get('status') != 'ok' or not body.get('english_unchanged'):
                raise RuntimeError('Invalid quota health response')
    elif kind == 'chess':
        key = env_secret('/etc/chess-sso-bridge.env', 'CHESS_SSO_BRIDGE_KEY')
        req = urllib.request.Request('http://127.0.0.1:5189/health', headers={'X-Chess-Bridge': key})
        with urllib.request.urlopen(req, timeout=3) as response:
            if json.load(response).get('status') != 'ok':
                raise RuntimeError('Invalid chess health response')
    else:
        key = env_secret('/opt/mealie/.sso-bridge/bridge.env', 'EAT_SSO_BRIDGE_KEY')
        connection = http.client.HTTPConnection('localhost', timeout=3)
        transport = socket.socket(socket.AF_UNIX)
        transport.settimeout(3)
        transport.connect('/opt/mealie/.sso-bridge/runtime/eat.sock')
        connection.sock = transport
        try:
            connection.request('POST', '/verify', json.dumps({'token': 'invalid-health-probe'}),
                               {'Content-Type': 'application/json', 'X-Eat-Bridge-Key': key})
            if connection.getresponse().status != 401:
                raise RuntimeError('Invalid kitchen identity health response')
        finally:
            connection.close()


def wait_health(kind):
    for _ in range(20):
        try:
            health(kind)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(kind + ' bridge health did not pass')


def save_manifest(backup, manifest):
    atomic_file(backup / 'manifest.json', json.dumps(manifest, indent=2).encode(), 0o600)


def rollback(backup):
    manifest = json.loads((backup / 'manifest.json').read_text())
    errors = []
    # Restore external routing before stopping the new sidecar; old paths remain usable.
    for name, operation in (
        ('nginx', lambda: (atomic_file(NGINX, (backup / 'nginx.conf').read_bytes()),
                           run('nginx', '-t'), run('systemctl', 'reload', 'nginx'))),
        ('admin', lambda: atomic_link(ADMIN, manifest['admin_before'])
         if Path(manifest['admin_before']).is_dir() else None),
        ('chess', lambda: (atomic_link(CHESS, manifest['chess_before']),
                           run('systemctl', 'restart', 'chess-sso-bridge'), wait_health('chess'))),
        ('eat', lambda: (atomic_file(EAT, (backup / 'eat.py').read_bytes()),
                         run('systemctl', 'restart', 'mealie-eat-sso-bridge'), wait_health('eat'))),
    ):
        if name not in manifest.get('changed', []):
            continue
        try:
            operation()
        except Exception:
            errors.append(name)
    if 'quota' in manifest.get('changed', []):
        try:
            # A write failure may have happened before systemd learned the unit.
            subprocess.run(['systemctl', 'stop', 'sso-quota.service'], capture_output=True, timeout=45)
            if manifest['quota_before']:
                atomic_link(QUOTA, manifest['quota_before'])
            else:
                QUOTA.unlink(missing_ok=True)
            if manifest['unit_before']:
                atomic_file(UNIT, (backup / 'sso-quota.service').read_bytes())
            else:
                subprocess.run(['systemctl', 'disable', 'sso-quota.service'], capture_output=True, timeout=45)
                UNIT.unlink(missing_ok=True)
            run('systemctl', 'daemon-reload')
            if manifest['quota_running_before']:
                run('systemctl', 'start', 'sso-quota.service')
                wait_health('quota')
            if manifest['unit_before'] and not manifest['quota_enabled_before']:
                run('systemctl', 'disable', 'sso-quota.service')
        except Exception:
            errors.append('quota')
    try:
        unchanged(manifest['baseline'])
    except Exception:
        errors.append('core_english_invariance')
    manifest['rollback'] = {'ok': not errors, 'failed_components': errors, 'database_restored': False}
    save_manifest(backup, manifest)
    print(json.dumps({'rollback_ok': not errors, 'failed_components': errors, 'database_restored': False}))
    if errors:
        raise RuntimeError('Rollback has failed components; inspect root-private manifest')


def release(payload, tag):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', tag):
        raise RuntimeError('Invalid release tag')
    payload = payload.resolve()
    sources = ['deploy/quota_service.py', 'deploy/adapters/chess_bridge.py',
               'deploy/adapters/eat_bridge.py', 'deploy/nginx-quota.conf',
               'deploy/sso-quota.service', 'dist/index.html']
    for name in sources:
        if not (payload / name).is_file():
            raise RuntimeError('Payload missing ' + name)
    observed = {'chess': digest(CHESS / 'chess_bridge.py'), 'eat': digest(EAT), 'nginx': digest(NGINX)}
    if observed != EXPECTED:
        raise RuntimeError('Production source baseline differs; rebase and review before release')
    probe = socket.socket()
    try:
        probe.bind(('127.0.0.1', 5191))
    finally:
        probe.close()
    before = baseline()
    schema_preflight()
    if before['core_pid'] in ('', '0'):
        raise RuntimeError('Core SSO is not running')
    backup = ARCHIVE / tag
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup.chmod(0o700)
    chess_release = Path('/opt/chess-sso-bridge/releases') / ('quota-' + tag)
    quota_release = Path('/opt/sso-quota/releases') / tag
    admin_release = Path('/var/www/sso-admin-releases') / tag
    for path in (chess_release, quota_release, admin_release):
        if path.exists():
            raise RuntimeError('Release destination already exists')
    # sqlite backup is a consistent private recovery artifact, never a rollback action.
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10) as source:
        with sqlite3.connect(backup / 'sso.db') as destination:
            source.backup(destination)
    (backup / 'sso.db').chmod(0o600)
    shutil.copy2(EAT, backup / 'eat.py')
    shutil.copy2(NGINX, backup / 'nginx.conf')
    shutil.copytree(CHESS.resolve(), backup / 'chess-source')
    for name in before['files']:
        shutil.copy2(SSO / name, backup / name)
    if UNIT.exists():
        shutil.copy2(UNIT, backup / 'sso-quota.service')
    admin_before = str(ADMIN.resolve()) if ADMIN.is_symlink() else str(
        Path('/var/www/sso-admin-releases') / (tag + '-original'))
    manifest = {'release': tag, 'baseline': before, 'source_hashes': observed,
                'payload_hashes': {str(p.relative_to(payload)): digest(p) for p in payload.rglob('*')
                                   if p.is_file() and (p.relative_to(payload).parts[0] in ('deploy', 'dist'))
                                   and '__pycache__' not in p.parts},
                'chess_before': str(CHESS.resolve()), 'admin_before': admin_before,
                'quota_before': str(QUOTA.resolve()) if QUOTA.is_symlink() else None,
                'unit_before': UNIT.exists(),
                'quota_enabled_before': subprocess.run(
                    ['systemctl', 'is-enabled', '--quiet', 'sso-quota.service'], capture_output=True
                ).returncode == 0,
                'quota_running_before': pid('sso-quota.service') not in ('', '0'),
                'changed': [], 'database_restored': False}
    save_manifest(backup, manifest)

    def changing(component):
        manifest['changed'].append(component)
        save_manifest(backup, manifest)

    try:
        quota_release.mkdir(parents=True)
        shutil.copy2(payload / 'deploy/quota_service.py', quota_release / 'quota_service.py')
        shutil.copytree(CHESS.resolve(), chess_release)
        shutil.copy2(payload / 'deploy/adapters/chess_bridge.py', chess_release / 'chess_bridge.py')
        # Preserve assets referenced by already-open or cached HTML documents.
        shutil.copytree(ADMIN.resolve(), admin_release)
        shutil.copytree(payload / 'dist', admin_release, dirs_exist_ok=True)
        admin_release.parent.chmod(0o755)
        for path in admin_release.rglob('*'):
            path.chmod(0o755 if path.is_dir() else 0o644)
        admin_release.chmod(0o755)
        changing('quota')
        atomic_link(QUOTA, quota_release)
        atomic_file(UNIT, (payload / 'deploy/sso-quota.service').read_bytes())
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'sso-quota.service')
        wait_health('quota')
        changing('chess')
        atomic_link(CHESS, chess_release)
        run('systemctl', 'restart', 'chess-sso-bridge')
        wait_health('chess')
        changing('eat')
        atomic_file(EAT, (payload / 'deploy/adapters/eat_bridge.py').read_bytes())
        run('systemctl', 'restart', 'mealie-eat-sso-bridge')
        wait_health('eat')
        content = NGINX.read_text()
        pattern = r'    # Dedicated chess administrator quota route\.\n.*?^    }\n'
        updated, count = re.subn(pattern, (payload / 'deploy/nginx-quota.conf').read_text(),
                                 content, count=1, flags=re.S | re.M)
        if count != 1:
            raise RuntimeError('Exact old chess nginx route was not found')
        changing('nginx')
        atomic_file(NGINX, updated.encode())
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        if ADMIN.is_symlink():
            changing('admin')
            atomic_link(ADMIN, admin_release)
        else:
            # Linux exchange atomically replaces the existing real directory with
            # a symlink and preserves the original at the other name, without a gap.
            original = Path(admin_before)
            original.symlink_to(admin_release)
            libc = ctypes.CDLL(None, use_errno=True)
            exchange = libc.renameat2
            exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            changing('admin')
            if exchange(-100, os.fsencode(ADMIN), -100, os.fsencode(original), 2) != 0:
                original.unlink()
                raise OSError(ctypes.get_errno(), 'Atomic admin directory exchange failed')
        unchanged(before)
        manifest['health'] = {'quota': True, 'chess': True, 'eat': True, 'nginx': True,
                              'core_english_unchanged': True}
        manifest['ok'] = True
        save_manifest(backup, manifest)
        print(json.dumps({'release': tag, 'ok': True, 'evidence': str(backup / 'manifest.json')}))
    except BaseException:
        rollback(backup)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--release')
    parser.add_argument('--rollback')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run on the target server as root')
    os.umask(0o077)
    ARCHIVE.mkdir(mode=0o700, parents=True, exist_ok=True)
    ARCHIVE.chmod(0o700)
    with (ARCHIVE / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', args.rollback):
                parser.error('invalid rollback tag')
            rollback(ARCHIVE / args.rollback)
        elif args.payload and args.release:
            release(args.payload, args.release)
        else:
            parser.error('provide --payload and --release, or --rollback')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Exception messages contain only fixed labels/paths, never credentials.
        print(json.dumps({'ok': False, 'error_type': type(error).__name__}), file=sys.stderr)
        sys.exit(1)
