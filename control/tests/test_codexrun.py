import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from rsi.codexrun import _stop, codex_version, run_codex
from rsi.mockmodel import (ScriptedModel, add_file, delete_file, hang_up, provider_config, run_command, run_js, say,
                           slow, with_usage)
from tests.support import needs_codex, scratch_config

# Saved command rules, as Codex writes them when the owner answers "don't ask again" in a manual session.
RULES = ('prefix_rule(pattern = ["touch"], decision = "allow")\n'
         'prefix_rule(pattern = ["mkdir"], decision = "forbidden")\n')


def post(base_url: str, body: dict, timeout: float = 10) -> tuple[str, list[tuple[str, dict]]]:
    """POST body to <base_url>/responses without any proxy; return the content type and the (event, data) pairs."""
    url = urlsplit(base_url)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
    try:
        connection.request('POST', url.path + '/responses', json.dumps(body), {'content-type': 'application/json'})
        response = connection.getresponse()
        kind, text = response.getheader('content-type'), response.read().decode()
    finally:
        connection.close()
    events = []
    for block in filter(None, text.split('\n\n')):
        name, data = block.split('\n')
        events.append((name.removeprefix('event: '), json.loads(data.removeprefix('data: '))))
    return kind, events


class ScriptedEndpoint(unittest.TestCase):
    def setUp(self):
        self.model = ScriptedModel(); self.base = self.model.start()
        self.addCleanup(self.model.stop)

    def test_base_url(self):
        self.assertRegex(self.base, r'^http://127\.0\.0\.1:\d+/v1$')

    def test_steps_in_order_then_script_finished(self):
        self.model.push(say('one'), run_js('text("two")'))
        (kind, first), (_, second), (_, third) = (post(self.base, {'n': n, 'input': []}) for n in range(3))
        self.assertEqual(kind, 'text/event-stream')
        self.assertEqual([name for name, _ in first],
                         ['response.created', 'response.output_item.done', 'response.completed'])
        self.assertTrue(all(data['type'] == name for name, data in first))
        self.assertEqual(first[1][1]['item']['content'], [{'type': 'output_text', 'text': 'one'}])
        self.assertEqual(second[1][1]['item']['type'], 'custom_tool_call')
        self.assertEqual((second[1][1]['item']['name'], second[1][1]['item']['input']), ('exec', 'text("two")'))
        self.assertEqual(third[1][1]['item']['content'][0]['text'], 'script finished')
        self.assertEqual(set(first[-1][1]['response']['usage']),
                         {'input_tokens', 'input_tokens_details', 'output_tokens', 'output_tokens_details',
                          'total_tokens'})
        self.assertEqual([request['n'] for request in self.model.requests], [0, 1, 2])

    def test_tool_calls_have_distinct_ids(self):
        calls = run_js('a') + run_js('b') + add_file('x', 'y') + delete_file('x')
        self.assertEqual(len({call['call_id'] for call in calls}), 4)
        self.assertEqual(len({call['id'] for call in calls}), 4)

    def test_patches(self):
        patch = '*** Begin Patch\n*** Add File: a "b".txt\n+x\n+\n+\\ y\n*** End Patch\n'
        self.assertIn('await tools.apply_patch(' + json.dumps(patch) + ')',
                      add_file('a "b".txt', 'x\n\n\\ y\n')[0]['input'])
        patch = '*** Begin Patch\n*** Delete File: /a/b\n*** End Patch\n'
        self.assertIn('await tools.apply_patch(' + json.dumps(patch) + ')', delete_file('/a/b')[0]['input'])

    def test_tool_outputs_of_the_last_request(self):
        post(self.base, {'input': [{'type': 'custom_tool_call_output', 'call_id': 'old', 'output': 'stale'}]})
        self.assertEqual(self.model.tool_outputs(), ['stale'])
        post(self.base, {'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'go'}]},
            {'type': 'custom_tool_call_output', 'call_id': 'a', 'output': 'plain'},
            {'type': 'function_call_output', 'call_id': 'b',
             'output': [{'type': 'input_text', 'text': 'one '}, {'type': 'input_text', 'text': 'two'}]}]})
        self.assertEqual(self.model.tool_outputs(), ['plain', 'one two'])

    def test_slow_waits_and_stop_releases_it(self):
        self.model.push(slow(0.5, say('late')))
        started = time.time(); _, events = post(self.base, {})
        self.assertGreaterEqual(time.time() - started, 0.5)
        self.assertEqual([name for name, _ in events],
                         ['response.created', 'response.output_item.done', 'response.completed'])
        self.model.push(slow(60, say('never')))
        outcome = []

        def wait_for_an_answer():
            try:
                post(self.base, {}); outcome.append('answered')
            except (OSError, http.client.HTTPException):
                outcome.append('no answer')

        waiting = threading.Thread(target=wait_for_an_answer, daemon=True)
        waiting.start(); time.sleep(0.3)
        started = time.time(); self.model.stop(); waiting.join(10)
        self.assertFalse(waiting.is_alive()); self.assertLess(time.time() - started, 5)
        self.assertEqual(outcome, ['no answer'])
        self.model.stop()  # a second stop is harmless

    def test_usage_is_zero_unless_scripted(self):
        self.model.push(say('plain'), with_usage(say('counted'), input_tokens=5, output_tokens=2))
        _, plain = post(self.base, {}); _, counted = post(self.base, {})
        self.assertEqual(plain[-1][1]['response']['usage']['total_tokens'], 0)
        usage = counted[-1][1]['response']['usage']
        self.assertEqual((usage['input_tokens'], usage['output_tokens'], usage['total_tokens']), (5, 2, 7))
        self.assertEqual([name for name, _ in counted],
                         ['response.created', 'response.output_item.done', 'response.completed'])

    def test_hang_up_answers_nothing(self):
        self.model.push(hang_up(), say('after'))
        with self.assertRaises((OSError, http.client.HTTPException)):
            post(self.base, {})
        self.assertEqual(post(self.base, {})[1][1][1]['item']['content'][0]['text'], 'after')
        self.assertEqual(len(self.model.requests), 2)

    def test_provider_config(self):
        self.assertEqual(provider_config('http://127.0.0.1:1/v1'), {
            'model_provider': 'mock', 'model_providers': {'mock': {
                'name': 'mock', 'base_url': 'http://127.0.0.1:1/v1', 'wire_api': 'responses',
                'request_max_retries': 0, 'stream_max_retries': 0}}})


class FakeCodex(unittest.TestCase):
    """A stand-in program as $RSI_CODEX: a Python script that notes what it was started with in seen.json."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ); patcher.start(); self.addCleanup(patcher.stop)

    def fake(self, output: str) -> Path:
        program = self.tmp / 'fake-codex'
        program.write_text(
            f'#!{sys.executable}\nimport json, os, sys\nif sys.argv[1:] != ["--version"]: sys.stdin.read()\n'
            'seen = {"env": dict(os.environ), "cwd": os.getcwd(), "argv": sys.argv, "pid": os.getpid(),\n'
            '        "pgid": os.getpgid(0), "home_existed": os.path.isdir(os.environ.get("CODEX_HOME", ""))}\n'
            f'json.dump(seen, open({str(self.tmp / "seen.json")!r}, "w"))\nprint({output!r})\n')
        program.chmod(0o755)
        os.environ['RSI_CODEX'] = str(program)
        # Not a native program, so the real resolution (rsi.programs.native_codex) would refuse it.
        patcher = mock.patch('rsi.codexrun.codex_program', return_value=str(program))
        patcher.start(); self.addCleanup(patcher.stop)
        return program

    def seen(self) -> dict:
        return json.loads((self.tmp / 'seen.json').read_text())

    def test_version_runs_with_a_throwaway_home_in_its_own_group(self):
        self.fake('codex-cli 9.8.7')
        self.assertEqual(codex_version(), '9.8.7')
        seen = self.seen(); home = Path(seen['env']['CODEX_HOME'])
        self.assertEqual(seen['argv'][1:], ['--version'])
        self.assertTrue(seen['home_existed']); self.assertFalse(home.exists())
        self.assertNotEqual(home, Path.home() / '.codex')
        self.assertEqual(seen['pid'], seen['pgid'])

    def test_version_without_output_is_a_clear_error(self):
        self.fake('')
        with self.assertRaisesRegex(RuntimeError, 'reported no version number'):
            codex_version()

    def test_codex_gets_a_minimal_environment_and_absolute_paths(self):
        self.fake('')
        os.environ.update(RSI_TOOL_FAR='secret', SSH_AUTH_SOCK='/x/agent.sock', OPENAI_API_KEY='k', LC_CTYPE='UTF-8',
                          PYTHONPATH='/x', RSI_TEST_MODEL_ENDPOINT='http://127.0.0.1:9/v1')
        (self.tmp / 'work').mkdir(); (self.tmp / 's.json').write_text('{}')
        here = os.getcwd(); os.chdir(self.tmp); self.addCleanup(os.chdir, here)
        os.environ['PATH'] = os.pathsep.join([str(self.tmp / 'env' / 'bin'), 'bin', '', '/usr/bin', '/bin'])
        result = run_codex(home=Path('home'), cwd=Path('work'), prompt='go', config={}, record_dir=Path('r'),
                           timeout=30, schema=Path('s.json'), root=self.tmp)
        seen = self.seen(); env = seen['env']
        self.assertEqual(env['PATH'], '/usr/bin:/bin')           # no entry inside the root, and none relative
        self.assertEqual((env['CODEX_HOME'], seen['cwd']), (str(self.tmp / 'home'), str(self.tmp / 'work')))
        self.assertEqual((env['RSI_TOOL_FAR'], env['LC_CTYPE']), ('secret', 'UTF-8'))
        for gone in ('SSH_AUTH_SOCK', 'OPENAI_API_KEY', 'PYTHONPATH', 'RSI_TEST_MODEL_ENDPOINT', 'RSI_CODEX'):
            self.assertNotIn(gone, env)
        allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'TERM', 'TMPDIR', 'CODEX_HOME',
                   'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
                   'no_proxy', '__CF_USER_TEXT_ENCODING'}
        self.assertEqual([key for key in env if key not in allowed and not key.startswith(('LC_', 'RSI_TOOL_'))], [])
        self.assertEqual(result.command[result.command.index('-o') + 1], str(self.tmp / 'r' / 'last-message.txt'))
        self.assertEqual(result.command[result.command.index('--output-schema') + 1], str(self.tmp / 's.json'))

    def test_proxy_variables_are_kept_and_the_test_endpoint_bypasses_them(self):
        self.fake('')
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
                    'no_proxy'):
            os.environ.pop(key, None)
        os.environ.update(HTTPS_PROXY='http://proxy.example:3128', all_proxy='socks5://proxy.example:1080',
                          NO_PROXY='example.org,localhost')
        os.environ.pop('RSI_TEST_MODEL_ENDPOINT', None)
        run_codex(home=self.tmp / 'h1', cwd=self.tmp, prompt='go', config={}, record_dir=self.tmp / 'r1', timeout=30)
        env = self.seen()['env']
        self.assertEqual((env['HTTPS_PROXY'], env['all_proxy'], env['NO_PROXY']),
                         ('http://proxy.example:3128', 'socks5://proxy.example:1080', 'example.org,localhost'))
        self.assertNotIn('no_proxy', env)
        os.environ['RSI_TEST_MODEL_ENDPOINT'] = 'http://127.0.0.1:9/v1'
        run_codex(home=self.tmp / 'h2', cwd=self.tmp, prompt='go', config={}, record_dir=self.tmp / 'r2', timeout=30)
        env = self.seen()['env']
        self.assertEqual((env['NO_PROXY'], env['no_proxy']), ('example.org,localhost,127.0.0.1', '127.0.0.1,localhost'))
        self.assertEqual(env['HTTPS_PROXY'], 'http://proxy.example:3128')

    def test_a_second_interrupt_during_the_grace_period_cannot_skip_the_kill(self):
        stubborn = subprocess.Popen(['/bin/sh', '-c', 'trap "" TERM; echo ready; exec /bin/sleep 60'],
                                    start_new_session=True, stdout=subprocess.PIPE)
        self.addCleanup(stubborn.stdout.close)
        stubborn.stdout.readline()                                  # SIGTERM is ignored from here on
        self.addCleanup(stubborn.wait); self.addCleanup(stubborn.kill)
        real_wait, waits = stubborn.wait, []

        def wait(timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                raise KeyboardInterrupt                 # the second Ctrl-C (or SIGTERM), while _stop waits
            return real_wait(timeout)
        with mock.patch.object(stubborn, 'wait', wait), self.assertRaises(KeyboardInterrupt):
            _stop(stubborn)
        self.assertEqual(real_wait(timeout=10), -signal.SIGKILL)

    def test_stop_survives_a_group_it_may_not_signal(self):
        process = subprocess.Popen(['/bin/sleep', '0'], start_new_session=True); process.wait()
        with mock.patch('rsi.codexrun.os.killpg', side_effect=PermissionError):
            _stop(process)


class MissingCodex(unittest.TestCase):
    def test_failed_without_the_program(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {'RSI_CODEX': str(tmp / 'no-such-codex')}):
            result = run_codex(home=tmp / 'home', cwd=tmp, prompt='go', config={}, record_dir=tmp / 'r', timeout=10)
        self.assertEqual((result.status, result.exit_code, result.usage), ('failed', None, None))
        self.assertEqual(result.command[0], str(tmp / 'no-such-codex'))
        for name in ('prompt.txt', 'events.jsonl', 'stderr.txt', 'last-message.txt'):
            self.assertTrue((tmp / 'r' / name).is_file())

    def test_a_codex_that_is_not_a_native_program_is_never_started(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        script = tmp / 'codex'; script.write_text(f'#!/bin/sh\ntouch {tmp / "ran"}\necho codex-cli 0.170.0\n')
        script.chmod(0o755)
        with mock.patch.dict(os.environ, {'RSI_CODEX': str(script)}):
            result = run_codex(home=tmp / 'home', cwd=tmp, prompt='go', config={}, record_dir=tmp / 'r', timeout=10)
            with self.assertRaisesRegex(RuntimeError, 'RSI_CODEX'):
                codex_version()
        self.assertEqual((result.status, result.exit_code), ('failed', None))
        self.assertIn('RSI_CODEX', (tmp / 'r' / 'stderr.txt').read_text())
        self.assertFalse((tmp / 'ran').exists())


@needs_codex
class CodexRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve(); self.work = self.tmp / 'work'; self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.model = ScriptedModel(); os.environ['RSI_TEST_MODEL_ENDPOINT'] = self.model.start()
        self.addCleanup(self.model.stop); self.addCleanup(os.environ.pop, 'RSI_TEST_MODEL_ENDPOINT', None)

    def call(self, name, **more):
        return run_codex(home=self.tmp / 'home', cwd=self.work, prompt='go', record_dir=self.tmp / name,
                         config=scratch_config(self.tmp, self.work), timeout=more.pop('timeout', 60), **more)

    def test_version_is_a_dotted_number(self):
        self.assertRegex(codex_version(), r'^\d+(\.\d+)+$')

    def test_finished(self):
        self.model.push(say('hello'))
        result = self.call('r1')
        self.assertEqual((result.status, result.last_message), ('finished', 'hello'))
        self.assertTrue(result.thread_id); self.assertIn('input_tokens', result.usage)
        for name in ('prompt.txt', 'events.jsonl', 'stderr.txt', 'last-message.txt'):
            self.assertTrue((self.tmp / 'r1' / name).is_file())
        self.assertIn('--ignore-user-config', result.command)

    def test_on_start_gets_the_pid(self):
        self.model.push(say('hello')); pids = []
        self.call('r0', on_start=pids.append)
        self.assertEqual((len(pids), isinstance(pids[0], int)), (1, True))

    def test_edit_tool_is_confined(self):
        (self.tmp / 'records').mkdir()
        self.model.push(add_file('inside.txt', 'x'), add_file(str(self.tmp / 'records' / 'outside.txt'), 'x'),
                        say('done'))
        self.call('r2')
        self.assertTrue((self.work / 'inside.txt').is_file())
        self.assertFalse((self.tmp / 'records' / 'outside.txt').exists())

    def test_saved_command_rules_of_the_home_are_not_loaded(self):
        # Besides config.toml, Codex reads <CODEX_HOME>/rules/*.rules, and an `allow` rule there may run a command
        # outside the sandbox. A `forbidden` rule shows whether the rules were loaded at all.
        records = self.tmp / 'records'; records.mkdir()
        rules = self.tmp / 'home' / 'rules'; rules.mkdir(parents=True)
        (rules / 'default.rules').write_text(RULES)

        def script(tag: str) -> list:
            return [run_command(f'mkdir {self.work / ("made-" + tag)}'),
                    run_command(f'touch {self.work / ("inside-" + tag)} {records / ("outside-" + tag)}'), say('done')]
        self.model.push(*script('rsi'))
        result = self.call('r9')
        self.assertEqual(result.status, 'finished'); self.assertIn('--ignore-rules', result.command)
        self.assertTrue((self.work / 'made-rsi').is_dir(), self.model.requests[-1]['input'][-1])   # not loaded
        self.assertTrue((self.work / 'inside-rsi').exists()); self.assertFalse((records / 'outside-rsi').exists())
        # The control: started without the flag, this Codex does load the rules (the forbidden mkdir is refused), so
        # the run above measured something.
        real = subprocess.Popen

        def without_the_flag(command, **options):
            return real([arg for arg in command if arg != '--ignore-rules'], **options)
        self.model.push(*script('control'))
        with mock.patch('rsi.codexrun.subprocess.Popen', without_the_flag):
            self.call('r10')
        self.assertFalse((self.work / 'made-control').exists())
        self.assertFalse((records / 'outside-control').exists())

    def test_timeout(self):
        self.model.push(slow(30, say('late')))
        started = time.time(); result = self.call('r3', timeout=3)
        self.assertEqual(result.status, 'timed_out'); self.assertIsNone(result.usage)
        self.assertLess(time.time() - started, 20)

    def test_usage_of_the_turn(self):
        self.model.push(with_usage(say('hello'), input_tokens=5, output_tokens=2))
        result = self.call('r7')
        self.assertEqual(result.status, 'finished'); self.assertEqual(result.usage['input_tokens'], 5)

    def test_failed_when_the_endpoint_hangs_up(self):
        self.model.push(hang_up())
        started = time.time(); result = self.call('r4', timeout=60)
        self.assertEqual((result.status, result.exit_code), ('failed', 1)); self.assertLess(time.time() - started, 20)

    def test_timed_out_when_the_endpoint_is_gone(self):
        # Codex 0.170.0 does not fail on a refused connection: it waits for the network until it is stopped.
        self.model.stop()
        self.assertEqual(self.call('r8', timeout=5).status, 'timed_out')

    def test_resume_and_schema(self):
        schema = self.tmp / 's.json'
        schema.write_text('{"type":"object","properties":{"a":{"type":"string"}},"required":["a"],'
                          '"additionalProperties":false}')
        self.model.push(say('{"a": "1"}'), say('second'))
        first = self.call('r5', schema=schema)
        self.assertEqual(self.model.requests[0]['text']['format']['type'], 'json_schema')
        second = self.call('r6', resume=first.thread_id)
        self.assertEqual(second.thread_id, first.thread_id); self.assertEqual(second.last_message, 'second')
        self.assertEqual(second.command[second.command.index('resume'):][:5],
                         ['resume', '--skip-git-repo-check', '--json', '--ignore-user-config', '--ignore-rules'])
        self.assertGreaterEqual(sum(1 for i in self.model.requests[-1]['input'] if i.get('role') == 'user'), 2)


if __name__ == '__main__':
    unittest.main()
