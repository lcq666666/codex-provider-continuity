#!/usr/bin/env python3
"""Keep CC Switch Codex templates on one stable history provider ID."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import copy
import tomllib
import sqlite3
import subprocess
import sys
import uuid


class StableProviderError(Exception):
    pass


ROOT_RE = re.compile(r'(?m)^(\s*model_provider\s*=\s*")[^"]+("\s*)$')
SECTION_RE = re.compile(r'(?m)^\s*\[model_providers\.([^\]]+)\]\s*$')


def ccswitch_running():
    if os.name != 'nt':
        return False
    result = subprocess.run(
        ['tasklist', '/FI', 'IMAGENAME eq cc-switch.exe', '/FO', 'CSV', '/NH'],
        capture_output=True, text=True, check=False,
    )
    return 'cc-switch.exe' in result.stdout.lower()


def normalize_config(text, target):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', target):
        raise StableProviderError('Stable ID must use 1-32 lowercase letters, digits or underscores.')
    parsed = tomllib.loads(text.lstrip('\ufeff'))
    if parsed.get('profile'):
        raise StableProviderError('Active profiles need manual review; root provider is not the effective provider.')
    old = parsed.get('model_provider', 'openai')
    providers = parsed.get('model_providers', {})
    if old != 'openai' and old not in providers:
        raise StableProviderError('Selected custom provider has no matching definition; repair it first.')
    if old != target and target in providers:
        raise StableProviderError('Target provider already exists; review this collision manually.')
    expected = copy.deepcopy(parsed)
    expected['model_provider'] = target
    if old == target:
        return text, old, False
    if old in providers:
        expected['model_providers'][target] = expected['model_providers'].pop(old)
    else:
        expected.setdefault('model_providers', {})[target] = {
            'name': 'OpenAI', 'wire_api': 'responses', 'requires_openai_auth': True,
            'base_url': 'https://api.openai.com/v1',
        }
    # Parse table names instead of guessing how dotted/quoted TOML keys are spelled.
    lines = text.lstrip('\ufeff').splitlines(keepends=True)
    root_end = next((i for i, line in enumerate(lines) if line.lstrip().startswith('[')), len(lines))
    replaced_root = False
    for i in range(root_end):
        line = lines[i]
        if re.match(r'^[ \t]*model_provider[ \t]*=', line):
            lines[i] = 'model_provider = ' + json.dumps(target) + '\n'
            replaced_root = True
    if not replaced_root:
        lines.insert(0, 'model_provider = ' + json.dumps(target) + '\n')
    if old in providers:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith('[') or stripped.startswith('[['):
                continue
            try:
                table = tomllib.loads(stripped + '\n__stable_probe__ = true')
            except ValueError:
                continue
            node = table.get('model_providers', {}).get(old)
            if node is None:
                continue
            suffix = []
            while '__stable_probe__' not in node:
                if len(node) != 1:
                    raise StableProviderError('Ambiguous provider table.')
                key, node = next(iter(node.items()))
                suffix.append(key)
            keys = ['model_providers', target] + suffix
            lines[i] = '[' + '.'.join(json.dumps(k, ensure_ascii=False) for k in keys) + ']\n'
    else:
        section = expected['model_providers'][target]
        lines.append('\n[model_providers.' + target + ']\n')
        lines.extend(k + ' = ' + ('true' if v is True else json.dumps(v)) + '\n'
                     for k, v in section.items())
    result = ''.join(lines)
    if tomllib.loads(result) != expected:
        raise StableProviderError('Config preservation check failed; template was left untouched.')
    return result, old, result != text


def locate_db(requested):
    if requested:
        path = Path(requested).expanduser().resolve()
    else:
        path = Path(os.environ.get('CCSWITCH_HOME', Path.home() / '.cc-switch')) / 'cc-switch.db'
        path = path.resolve()
    if not path.is_file():
        raise StableProviderError(f'CC Switch database not found: {path}')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', help='CC Switch database path')
    parser.add_argument('--target', default='shared', help='Stable Codex history provider ID')
    parser.add_argument('--apply', action='store_true', help='Write the normalized templates')
    parser.add_argument('--allow-running', action='store_true', help='Allow writing while CC Switch is running')
    args = parser.parse_args(argv)
    try:
        db_path = locate_db(args.db)
    except StableProviderError as exc:
        parser.exit(1, f'Error: {exc}\n')
    if args.apply and ccswitch_running() and not args.allow_running:
        parser.exit(1, 'CC Switch is running. Close it before --apply, or explicitly pass --allow-running.\n')
    connection = sqlite3.connect(db_path, timeout=20)
    try:
        rows = connection.execute(
            "SELECT id, name, settings_config FROM providers WHERE app_type='codex' ORDER BY id"
        ).fetchall()
        updates = []
        conflicts = []
        for provider_id, name, settings_text in rows:
            settings = json.loads(settings_text)
            config = settings.get('config')
            if not isinstance(config, str):
                conflicts.append({'id': provider_id, 'name': name, 'reason': 'config is not text'})
                continue
            try:
                normalized, old, changed = normalize_config(config, args.target)
            except (StableProviderError, ValueError) as exc:
                conflicts.append({'id': provider_id, 'name': name, 'reason': str(exc)})
                continue
            if changed:
                settings['config'] = normalized
                updates.append((provider_id, settings_text, old, json.dumps(settings, ensure_ascii=False, separators=(',', ':'))))
        result = {
            'database': str(db_path), 'target_provider': args.target,
            'codex_templates': len(rows), 'changed': len(updates), 'conflicts': conflicts,
            'preview': not args.apply,
        }
        if args.apply and conflicts:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        if args.apply and updates:
            backup = db_path.parent / (
                'cc-switch.db.before-stable-provider-' +
                datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8] + '.bak'
            )
            saved = sqlite3.connect(backup)
            try:
                connection.backup(saved)
            finally:
                saved.close()
            try:
                connection.execute('BEGIN IMMEDIATE')
                for provider_id, original_settings, _old, settings in updates:
                    cursor = connection.execute(
                        "UPDATE providers SET settings_config=? WHERE id=? AND app_type='codex' AND settings_config=?",
                        (settings, provider_id, original_settings))
                    if cursor.rowcount != 1:
                        raise StableProviderError('CC Switch template changed during planning; transaction rolled back.')
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            result.update({'preview': False, 'backup': str(backup), 'updated_ids': [item[0] for item in updates]})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, StableProviderError) as exc:
        parser.exit(1, f'Error: {exc}\n')
    finally:
        connection.close()


if __name__ == '__main__':
    sys.exit(main())
