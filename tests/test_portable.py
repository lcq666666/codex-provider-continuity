from contextlib import redirect_stdout, closing
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ccswitch_stable as cc
import portable
import provider_history as history


class ConfigTests(unittest.TestCase):
    def test_quoted_single_quote_and_child_tables_preserve_connection(self):
        text = """model_provider = 'legacy' # a comment
model = "sample"
["model_providers"."legacy"]
base_url = "https://example.invalid/v1"
env_key = "EXAMPLE_KEY"
["model_providers"."legacy"."http_headers"]
Example = "dummy"
[mcp_servers.test]
command = "unchanged"
"""
        result, old, changed = cc.normalize_config(text, 'shared')
        self.assertEqual(old, 'legacy')
        self.assertTrue(changed)
        before, after = tomllib.loads(text), tomllib.loads(result)
        self.assertEqual(before['model_providers']['legacy'], after['model_providers']['shared'])
        self.assertEqual(before['mcp_servers'], after['mcp_servers'])
        self.assertFalse(cc.normalize_config(result, 'shared')[2])

    def test_builtin_default_without_root_gets_explicit_official_api_route(self):
        result, old, changed = cc.normalize_config('model = "example"\n', 'shared')
        cfg = tomllib.loads(result)
        self.assertEqual(old, 'openai')
        self.assertTrue(changed)
        self.assertEqual(cfg['model_providers']['shared']['base_url'], 'https://api.openai.com/v1')

    def test_unknown_custom_provider_collision_and_profile_refused(self):
        cases = ['model_provider="unknown"\n',
                 'profile="work"\n',
                 'model_provider="old"\n[model_providers.old]\n[model_providers.shared]\n']
        for text in cases:
            with self.subTest(text=text), self.assertRaises(cc.StableProviderError):
                cc.normalize_config(text, 'shared')


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'different user with spaces'
        self.home.mkdir()
        self.config = self.home / 'config.toml'
        self.config.write_text('model_provider="legacy"\n[model_providers.legacy]\n'
                               'base_url="https://example.invalid/v1"\n', encoding='utf-8')
        self.auth = self.home / 'auth.json'
        self.auth.write_bytes(b'{"OPENAI_API_KEY":"synthetic"}')
        (self.home / 'sqlite').mkdir()
        self.db = self.home / 'sqlite' / 'state_5.sqlite'
        self.rollout = self.home / 'thread.jsonl.zst'
        meta = {'type': 'session_meta', 'payload': {'id': 'thread', 'model_provider': 'legacy'}}
        self.plain = (json.dumps(meta).encode() + b'\n' + b'{"type":"event_msg","text":"history"}\n')
        self.rollout.write_bytes(history.pack(self.rollout, self.plain))
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, rollout_path TEXT, archived INTEGER)')
            con.execute('INSERT INTO threads VALUES (?,?,?,1)', ('thread', 'legacy', str(self.rollout)[:-4]))
        self.ccdb = self.home / 'cc-switch.db'
        with closing(sqlite3.connect(self.ccdb)) as con, con:
            con.execute('CREATE TABLE providers(id TEXT,app_type TEXT,name TEXT,settings_config TEXT, PRIMARY KEY(id,app_type))')
            for app in ['codex', 'claude']:
                settings = json.dumps({'auth': {'example': 'credential'}, 'config': self.config.read_text()})
                con.execute('INSERT INTO providers VALUES (?,?,?,?)', ('same-id', app, 'Synthetic', settings))

    def run_quiet(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return portable.execute('setup', self.home, cc_db=str(self.ccdb), **kwargs)

    def test_preview_then_apply_keeps_auth_and_other_app_and_restores_path(self):
        before_config, before_rollout = self.config.read_bytes(), self.rollout.read_bytes()
        with closing(sqlite3.connect(self.ccdb)) as con:
            other = con.execute("SELECT settings_config FROM providers WHERE app_type='claude'").fetchone()[0]
        self.run_quiet()
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(self.rollout.read_bytes(), before_rollout)
        self.assertFalse((self.home / 'backups').exists())
        with patch.object(portable, 'running_writers', return_value=[]), patch.object(cc, 'ccswitch_running', return_value=False):
            self.run_quiet(apply=True)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT model_provider,rollout_path,archived FROM threads').fetchone(),
                             ('shared', str(self.rollout.resolve()), 1))
        with closing(sqlite3.connect(self.ccdb)) as con:
            self.assertEqual(con.execute("SELECT settings_config FROM providers WHERE app_type='claude'").fetchone()[0], other)
            settings = json.loads(con.execute("SELECT settings_config FROM providers WHERE app_type='codex'").fetchone()[0])
            self.assertEqual(settings['auth'], {'example': 'credential'})
        self.assertEqual(self.auth.read_bytes(), b'{"OPENAI_API_KEY":"synthetic"}')
        self.assertEqual(history.unpack(self.rollout, self.rollout.read_bytes()).partition(b'\n')[2], self.plain.partition(b'\n')[2])
        manifest = next((self.home / 'backups').glob('*/manifest.json'))
        with redirect_stdout(io.StringIO()):
            history.main(['--home', str(self.home), '--restore', str(manifest), '--apply', '--offline'])
        self.assertEqual(self.rollout.read_bytes(), before_rollout)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT model_provider,rollout_path FROM threads').fetchone(),
                             ('legacy', str(self.rollout)[:-4]))

    def test_running_apps_block_all_writes(self):
        before = self.config.read_bytes()
        with patch.object(portable, 'running_writers', return_value=['codex.exe']):
            with self.assertRaisesRegex(ValueError, 'Close these apps'):
                self.run_quiet(apply=True)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.home / 'backups').exists())

    def test_cc_backup_captures_wal_and_only_codex_is_updated(self):
        with closing(sqlite3.connect(self.ccdb)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE providers SET name='Committed WAL' WHERE app_type='codex'")
            writer.commit()
            with patch.object(cc, 'ccswitch_running', return_value=False), redirect_stdout(io.StringIO()):
                self.assertEqual(cc.main(['--db', str(self.ccdb), '--apply']), 0)
            saved = next(self.home.glob('cc-switch.db.before-stable-provider-*.bak'))
            with closing(sqlite3.connect(saved)) as con:
                self.assertEqual(con.execute("SELECT name FROM providers WHERE app_type='codex'").fetchone()[0], 'Committed WAL')


if __name__ == '__main__':
    unittest.main()
