import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from rsi.codexrun import codex_version, run_codex
from rsi.mockmodel import ScriptedModel, add_file, delete_file, hang_up, provider_config, run_js, say, slow, with_usage
from tests.support import needs_codex, scratch_config


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


class CodexHome(unittest.TestCase):
    def test_version_runs_with_a_throwaway_home(self):
        seen = {}

        def fake_run(command, **options):
            home = Path(options['env']['CODEX_HOME'])
            seen.update(command=command, home=home, existed=home.is_dir())
            return subprocess.CompletedProcess(command, 0, 'codex-cli 9.8.7\n', '')

        with mock.patch('rsi.codexrun.subprocess.run', fake_run):
            self.assertEqual(codex_version(), '9.8.7')
        self.assertEqual(seen['command'][1:], ['--version'])
        self.assertTrue(seen['existed']); self.assertFalse(seen['home'].exists())
        self.assertNotEqual(seen['home'], Path.home() / '.codex')


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
        self.assertGreaterEqual(sum(1 for i in self.model.requests[-1]['input'] if i.get('role') == 'user'), 2)


if __name__ == '__main__':
    unittest.main()
