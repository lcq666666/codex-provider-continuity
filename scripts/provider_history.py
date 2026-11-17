#!/usr/bin/env python3
"""Offline, backed-up Codex provider metadata migration. Python 3.11+."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import tomllib
import uuid


class MigrationError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def codec():
    try:
        from compression import zstd
        return zstd
    except ImportError:
        try:
            import zstandard
            return zstandard
        except ImportError as exc:
            raise MigrationError(
                "Compressed rollout needs Python 3.14+ or pip install zstandard."
            ) from exc


def unpack(path, data):
    if path.suffix != '.zst':
        return data
    module = codec()
    # Stream readers also support frames without a declared decompressed size.
    import io
    if hasattr(module, 'ZstdFile'):
        with module.ZstdFile(io.BytesIO(data), 'rb') as stream:
            return stream.read()
    with module.ZstdDecompressor().stream_reader(io.BytesIO(data)) as stream:
        return stream.read()


def pack(path, data):
    if path.suffix != '.zst':
        return data
    module = codec()
    if hasattr(module, 'compress'):
        return module.compress(data)
    return module.ZstdCompressor().compress(data)


def header(plain, thread_id):
    first, newline, body = plain.partition(b'\n')
    try:
        meta = json.loads(first)
        valid = (meta.get('type') == 'session_meta'
                 and meta['payload']['id'] == thread_id)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise MigrationError(f'Invalid session metadata for {thread_id}') from exc
    if not valid:
        raise MigrationError(f'Session identity mismatch for {thread_id}')
    return meta, first, newline, body


def rewrite(path, data, thread_id, target):
    plain = unpack(path, data)
    meta, first, newline, body = header(plain, thread_id)
    old = meta['payload'].get('model_provider')
    if old == target:
        return data, old
    meta['payload']['model_provider'] = target
    replacement = json.dumps(meta, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(replacement) > len(first):
        raise MigrationError(
            f'Provider name does not fit metadata header for {thread_id}. '
            'Choose a shorter provider ID. History byte offsets must stay valid.'
        )
    replacement = replacement.ljust(len(first), b' ')
    return pack(path, replacement + newline + body), old


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_manifest(folder, manifest):
    atomic_write(folder / 'manifest.json', json.dumps(manifest, indent=2).encode('utf-8'))


def database(home, requested):
    if requested:
        path = Path(requested).expanduser().resolve()
    else:
        paths = [p for root in (home, home / 'sqlite') for p in root.glob('state_*.sqlite')
                 if re.fullmatch(r'state_\d+\.sqlite', p.name)]
        if len(paths) != 1:
            raise MigrationError('Expected one state_*.sqlite database. Select it with --db.')
        path = paths[0].resolve()
    if not path.is_file():
        raise MigrationError(f'Database does not exist: {path}')
    return path


@contextmanager
def connect(path, write=False):
    connection = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                                 uri=True, timeout=10)
    columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
    if not {'id', 'model_provider', 'rollout_path'} <= columns:
        connection.close()
        raise MigrationError('Unsupported threads schema. Expected id, model_provider, rollout_path.')
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def rollout_candidates(home, raw):
    """Return safe path alternatives for legacy/Windows rollout references."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = home / path
    text = str(path)
    # Prefer the normal Windows spelling so repaired rows do not retain a
    # `\\?\` prefix that some Codex builds cannot reopen.
    normalized = text[4:] if text.startswith('\\\\?\\') else text
    bases = [Path(normalized)]
    if Path(normalized).suffix == '.jsonl':
        bases.append(Path(normalized + '.zst'))
    elif Path(normalized).suffix == '.zst':
        bases.append(Path(normalized[:-4]))
    candidates = bases + [path]
    if path.suffix == '.jsonl':
        candidates.append(Path(str(path) + '.zst'))
    unique = []
    seen = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.normpath(str(candidate)))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def resolve_rollout(home, raw):
    for candidate in rollout_candidates(home, raw):
        if candidate.is_symlink():
            raise MigrationError(f'Symlink rollout is unsupported: {candidate}')
        if candidate.is_file() and candidate.name.endswith(('.jsonl', '.jsonl.zst')):
            return candidate.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = home / path
    raise MigrationError(f'Rollout missing: {path}. No safe .jsonl/.jsonl.zst alternative was found.')


def plan(connection, home, args):
    rows = connection.execute('SELECT id, model_provider, rollout_path FROM threads ORDER BY id').fetchall()
    if args.thread:
        rows = [row for row in rows if row[0] == args.thread]
        if not rows:
            raise MigrationError(f'Thread not found: {args.thread}')
    excluded = set(getattr(args, 'exclude_thread', []) or [])
    rows = [row for row in rows if row[0] not in excluded]
    entries = []
    seen = set()
    for thread_id, provider, raw_path in rows:
        path = resolve_rollout(home, raw_path)
        if path in seen:
            raise MigrationError(f'Multiple selected threads share a rollout: {path}')
        seen.add(path)
        original = path.read_bytes()
        modified, metadata_provider = rewrite(path, original, thread_id, args.target)
        path_after = str(path)
        if provider == args.target and original == modified and str(raw_path) == path_after:
            continue
        entries.append({
            'id': thread_id, 'provider_before': provider, 'metadata_provider_before': metadata_provider,
            'rollout_path_before': raw_path, 'rollout_path_after': path_after, 'path': path_after,
            'before_sha256': digest(original), 'after_sha256': digest(modified),
        })
    return entries, len(rows)


def doctor(connection, home, target):
    """Inspect indexed chats without changing files or the database."""
    rows = connection.execute(
        'SELECT id, model_provider, rollout_path, archived FROM threads ORDER BY id'
    ).fetchall()
    report = {
        'scanned': len(rows),
        'target_provider': target,
        'provider_mismatches': [],
        'metadata_mismatches': [],
        'path_repairs': [],
        'missing_rollouts': [],
        'archived_scanned': sum(bool(row[3]) for row in rows),
    }
    for thread_id, provider, raw_path, _archived in rows:
        try:
            path = resolve_rollout(home, raw_path)
        except MigrationError:
            report['missing_rollouts'].append({'id': thread_id, 'path': raw_path})
            continue
        if str(raw_path) != str(path):
            report['path_repairs'].append({'id': thread_id, 'from': raw_path, 'to': str(path)})
        try:
            meta, _first, _newline, _body = header(unpack(path, path.read_bytes()), thread_id)
        except (MigrationError, OSError, ValueError) as exc:
            report['missing_rollouts'].append({'id': thread_id, 'path': str(path), 'error': str(exc)})
            continue
        metadata_provider = meta['payload'].get('model_provider')
        if provider != target:
            report['provider_mismatches'].append({'id': thread_id, 'from': provider, 'to': target})
        if metadata_provider != target:
            report['metadata_mismatches'].append({'id': thread_id, 'from': metadata_provider, 'to': target})
    affected = ({item['id'] for item in report['provider_mismatches']}
                | {item['id'] for item in report['metadata_mismatches']}
                | {item['id'] for item in report['path_repairs']})
    report['affected_threads'] = len(affected)
    report['changes'] = len(affected)
    return report


@contextmanager
def migration_lock(home):
    path = home / '.provider-history.lock'
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise MigrationError(
            f'Migration lock exists: {path}. If a previous process crashed, '
            'confirm it has stopped and remove this lock before recovery.'
        ) from exc
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def row_state(connection, entry):
    row = connection.execute('SELECT model_provider, rollout_path FROM threads WHERE id=?',
                             (entry['id'],)).fetchone()
    if row is None or row[1] != entry['rollout_path_before']:
        raise MigrationError(f'Thread or rollout path changed: {entry["id"]}')
    return row[0]


def row_rollout(connection, entry):
    row = connection.execute('SELECT rollout_path FROM threads WHERE id=?',
                             (entry['id'],)).fetchone()
    if row is None:
        raise MigrationError(f'Thread not found: {entry["id"]}')
    return row[0]


def migrate(connection, home, db_path, args, entries, config_data):
    folder = home / 'backups' / (
        'provider-migration-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    )
    folder.mkdir(parents=True, mode=0o700)
    manifest = {'version': 1, 'home': str(home), 'database': str(db_path),
                'target_provider': args.target, 'status': 'preparing', 'entries': entries}
    save_manifest(folder, manifest)
    # SQLite backup captures committed WAL content as well as the main database.
    backup = sqlite3.connect(folder / 'database.sqlite')
    try:
        connection.backup(backup)
    finally:
        backup.close()
    for index, entry in enumerate(entries):
        path = Path(entry['path'])
        original = path.read_bytes()
        if digest(original) != entry['before_sha256']:
            raise MigrationError(f'Rollout changed during planning: {entry["id"]}')
        modified, _ = rewrite(path, original, entry['id'], args.target)
        if digest(modified) != entry['after_sha256']:
            raise MigrationError(f'Inconsistent staged content: {entry["id"]}')
        entry['backup_file'] = f'{index:06d}.before'
        entry['staged_file'] = f'{index:06d}.after'
        (folder / entry['backup_file']).write_bytes(original)
        (folder / entry['staged_file']).write_bytes(modified)
    manifest['status'] = 'prepared'
    save_manifest(folder, manifest)
    print(json.dumps({'backup': str(folder)}, ensure_ascii=False), flush=True)
    replaced = []
    try:
        connection.execute('BEGIN IMMEDIATE')
        if (home / 'config.toml').read_bytes() != config_data:
            raise MigrationError('Configuration changed during planning. Run the preview again.')
        for entry in entries:
            path = Path(entry['path'])
            if row_state(connection, entry) != entry['provider_before']:
                raise MigrationError(f'Thread provider changed: {entry["id"]}')
            if digest(path.read_bytes()) != entry['before_sha256']:
                raise MigrationError(f'Rollout changed before apply: {entry["id"]}')
            atomic_write(path, (folder / entry['staged_file']).read_bytes())
            replaced.append(entry)
            connection.execute('UPDATE threads SET model_provider=?, rollout_path=? WHERE id=?',
                               (args.target, entry.get('rollout_path_after', entry['rollout_path_before']), entry['id']))
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise MigrationError('SQLite integrity check failed.')
        connection.commit()
    except BaseException:
        connection.rollback()
        # Never overwrite bytes written by another process after our replacement.
        for entry in reversed(replaced):
            path = Path(entry['path'])
            if digest(path.read_bytes()) != entry['after_sha256']:
                raise MigrationError(f'Concurrent rollout edit. Recover using {folder / "manifest.json"}')
            atomic_write(path, (folder / entry['backup_file']).read_bytes())
        manifest['status'] = 'rolled_back'
        save_manifest(folder, manifest)
        raise
    manifest['status'] = 'applied'
    save_manifest(folder, manifest)
    return {'migrated': len(entries), 'target_provider': args.target, 'backup': str(folder)}


def restore(home, manifest_path):
    folder = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or Path(manifest['home']).resolve() != home:
        raise MigrationError('Manifest version or CODEX_HOME mismatch.')
    if manifest['status'] == 'preparing':
        raise MigrationError('Backup preparation was incomplete. No live changes were started.')
    with migration_lock(home), connect(Path(manifest['database']), write=True) as connection:
        connection.execute('BEGIN IMMEDIATE')
        # Preflight the entire recovery before replacing anything. Accept either side
        # of a crash between a file replacement and the database commit.
        for entry in manifest['entries']:
            current = digest(Path(entry['path']).read_bytes())
            if current not in {entry['before_sha256'], entry['after_sha256']}:
                raise MigrationError(f'Rollout has newer edits. Recovery stopped: {entry["id"]}')
            saved = (folder / entry['backup_file']).resolve()
            if saved.parent != folder.resolve() or digest(saved.read_bytes()) != entry['before_sha256']:
                raise MigrationError('Backup file is missing, altered, or outside the backup folder.')
            provider_row = connection.execute('SELECT model_provider FROM threads WHERE id=?',
                                              (entry['id'],)).fetchone()
            if provider_row is None or provider_row[0] not in {entry['provider_before'], manifest['target_provider']}:
                raise MigrationError(f'Provider changed since migration: {entry["id"]}')
            current_path = row_rollout(connection, entry)
            if current_path not in {entry['rollout_path_before'], entry.get('rollout_path_after', entry['rollout_path_before'])}:
                raise MigrationError(f'Rollout path changed since migration: {entry["id"]}')
        for entry in manifest['entries']:
            path = Path(entry['path'])
            if digest(path.read_bytes()) not in {entry['before_sha256'], entry['after_sha256']}:
                raise MigrationError(f'Rollout changed during recovery: {entry["id"]}')
            atomic_write(path, (folder / entry['backup_file']).read_bytes())
            connection.execute('UPDATE threads SET model_provider=?, rollout_path=? WHERE id=?',
                               (entry['provider_before'], entry['rollout_path_before'], entry['id']))
        connection.commit()
    manifest['status'] = 'restored'
    save_manifest(folder, manifest)
    return {'restored': len(manifest['entries']), 'manifest': str(manifest_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    parser.add_argument('--db', help='Explicit state database path when discovery is ambiguous')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--all', action='store_true', help='Include active and archived indexed threads')
    mode.add_argument('--thread', help='Select one existing thread ID')
    mode.add_argument('--doctor', action='store_true', help='Diagnose provider and rollout index drift')
    mode.add_argument('--restore', type=Path, help='Restore using a local manifest.json')
    parser.add_argument('--target', help='Provider ID. Defaults to config.toml model_provider or openai')
    parser.add_argument('--exclude-thread', action='append', default=[], help='Skip a live thread during a bulk migration; may be repeated')
    parser.add_argument('--apply', action='store_true', help='Write migration or restoration. Default is preview')
    parser.add_argument('--offline', action='store_true', help='Confirm all Codex writers for this home are stopped')
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    try:
        if args.apply and not args.offline:
            raise MigrationError('Stop Codex desktop, CLI and app-server writers, then pass --offline.')
        if args.restore:
            if args.target or args.db:
                raise MigrationError('--restore uses the database and providers recorded in its manifest.')
            if not args.apply:
                return {'restore_manifest': str(args.restore.resolve()), 'preview': True}
            return restore(home, args.restore.resolve())
        config_data = (home / 'config.toml').read_bytes()
        config = tomllib.loads(config_data.decode('utf-8-sig'))
        if args.target is None:
            # Profile and CLI overrides are intentionally explicit, since a local
            # file alone cannot tell which launch-time settings are in use.
            if config.get('profile'):
                raise MigrationError('An active profile is configured. Pass its effective --target provider ID.')
            args.target = config.get('model_provider', 'openai')
        if not isinstance(args.target, str) or not args.target.strip():
            raise MigrationError('Target provider must be a non-empty string.')
        db_path = database(home, args.db)
        if args.doctor:
            with connect(db_path) as connection:
                return doctor(connection, home, args.target)
        if args.apply:
            with migration_lock(home), connect(db_path, write=True) as connection:
                entries, scanned = plan(connection, home, args)
                if entries:
                    return migrate(connection, home, db_path, args, entries, config_data)
        else:
            with connect(db_path) as connection:
                entries, scanned = plan(connection, home, args)
        return {'preview': not args.apply, 'scanned': scanned, 'changes': len(entries),
                'target_provider': args.target, 'threads': [entry['id'] for entry in entries]}
    except (MigrationError, OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    print(json.dumps(main(), ensure_ascii=False, indent=2))
