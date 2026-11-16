"""Real CLI and SQLite checks using synthetic sources; no live accounts or model calls."""
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / 'state' / 'test.sqlite'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('KB_')}
        self.env.update(KB_DB=str(self.db), HOME=str(self.base))
        self.notes = self.base / 'notes'
        self.notes.mkdir()

    def cli(self, script, *args, ok=True, **env):
        result = subprocess.run([sys.executable, str(ROOT / script), *args],
                                env={**self.env, **env}, cwd=self.base,
                                capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def ingest(self):
        return self.cli('ingest_all.py', '--only', 'files', KB_FILES_ROOTS=str(self.notes))

    def rows(self, sql='SELECT kind,source,title,content,metadata FROM docs'):
        with sqlite3.connect(self.db) as con:
            return con.execute(sql).fetchall()

    def test_notes_search_repeat_update_delete(self):
        note = self.notes / 'release.md'
        note.write_text('# Cobalt release\nUse the amber deployment window.\n')
        self.ingest()
        initial = self.rows()
        self.assertEqual(len(initial), 1)
        self.assertIn(str(note), self.cli('kb.py', 'search', 'cobalt release'))
        self.assertIn('0 items', self.ingest())
        self.assertEqual(self.rows(), initial)
        note.write_text('# Violet release\nUse the sapphire deployment window.\n')
        self.ingest()
        self.assertIn('0 results', self.cli('kb.py', 'search', 'cobalt amber'))
        self.assertIn('sapphire', self.cli('kb.py', 'search', 'sapphire'))
        note.unlink()
        self.ingest()
        self.assertEqual(self.rows(), [])

    def test_source_access_errors_are_failures(self):
        self.cli('ingest_all.py', '--only', 'files', ok=False)
        self.cli('ingest_all.py', '--only', 'files', ok=False,
                 KB_FILES_ROOTS=str(self.base / 'missing'))
        self.cli('ingest_all.py', '--only', 'unknown', ok=False)

    def test_file_symlinks_and_repository_internals_are_skipped(self):
        outside = self.base / 'outside.md'
        outside.write_text('unwanted private content')
        (self.notes / 'linked.md').symlink_to(outside)
        hidden = self.notes / '.git'
        hidden.mkdir()
        (hidden / 'internal.md').write_text('unwanted internal content')
        (self.notes / 'safe.md').write_text('safe visible note')
        self.ingest()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn('safe visible', rows[0][3])

    def test_redaction_and_one_result_per_original(self):
        secret = 'sk-' + 'xY2z' * 10
        (self.notes / 'long.md').write_text('# Cobalt release\n' +
            ('cobalt release detail ' * 500) + '\nAPI_KEY=' + secret)
        self.ingest()
        self.assertGreater(len(self.rows()), 1)
        self.assertNotIn(secret, str(self.rows()))
        self.assertIn('[MASKED]', str(self.rows()))
        self.assertIn('1 results', self.cli('kb.py', 'search', 'cobalt release detail'))
        self.assertIn('0 results', self.cli('kb.py', 'search', 'cobalt', '--source', 'absent'))
        self.assertIn('0 results', self.cli('kb.py', 'search', 'cobalt', '--after', '2999-01-01'))

    def test_spaced_session_json_and_duplicate_events(self):
        claude = self.base / 'claude'
        codex = self.base / 'codex'
        claude.mkdir()
        codex.mkdir()
        ts = '2026-01-01T00:00:00Z'
        cpath = claude / 'session.jsonl'
        cpath.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'user', 'timestamp': ts, 'cwd': '/demo', 'message': {'content': 'cobalt session question'}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Choose amber.'}]}},
            {'type': 'user', 'isMeta': True, 'message': {'content': 'injectedmeta forbidden'}}]))
        xpath = codex / 'rollout-demo.jsonl'
        xpath.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'session_meta', 'timestamp': ts, 'payload': {'timestamp': ts, 'cwd': '/demo', 'subagent_history_start_ordinal': 2}},
            {'type': 'response_item', 'ordinal': 1, 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'parenthistory forbidden'}]}},
            {'type': 'response_item', 'ordinal': 2, 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'violet session question'}]}},
            {'type': 'event_msg', 'ordinal': 3, 'payload': {'type': 'agent_message', 'message': 'eventecho forbidden'}}]))
        for path in (cpath, xpath):
            os.utime(path, (time.time() - 10000,) * 2)
        self.cli('ingest_all.py', '--only', 'claude_sessions,codex_sessions',
                 KB_CLAUDE_ROOT=str(claude), KB_CODEX_ROOT=str(codex))
        rows = self.rows()
        self.assertEqual({r[1] for r in rows}, {'claude', 'codex'})
        self.assertNotIn('forbidden', str(rows))
        self.assertIn('0 items', self.cli('ingest_all.py', '--only', 'claude_sessions,codex_sessions',
                 KB_CLAUDE_ROOT=str(claude), KB_CODEX_ROOT=str(codex)))

    def test_jsonl_import_and_invalid_batch_rollback(self):
        path = self.base / 'items.jsonl'
        item = {'id': 'demo', 'timestamp': '2026-01-01T00:00:00Z', 'title': 'Import',
                'content': 'cobalt imported', 'url': 'https://example.com/demo',
                'metadata': {'nested': {'password': 'demovalue'}, 'count': 7}}
        path.write_text(json.dumps(item) + '\n')
        env = {'KB_JSONL_PATH': str(path)}
        self.cli('ingest_all.py', '--only', 'jsonl', **env)
        initial = self.rows()
        self.assertEqual(len(initial), 1)
        self.assertNotIn('demovalue', str(initial))
        self.assertEqual(json.loads(initial[0][4])['count'], 7)
        self.assertIn('0 items', self.cli('ingest_all.py', '--only', 'jsonl', **env))
        item['content'] = 'violet replacement'
        path.write_text(json.dumps(item) + '\n{broken\n')
        self.cli('ingest_all.py', '--only', 'jsonl', ok=False, **env)
        self.assertEqual(self.rows(), initial)
        path.write_text(json.dumps(item) + '\n')
        self.cli('ingest_all.py', '--only', 'jsonl', **env)
        self.assertIn('violet replacement', str(self.rows()))

    def test_explicit_summary_bridge_and_invalid_output(self):
        (self.notes / 'long.md').write_text('# Cobalt release\n' + 'amber decision ' * 500)
        self.ingest()
        before = self.rows()
        self.cli('summarize.py', '--estimate')
        self.assertEqual(self.rows(), before)
        self.cli('summarize.py', ok=False)
        adapter = self.base / 'adapter.py'
        adapter.write_text('import json,sys\np=json.load(sys.stdin)\n'
            'assert "amber" in p["text"]\n'
            'print(json.dumps({"segments":[{"title":"Cobalt", "question":"release decision", '
            '"summary":"Use amber."}],"atoms":[{"type":"decision","text":"Amber window"}]}))\n')
        command = shlex.join([sys.executable, str(adapter)])
        self.cli('summarize.py', '--command', command, '--limit', '1')
        kinds = [row[0] for row in self.rows()]
        self.assertIn('segment', kinds)
        self.assertIn('atom', kinds)
        self.assertEqual(kinds.count('raw'), len(before))
        (self.notes / 'long.md').write_text('# New cobalt release\n' + 'amber choice ' * 500)
        self.ingest()
        adapter.write_text('print("{}")\n')
        self.cli('summarize.py', '--command', command, '--limit', '1', ok=False)
        self.assertTrue(all(row[0] == 'raw' for row in self.rows()))

    def test_changed_source_rejects_inflight_summary(self):
        note = self.notes / 'long.md'
        note.write_text('# Cobalt release\n' + 'amber decision ' * 500)
        self.ingest()
        adapter = self.base / 'racing_adapter.py'
        adapter.write_text(
            'import json,os,subprocess,sys\njson.load(sys.stdin)\n'
            f'path={str(note)!r}\n'
            'with open(path,"w") as f: f.write("# Changed\\n" + "violet choice " * 500)\n'
            f'os.environ["KB_FILES_ROOTS"]={str(self.notes)!r}\n'
            f'subprocess.run([sys.executable,{str(ROOT / "ingest_all.py")!r},"--only","files"],'
            'stdout=subprocess.DEVNULL,check=True)\n'
            'print(json.dumps({"segments":[{"title":"Stale", "question":"Old?", '
            '"summary":"Old answer."}],"atoms":[]}))\n')
        command = shlex.join([sys.executable, str(adapter)])
        output = self.cli('summarize.py', '--command', command, '--limit', '1', ok=False)
        self.assertIn('source changed', output)
        self.assertTrue(all(row[0] == 'raw' for row in self.rows()))
        self.assertNotIn('amber', str(self.rows()))


if __name__ == '__main__':
    unittest.main()
