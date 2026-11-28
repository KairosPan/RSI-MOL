"""Regression coverage for native Windows check reporting and positive/negative controls."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rsi.check import ALLOWED, BLOCKED, UNKNOWN, _result
from rsi.paths import Layout
from rsi import win_check, win_tool_checks


class WindowsCheckTests(unittest.TestCase):
    def test_exec_error_reaches_problem_summary_and_case_details(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            layout.meta.mkdir()
            call = layout.root / 'call'
            call.mkdir()
            (call / 'stderr.txt').write_text('unrelated startup warning\n', encoding='utf-8')
            (call / 'events.jsonl').write_text(
                'incomplete\n{"type":"turn.failed","error":{"message":"bad test configuration"}}\n'
                '{"type":"item.completed","message":"ordinary tool output"}\n', encoding='utf-8')
            problems = []
            with patch.object(win_tool_checks, 'meta_config', return_value={}), \
                    patch.object(win_tool_checks, 'ScriptedModel') as model, \
                    patch.object(win_tool_checks, 'run_codex', return_value=SimpleNamespace(status='failed', exit_code=1)):
                model.return_value.start.return_value = 'http://127.0.0.1:1'
                model.return_value.requests = []
                rows = win_tool_checks._exec_checks(layout, layout, 'meta', {}, 'codex', problems)
            self.assertEqual(len(problems), 1)
            self.assertIn('meta tool check', problems[0])
            self.assertIn('bad test configuration', problems[0])
            self.assertTrue(all(row['observed'] == UNKNOWN and 'bad test configuration' in row['detail']
                                for row in rows))

    def test_cli_startup_failure_without_events_retains_bounded_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'stderr.txt').write_text(
                'old warning\n' + 'z' * 20000 + '\nError: invalid config\nCaused by: ' + 'q' * 2400, encoding='utf-8')
            reason = win_tool_checks._failure_message(folder)
            self.assertTrue(reason.startswith('Error: invalid config Caused by: '))
            self.assertLessEqual(len(reason), 600)

    def test_expected_access_matches_role(self):
        self.assertEqual(win_check._expected('lab')['write_env'], ALLOWED)
        self.assertEqual(win_check._expected('trial')['read_env'], ALLOWED)
        self.assertEqual(win_check._expected('trial_net')['write_env'], BLOCKED)
        self.assertEqual(win_check._expected('meta')['write_candidate'], ALLOWED)
        for profile in win_check.PROFILES:
            self.assertEqual(win_check._expected(profile)['write_records'], BLOCKED)
            self.assertEqual(win_check._expected(profile)['write_private_temp'], ALLOWED)

    def test_gate_requires_every_expected_result(self):
        required = [('write_work', 'lab', ALLOWED), ('write_work', 'trial', BLOCKED)]
        rows = [_result(name, profile, expected, expected) for name, profile, expected in required]
        with patch.object(win_check, 'required_cases', return_value=required):
            self.assertTrue(win_check.covers_required(rows))
            self.assertFalse(win_check.covers_required(rows[:1]))
            self.assertFalse(win_check.covers_required(rows + [_result('extra', 'lab', BLOCKED, UNKNOWN)]))
            self.assertFalse(win_check.covers_required([dict(rows[0], observed=UNKNOWN), rows[1]]))
            self.assertFalse(win_check.covers_required(None))

    def test_offline_scope_measures_network_request_without_enabling_net_profiles(self):
        offline, full = set(win_check.required_cases(True)), set(win_check.required_cases(False))
        self.assertIn(('requested_network', 'lab', BLOCKED), offline)
        self.assertIn(('local_tcp', 'lab_net', BLOCKED), full)
        self.assertFalse(any(profile.endswith('_net') for _, profile, _ in offline))
        rows = [_result(name, profile, expected, expected) for name, profile, expected in offline]
        self.assertTrue(win_check.covers_required(rows, offline=True))
        self.assertFalse(win_check.covers_required(rows, offline=False))

    @unittest.skipUnless(os.name == 'nt', 'Windows host identity')
    def test_moved_installation_requires_new_check(self):
        with tempfile.TemporaryDirectory() as folder:
            original = win_check.check_scope(Layout(Path(folder)))
            self.assertNotEqual(original, win_check.check_scope(Layout(Path(folder) / 'another')))

    @unittest.skipUnless(os.name == 'nt', 'native Windows scratch layout')
    def test_startup_failure_is_not_a_denial(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Layout(Path(folder))
            unreachable = _result('read_owner', 'lab', BLOCKED, UNKNOWN, 'sandbox did not start')
            required = [('read_owner', 'lab', BLOCKED), ('read_owner', 'meta', BLOCKED)]
            with patch.object(win_check, 'native_codex', return_value='codex.exe'), \
                    patch.object(win_check, '_profile', return_value=[unreachable]) as probe, \
                    patch.object(win_check, 'required_cases', return_value=required):
                problems = []
                rows = win_check.run_windows_cases(root, {}, problems, [])
                self.assertEqual(probe.call_count, 1)
                self.assertTrue(problems)
                self.assertEqual([row['observed'] for row in rows], [UNKNOWN, UNKNOWN])
                self.assertEqual(list(root.staging.iterdir()), [])
