import json
import os
import unittest
from pathlib import Path
from unittest import mock

from rsi import cli
from rsi.check import CHECK_FILE, gate, owner_fingerprints, run_check
from rsi.codexrun import codex_version
from rsi.labrun import run_task
from rsi.meta import run_round
from rsi.paths import Refused
from tests.support import Root, needs_codex
from tests.test_cli import run

WANTED = (('lab', 'read_records'), ('lab_net', 'local_tcp'), ('lab_net', 'local_unix_socket'), ('trial', 'write_work'),
          ('meta', 'write_records'), ('lab', 'edit_via_symlink'), ('meta', 'edit_existing_record'),
          ('trial', 'write_env'), ('meta', 'replace_own_root'), ('lab', 'dot_codex_link'), ('meta', 'dot_codex_link'),
          ('meta', 'meta_agents_md_not_loaded'), ('lab', 'lab_agents_md_loaded'),
          ('trial_net', 'write_library'), ('meta', 'write_candidate'), ('lab', 'write_user_temp'),
          ('meta', 'zsh_heredoc'), ('lab', 'tool_server_wrapper'), ('lab_net', 'tool_server_wrapper'),
          ('lab', 'delete_existing_record'), ('meta', 'edit_dot_codex'), ('lab', 'edit_dot_codex'),
          ('lab', 'edit_dot_agents'), ('meta', 'edit_dot_agents'))
# Every create case tries mkdir, rename-to and a link, for three letter cases, in every writable root of its profile.
CREATE_ATTEMPTS = {'lab': 9, 'lab_net': 9, 'trial': 9, 'trial_net': 9, 'meta': 18}
INFO = {('meta', 'meta_planted_skill_loaded'), ('lab', 'lab_can_create_dot_agents'),
        ('lab', 'lab_can_write_in_existing_dot_agents')}


def tree(folder: Path) -> set[str]:
    return {path.relative_to(folder).as_posix() for path in folder.rglob('*')}


def scratch_left(root: Root) -> list[str]:
    staging = root.layout.staging
    return [name for name in os.listdir(staging) if name.startswith('check-')] if staging.exists() else []


def sockets_left() -> set[str]:
    return {name for name in os.listdir('/tmp') if name.startswith('rsi-check-')}


@needs_codex
class Check(unittest.TestCase):
    def test_a_fresh_root_passes(self):
        home, sockets = set(os.listdir(Path.home())), sockets_left()
        with Root(passed_check=False) as root:
            self.assertIsNotNone(gate(root.layout))
            work, records = tree(root.layout.work), tree(root.layout.records)
            report = run_check(root.layout)
            failed = [(r['profile'], r['name'], r['observed'], r['detail']) for r in report['results'] if not r['ok']]
            self.assertEqual(failed, [])
            self.assertEqual(report['problems'], []); self.assertTrue(report['passed'])
            self.assertEqual(report['codex_version'], codex_version())
            names = {(r['profile'], r['name']) for r in report['results']}
            for wanted in WANTED:
                self.assertIn(wanted, names)
            self.assertTrue(all(r['expected'] in ('allowed', 'blocked') for r in report['results']))
            for profile in CREATE_ATTEMPTS:
                for name in ('create_dot_codex', 'create_dot_agents'):
                    [r] = [r for r in report['results'] if (r['profile'], r['name']) == (profile, name)]
                    self.assertEqual(r['detail'].count(' blocked'), CREATE_ATTEMPTS[profile], r)
            self.assertEqual({(i['profile'], i['name']) for i in report['info']}, INFO)
            self.assertTrue(all(i['value'] in (True, False, None) for i in report['info']))
            self.assertEqual(report['owner_files'], owner_fingerprints(root.layout))
            self.assertIsNone(report['owner_files']['meta/settings.toml'])
            self.assertIsNone(gate(root.layout))
            self.assertEqual(json.loads((root.layout.records / CHECK_FILE).read_text()), report)
            self.assertEqual([p.name for p in root.layout.records.iterdir() if p.name.startswith('.rsi-check')], [])
            # The check worked in a scratch root that is gone now, and left the real folders alone.
            self.assertEqual(scratch_left(root), [])
            self.assertEqual(tree(root.layout.work), work)
            self.assertEqual(tree(root.layout.records) - records, {CHECK_FILE, 'index.jsonl'})
            lines = (root.layout.records / 'index.jsonl').read_text().splitlines()
            self.assertEqual([json.loads(line)['kind'] for line in lines], ['check'])
        self.assertEqual(sockets_left(), sockets)
        new = set(os.listdir(Path.home())) - home
        self.assertEqual([name for name in new if 'rsi' in name.lower() or 'codex' in name.lower()], [])

    def test_a_hole_is_found(self):
        with Root(passed_check=False) as root, mock.patch('rsi.compose.root_rule', return_value='read'):
            report = run_check(root.layout)
            self.assertFalse(report['passed'])
            self.assertIn(('lab', 'read_records', 'allowed'), [(r['profile'], r['name'], r['observed'])
                                                              for r in report['results']])
            self.assertIsNotNone(gate(root.layout))
            self.assertEqual(scratch_left(root), [])


@needs_codex
class Gate(unittest.TestCase):
    def test_gate_follows_the_codex_version(self):
        with Root() as root:
            self.assertIsNone(gate(root.layout))
            path = root.layout.records / CHECK_FILE
            data = json.loads(path.read_text()); data['codex_version'] = '0.0.1'
            path.write_text(json.dumps(data))
            self.assertIn('rsi check', gate(root.layout))
            root.add_task('T1')
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            with self.assertRaises(Refused): run_round(root.layout, [])
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))

    def test_no_check_a_failed_check_or_an_unreadable_one_stops_every_agent(self):
        with Root(passed_check=False) as root:
            root.add_task('T1')
            path = root.layout.records / CHECK_FILE
            failed = {'codex_version': codex_version(), 'passed': False, 'at': 'x', 'results': [], 'problems': []}
            for content in (None, json.dumps(failed), '{not json', '[]', json.dumps({**failed, 'passed': 'yes'})):
                if content is not None:
                    path.write_text(content)
                reason = gate(root.layout)
                self.assertIn('rsi check', reason, content)
                with self.assertRaises(Refused, msg=content) as refusal:
                    run_task(root.layout, 'T1')
                self.assertIn('rsi check', str(refusal.exception))
                with self.assertRaises(Refused, msg=content):
                    run_round(root.layout, [])
            path.unlink(); path.symlink_to(root.layout.owner / 'limits.toml')      # never followed
            self.assertIn('rsi check', gate(root.layout))
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))

    def test_gate_notices_when_the_owner_files_change(self):
        with Root() as root:
            root.add_task('T1')
            self.assertIsNone(gate(root.layout))
            envelope, settings = root.layout.owner / 'lab-envelope.toml', root.layout.owner / 'meta' / 'settings.toml'
            before = envelope.read_text()
            envelope.write_text(before + '# 负责人改过\n')
            reason = gate(root.layout)
            self.assertIn('owner/lab-envelope.toml', reason); self.assertIn('rsi check', reason)
            with self.assertRaises(Refused): run_task(root.layout, 'T1')
            envelope.write_text(before)
            self.assertIsNone(gate(root.layout))
            settings.parent.mkdir(exist_ok=True); settings.write_text('model_reasoning_effort = "low"\n')
            self.assertIn('owner/meta/settings.toml', gate(root.layout))          # absent when checked, there now
            with self.assertRaises(Refused): run_round(root.layout, [])
            settings.unlink()
            self.assertIsNone(gate(root.layout))
            path = root.layout.records / CHECK_FILE
            data = json.loads(path.read_text()); del data['owner_files']; path.write_text(json.dumps(data))
            self.assertIn('rsi check', gate(root.layout))                          # a check that did not record them
            self.assertEqual((root.records.calls(), root.model.requests), ([], []))


class Command(unittest.TestCase):
    RESULT = {'name': 'read_records', 'profile': 'lab', 'expected': 'blocked', 'observed': 'blocked', 'ok': True,
              'detail': ''}

    def report(self, *results, problems=()):
        return {'codex_version': '9.9.9', 'owner_files': {}, 'passed': all(r['ok'] for r in results) and not problems,
                'at': 'x', 'results': list(results), 'problems': list(problems), 'notes': [],
                'info': [{'name': 'meta_planted_skill_loaded', 'profile': 'meta', 'value': True, 'detail': 'd'}]}

    def test_check_prints_each_failed_case_or_all_passed(self):
        with Root(passed_check=False) as root:
            with mock.patch.object(cli, 'run_check', return_value=self.report(self.RESULT)) as check:
                code, out, _ = run('check')
            self.assertEqual((code, check.call_args.args[0].root), (0, root.layout.root))
            self.assertIn('全部通过', out); self.assertIn('meta meta_planted_skill_loaded = 是', out)
            hole = dict(self.RESULT, observed='allowed', ok=False, detail='读到了\x1b[2J')
            gone = dict(self.RESULT, name='local_tcp', profile='lab_net', observed='unknown', ok=False, detail='超时')
            with mock.patch.object(cli, 'run_check', return_value=self.report(self.RESULT, hole, gone,
                                                                              problems=['根路径不合规'])):
                code, out, _ = run('check')
            self.assertEqual(code, 1); self.assertNotIn('全部通过', out); self.assertNotIn('\x1b', out)
            failed = [line for line in out.splitlines() if 'read_records' in line or 'local_tcp' in line]
            self.assertEqual(len(failed), 2, out)
            self.assertIn('lab read_records', failed[0]); self.assertIn('lab_net local_tcp', failed[1])
            self.assertIn('根路径不合规', out)

    def test_status_shows_the_check(self):
        with Root(passed_check=False) as root:
            self.assertIsNone(json.loads(run('status', '--json')[1])['check'])
            self.assertIn('隔离检查：还没有运行 rsi check', run('status')[1])
            (root.layout.records / CHECK_FILE).write_text(json.dumps(self.report(self.RESULT)))
            check = json.loads(run('status', '--json')[1])['check']
            self.assertEqual((check['codex_version'], check['passed']), ('9.9.9', True))
            self.assertIn('隔离检查：Codex 9.9.9，通过', run('status')[1])
            (root.layout.records / CHECK_FILE).write_text('{not json')
            self.assertIn('隔离检查：', run('status')[1])


if __name__ == '__main__':
    unittest.main()
