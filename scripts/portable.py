#!/usr/bin/env python3
"""Portable menu and CLI for Codex local history repair. Python 3.11+."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import ccswitch_stable as cc
import provider_history as history


def running_writers():
    names = {'codex.exe', 'codex-desktop.exe', 'cc-switch.exe', 'codex', 'codex-desktop', 'cc-switch'}
    if os.name == 'nt':
        import csv
        result = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                                text=True, errors='replace', check=True)
        return sorted({r[0] for r in csv.reader(result.stdout.splitlines()) if r and r[0].lower() in names})
    result = subprocess.run(['ps', '-A', '-o', 'comm='], capture_output=True, text=True, check=True)
    return sorted({Path(line.strip()).name for line in result.stdout.splitlines()
                   if Path(line.strip()).name.lower() in names})


def quiet_history(args):
    with redirect_stdout(io.StringIO()):
        return history.main(args)


def execute(action, home, target='shared', cc_db=None, state_db=None, apply=False):
    home = Path(home).expanduser().resolve()
    candidate = Path(cc_db).expanduser().resolve() if cc_db else Path(
        os.environ.get('CCSWITCH_HOME', str(Path.home() / '.cc-switch'))) / 'cc-switch.db'
    cc_needed = action == 'cc-switch' or (action == 'setup' and candidate.is_file())
    if action != 'cc-switch':
        config_path = home / 'config.toml'
        config_before = config_path.read_bytes()
        normalized, old, config_changed = cc.normalize_config(config_before.decode('utf-8-sig'), target)
        db = history.database(home, state_db)
        base = ['--home', str(home), '--db', str(db), '--target', target]
        if action == 'doctor':
            with history.connect(db) as connection:
                report = history.doctor(connection, home, target)
            print(json.dumps({'codex_home': str(home), 'database': str(db),
                              'current_provider': old, 'target_provider': target,
                              'scanned': report['scanned'], 'archived': report['archived_scanned'],
                              'provider_mismatches': len(report['provider_mismatches']),
                              'metadata_mismatches': len(report['metadata_mismatches']),
                              'path_repairs': len(report['path_repairs']),
                              'missing_or_invalid': len(report['missing_rollouts']),
                              'config_change_needed': config_changed}, ensure_ascii=False, indent=2))
            return 0
        preview = quiet_history(base + ['--all'])
        print(json.dumps({'threads_scanned': preview['scanned'], 'threads_to_update': preview['changes'],
                          'config_change_needed': config_changed}, ensure_ascii=False))
    if cc_needed:
        if cc.main(['--db', str(candidate), '--target', target]) != 0:
            raise ValueError('CC Switch preview failed.')
    if not apply:
        print('PREVIEW ONLY. No config, credentials, templates or history were changed.')
        return 0
    writers = running_writers()
    if writers:
        raise ValueError('Close these apps fully, including tray/background processes: ' + ', '.join(writers))
    # CC Switch and Codex changes have separate backups and transactions.
    if cc_needed:
        if cc.main(['--db', str(candidate), '--target', target, '--apply']) != 0:
            raise ValueError('CC Switch normalization failed. History was not changed.')
    if action in {'repair', 'setup'}:
        if config_path.read_bytes() != config_before:
            raise ValueError('Config changed since preview. Run again.')
        config_backup = None
        try:
            if config_changed:
                config_backup = config_path.with_name('config.toml.before-stable-' + uuid.uuid4().hex[:10])
                config_backup.write_bytes(config_before)
                history.atomic_write(config_path, normalized.encode('utf-8'))
            result = quiet_history(base + ['--all', '--apply', '--offline'])
        except BaseException:
            if config_backup and config_path.read_bytes() == normalized.encode('utf-8'):
                history.atomic_write(config_path, config_before)
            raise
        result['config_backup'] = str(config_backup) if config_backup else None
        print(json.dumps(result, ensure_ascii=False, indent=2))
        check = quiet_history(base + ['--all'])
        if check['changes']:
            raise ValueError('Verification found remaining changes. See local backup manifests.')
        print('VERIFIED: history metadata and paths match the stable provider.')
    print('DONE. Reopen CC Switch and Codex to load the new configuration.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=['doctor', 'repair', 'cc-switch', 'setup'], default='doctor')
    parser.add_argument('--home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    parser.add_argument('--cc-db', help='Explicit CC Switch database path')
    parser.add_argument('--db', help='Explicit Codex state database when discovery is ambiguous')
    parser.add_argument('--target', default='shared')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--menu', action='store_true')
    args = parser.parse_args(argv)
    if args.menu:
        print('Codex 会话连续性工具 / Codex Context Provider\n')
        print('1. 只读诊断\n2. 首次配置与历史修复（含已安装的 CC Switch）\n3. 新增 provider 后更新 CC Switch 模板\n0. 退出')
        choice = input('选择 / Choose [1]: ').strip() or '1'
        if choice == '0':
            return 0
        if choice not in {'1', '2', '3'}:
            raise ValueError('Unknown menu option.')
        args.action = {'1': 'doctor', '2': 'setup', '3': 'cc-switch'}[choice]
        if choice != '1':
            print('请先完全退出 Codex Desktop、CLI/app-server 和 CC Switch（含托盘）。')
            print('会话 provider ID 将统一为 shared；先预览，然后输入 APPLY 才写入。')
    execute(args.action, args.home, args.target, args.cc_db, args.db, apply=False)
    if args.action == 'doctor':
        return 0
    apply = args.apply
    if args.menu:
        apply = input('应用以上变更请输入 APPLY；回车退出: ').strip() == 'APPLY'
    if apply:
        return execute(args.action, args.home, args.target, args.cc_db, args.db, apply=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, sqlite3.Error, history.MigrationError, cc.StableProviderError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
