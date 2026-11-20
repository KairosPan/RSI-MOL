import errno
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import tomllib
import unittest
import uuid
from pathlib import Path
from unittest import mock

from rsi import compose
from rsi.codexrun import codex_program, run_codex
from rsi.compose import (NATIVE_TARGETS, keep_owner_config, lab_config, load_envelope, meta_config, native_codex,
                          profiles, temp_folders, tool_server_problems, write_home_config, write_role_files)
from rsi.mockmodel import ScriptedModel, run_js, say
from rsi.paths import Layout, Refused
from rsi.programs import clean_path
from rsi.settings import load_settings
from rsi.tomlout import render_file, render_flags, toml_value
from tests.support import needs_codex

LITERATURE = 'name = "literature"\ndescription = "d"\ndeveloper_instructions = "i"\n'


def resolving_to(*addresses):
    """A stand-in for socket.getaddrinfo that answers every host with these addresses; it records its calls."""
    def resolve(host, port, *args, **kwargs):
        resolve.calls.append((host, port))
        return [(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))
                for address in addresses]
    resolve.calls = []
    return resolve


def unresolvable(host, port, *args, **kwargs):
    raise socket.gaierror(socket.EAI_NONAME, 'nodename nor servname provided, or not known')


def no_lookup(*args, **kwargs):
    raise AssertionError('no host should be resolved')


PUBLIC = resolving_to('93.184.215.14')
OWNER_CONFIG = b'model = "m"\n[tui]\nx = 1\n'
OTHER_CONFIG = b'approval_policy = "never"\n'
WRAPPER_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
NATIVE = b'\xcf\xfa\xed\xfe a native program\n'

# A minimal MCP server for the real test: it notes its PATH, whether it could write records/, and every method.
PROBE_SERVER = r'''
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
def note(text):
    with open(os.path.join(here, 'probe.log'), 'a') as stream:
        stream.write(text + '\n')
note('PATH=' + os.environ.get('PATH', ''))
try:
    with open(os.path.join(here, '..', '..', '..', 'records', 'x'), 'w') as stream:
        stream.write('x')
    note('records: written')
except OSError:
    note('records: denied')
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    note('method ' + str(method))
    if 'id' not in message:
        continue
    if method == 'initialize':
        result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'probe', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'rsi_probe_ping', 'description': 'Answers pong.',
                             'inputSchema': {'type': 'object', 'properties': {}}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': 'pong'}]}
    else:
        result = None
    answer = {'jsonrpc': '2.0', 'id': message['id']}
    answer.update({'result': result} if result is not None else {'error': {'code': -32601, 'message': method}})
    print(json.dumps(answer), flush=True)
'''


def executable(path: Path, content: bytes = NATIVE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content); path.chmod(0o755)
    return path


class ComposeTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        self.layout = Layout(root)
        self.envelope = {'approval_policy': 'on-request', 'features': {'apps': True, 'plugins': True},
                         'apps': {'_default': {'enabled': False}, 'conn_a': {'enabled': True}},
                         'plugins': {'_default': {'enabled': False}, 'pdf@x': {'enabled': True}},
                         'rsi': {'extra_read': ['/opt/x']}}
        patcher = mock.patch.dict(os.environ); patcher.start(); self.addCleanup(patcher.stop)
        self.outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.native = executable(self.outside / 'native' / 'codex')       # a stand-in for Codex's native program
        os.environ['RSI_CODEX'] = str(self.native)
        self.lab_path = str(self.layout.env) + '/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'

    def settings(self, text=''):
        (self.layout.work / 'codex').mkdir(parents=True, exist_ok=True)
        (self.layout.work / 'codex' / 'settings.toml').write_text(text)
        return load_settings(self.layout.work)

    def test_task_run(self):
        c = lab_config(self.layout, self.envelope, self.settings(), trial=False)
        fs = c['permissions']['lab']['filesystem']; root = str(self.layout.root)
        self.assertEqual((c['approval_policy'], c['default_permissions']), ('never', 'lab'))
        self.assertEqual((fs[':minimal'], fs[root], fs[root + '/work']), ('read', 'deny', 'write'))
        self.assertNotIn(root + '/work/.codex', fs)          # Codex resolves an entry through a link (rsi check)
        self.assertEqual((fs[root + '/library'], fs[root + '/env'], fs['/opt/x']), ('write', 'write', 'read'))
        self.assertEqual(c['permissions']['lab']['network'], {'enabled': False})
        self.assertEqual(sorted(c['permissions']), ['lab', 'lab_net', 'trial', 'trial_net'])
        self.assertNotIn(root + '/work', c['permissions']['trial']['filesystem'])
        self.assertNotIn('network_proxy', c['features'])
        tomllib.loads(render_file(c))

    def test_network_and_trial(self):
        c = lab_config(self.layout, self.envelope, self.settings('network = true\n'), trial=True)
        self.assertEqual(c['default_permissions'], 'trial_net'); self.assertIs(c['features']['network_proxy'], True)
        self.assertEqual(c['permissions']['trial_net']['network'],
                         {'enabled': True, 'mode': 'full', 'domains': {'*': 'allow'}})
        self.assertTrue(c['shell_environment_policy']['set']['TMPDIR'].endswith('/trial/.tmp'))
        self.assertEqual(c['shell_environment_policy']['set']['TMPPREFIX'], str(self.layout.trial / '.tmp/zsh'))

    def test_tool_server_is_wrapped(self):
        text = ('[tool_servers.lit]\ncommand = "python3"\nargs = ["tools/lit/server.py"]\nnetwork = true\n'
                '[tool_servers.far]\nurl = "https://e/mcp"\n')
        c = lab_config(self.layout, self.envelope, self.settings(text), trial=False, resolve=PUBLIC)
        lit = c['mcp_servers']['lit']
        self.assertEqual(lit['command'], str(self.native))
        self.assertEqual(lit['args'], ['sandbox', '-c', 'permissions=' + toml_value(c['permissions']),
                                       '-c', 'features={network_proxy = true}', '-P', 'lab_net',
                                       '--enable', 'network_proxy', '--', '/usr/bin/env', 'PATH=' + self.lab_path,
                                       'TMPPREFIX=' + str(self.layout.work / '.tmp/zsh'),
                                       'python3', str(self.layout.work / 'tools/lit/server.py')])
        self.assertEqual(tomllib.loads(lit['args'][2])['permissions'], c['permissions'])
        self.assertEqual(lit['env'], {'CODEX_HOME': str(self.layout.lab_home / 'rsi/wrapper-home'),
                                      'TMPDIR': str(self.layout.work / '.tmp'), 'PATH': WRAPPER_PATH})
        self.assertEqual(c['mcp_servers']['far'], {'url': 'https://e/mcp'})
        self.assertEqual(c['default_permissions'], 'lab')           # commands stay offline

    def test_disable_and_roles(self):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        s = self.settings('[disable]\nconnectors = ["conn_a", "nope"]\nplugins = ["pdf@x"]\n')
        c = lab_config(self.layout, self.envelope, s, trial=False)
        self.assertEqual((c['apps']['conn_a'], c['plugins']['pdf@x']), ({'enabled': False}, {'enabled': False}))
        self.assertTrue(any('nope' in p for p in s.problems))
        self.assertEqual(c['agents']['literature']['config_file'],
                         str(self.layout.lab_home / 'rsi/roles/work/literature.toml'))

    def test_meta(self):
        c = meta_config(self.layout, self.envelope, {'model_reasoning_effort': 'low'})
        fs = c['permissions']['meta']['filesystem']
        root = str(self.layout.root)
        self.assertEqual((fs[root], fs[root + '/records'], fs[root + '/work'], fs[root + '/meta'],
                          fs[root + '/candidate']), ('deny', 'read', 'read', 'write', 'write'))
        self.assertEqual((c['features'], c['model_reasoning_effort']), ({'apps': False, 'plugins': False}, 'low'))

    @needs_codex
    def test_the_real_sandbox_accepts_the_flags(self):
        os.environ.pop('RSI_CODEX', None)
        for d in ('work', 'records', 'codex-home'): (self.layout.root / d).mkdir(exist_ok=True)
        config = lab_config(self.layout, self.envelope, self.settings(), trial=False)
        flags = render_flags({'permissions': config['permissions']})

        def sandboxed(command):
            return subprocess.run([codex_program(), 'sandbox', *flags, '-P', 'lab', '--', '/bin/sh', '-c', command],
                                  env={**os.environ, 'CODEX_HOME': str(self.layout.lab_home)},
                                  capture_output=True).returncode
        self.assertEqual(sandboxed('echo x > ' + str(self.layout.work / 'a.txt')), 0)
        self.assertNotEqual(sandboxed('echo x > ' + str(self.layout.root / 'records' / 'a.txt')), 0)

    # Beyond the brief.

    def test_everything_in_a_task_run(self):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        s = self.settings('model = "m"\nmodel_reasoning_effort = "high"\nweb_search = "cached"\n'
                          '[agents]\nmax_depth = 2\nmax_threads = 5\n')
        c = lab_config(self.layout, self.envelope, s, trial=False)
        env = str(self.layout.env)
        self.assertEqual((c['model'], c['model_reasoning_effort'], c['web_search']), ('m', 'high', 'cached'))
        self.assertEqual(c['agents'], {'max_depth': 2, 'max_concurrent_threads_per_session': 5,
                                       'literature': {'description': 'd', 'config_file':
                                                      str(self.layout.lab_home / 'rsi/roles/work/literature.toml')}})
        path = env + '/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
        self.assertEqual(c['shell_environment_policy'],
                         {'inherit': 'core', 'set': {'TMPDIR': str(self.layout.work / '.tmp'), 'PATH': path,
                                                     'TMPPREFIX': str(self.layout.work / '.tmp/zsh')}})
        self.assertEqual((c['apps'], c['plugins']), (self.envelope['apps'], self.envelope['plugins']))
        self.assertEqual((c['features'], c['mcp_servers']), ({'apps': True, 'plugins': True}, {}))
        fs = c['permissions']['lab']['filesystem']; home = Path.home().resolve()
        self.assertEqual((fs[str(self.layout.lab_home / 'plugins/cache')], fs[str(home / '.codex')],
                          fs[str(home / '.codex-rsi-lab')]), ('read', 'deny', 'deny'))
        self.assertNotIn(str(self.layout.records), fs)
        self.assertEqual(c['permissions']['lab_net']['filesystem'], fs)
        for folder in ('/opt/homebrew/bin', '/opt/homebrew/etc/fonts'):
            self.assertEqual(fs.get(folder), 'read' if os.path.exists(folder) else None, folder)
        self.assertEqual(lab_config(self.layout, self.envelope, s, trial=False, interactive=True)['approval_policy'],
                         'on-request')
        self.assertEqual(self.envelope['apps']['conn_a'], {'enabled': True})

    def test_trial_paths(self):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        s = self.settings('[tool_servers.lit]\ncommand = "tools/lit/run"\nargs = ["-v", "x.py", "notes.md"]\n')
        (self.layout.trial).mkdir(); (self.layout.trial / 'notes.md').write_text('n')
        c = lab_config(self.layout, self.envelope, s, trial=True)
        trial = self.layout.trial
        self.assertEqual(c['mcp_servers']['lit']['args'],
                         ['sandbox', '-c', 'permissions=' + toml_value(c['permissions']), '-P', 'trial', '--',
                          '/usr/bin/env', 'PATH=' + self.lab_path, 'TMPPREFIX=' + str(trial / '.tmp/zsh'),
                          str(trial / 'tools/lit/run'), '-v', 'x.py',
                          str(trial / 'notes.md')])
        self.assertEqual(c['mcp_servers']['lit']['env']['TMPDIR'], str(trial / '.tmp'))
        self.assertEqual((c['mcp_servers']['lit']['default_tools_approval_mode'],
                          c['mcp_servers']['lit']['startup_timeout_sec']), ('approve', 30))
        self.assertEqual(c['agents']['literature']['config_file'],
                         str(self.layout.lab_home / 'rsi/roles/trial/literature.toml'))
        self.assertNotIn('network_proxy', c['features'])
        fs = c['permissions']['trial']['filesystem']
        self.assertEqual(fs[str(trial)], 'write'); self.assertNotIn(str(trial / '.codex'), fs)
        # A trial (a probe Meta wrote) must not change the live Lab's shared environment or library.
        for profile in ('trial', 'trial_net'):
            fs = c['permissions'][profile]['filesystem']
            self.assertEqual((fs[str(self.layout.library)], fs[str(self.layout.env)]), ('read', 'read'), profile)
        for profile in ('lab', 'lab_net'):
            fs = c['permissions'][profile]['filesystem']
            self.assertEqual((fs[str(self.layout.library)], fs[str(self.layout.env)]), ('write', 'write'), profile)

    def wrapper(self, text='[tool_servers.t]\ncommand = "c"\n'):
        return lab_config(self.layout, self.envelope, self.settings(text), trial=False, resolve=no_lookup)

    def test_the_wrapper_never_runs_from_the_agents_reach(self):
        planted = self.layout.env / 'bin'
        executable(planted / 'node', b'#!/bin/sh\necho planted\n'); executable(planted / 'codex')
        os.environ['PATH'] = str(planted) + os.pathsep + os.environ['PATH']
        t = self.wrapper()['mcp_servers']['t']
        root = str(self.layout.root)
        self.assertEqual(t['command'], str(self.native)); self.assertFalse(t['command'].startswith(root + '/'))
        self.assertEqual(t['env']['PATH'], WRAPPER_PATH)
        self.assertFalse([path for path in t['env']['PATH'].split(':') if path.startswith(root)])
        prefix = 'TMPPREFIX=' + str(self.layout.work / '.tmp/zsh')
        self.assertEqual(t['args'][t['args'].index('--') + 1:], ['/usr/bin/env', 'PATH=' + self.lab_path, prefix, 'c'])
        link = self.outside / 'link-to-planted'; link.symlink_to(planted / 'codex')
        for program in (None, str(planted / 'codex'), str(link)):
            if program is None:
                os.environ.pop('RSI_CODEX')                         # on PATH only in the agents' env/bin: passed over
                os.environ['PATH'] = f'{planted}{os.pathsep}{self.outside / "relative"}{os.pathsep}/usr/bin:/bin'
            else:
                os.environ['RSI_CODEX'] = program
            with self.assertRaises(Refused, msg=program):
                self.wrapper()

    @needs_codex
    def test_codex_is_looked_up_past_the_path_entries_an_agent_can_write(self):
        os.environ.pop('RSI_CODEX')
        real = native_codex(self.layout)
        planted = self.layout.env / 'bin'; executable(planted / 'codex')
        for entry in (str(planted), 'env/bin', str(Path(temp_folders()[0]))):
            os.environ['PATH'] = entry + os.pathsep + os.environ['PATH']
        self.assertEqual(native_codex(self.layout), real)
        self.assertEqual(self.wrapper()['mcp_servers']['t']['command'], real)
        kept = clean_path(self.layout.root).split(os.pathsep)
        self.assertEqual({str(planted), 'env/bin', temp_folders()[0]} & set(kept), set())
        self.assertTrue(set(kept) <= set(os.environ['PATH'].split(os.pathsep)))

    def test_codex_must_be_an_absolute_native_program(self):
        for program in ('codex', 'bin/codex', '', str(self.outside / 'missing')):
            os.environ['RSI_CODEX'] = program
            with self.assertRaises(Refused, msg=program) as refusal:
                self.wrapper()
            self.assertIn('RSI_CODEX', str(refusal.exception))
        plain = executable(self.outside / 'plain'); plain.chmod(0o644)    # not executable
        os.environ['RSI_CODEX'] = str(plain)
        with self.assertRaises(Refused):
            self.wrapper()
        os.environ['RSI_CODEX'] = str(executable(self.outside / 'script', b'#!/bin/sh\nexec true\n'))
        with self.assertRaises(Refused):
            self.wrapper()
        os.environ['RSI_CODEX'] = 'relative-but-no-local-server'
        self.assertEqual(self.wrapper('')['mcp_servers'], {})

    def test_an_npm_shim_is_followed_to_its_native_program(self):
        package = self.outside / 'lib/node_modules/@openai/codex'
        (package / 'package.json').parent.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'name': '@openai/codex', 'version': '0.170.0'}))
        shim = executable(package / 'bin/codex.js', b'#!/usr/bin/env node\n')
        link = self.outside / 'bin/codex'; link.parent.mkdir(); link.symlink_to(shim)
        os.environ['RSI_CODEX'] = str(link)
        with self.assertRaises(Refused) as refusal:                       # a shim without its native program
            self.wrapper()
        self.assertIn('RSI_CODEX', str(refusal.exception))
        target = NATIVE_TARGETS.get((platform.system(), platform.machine()))
        if target is None:
            self.skipTest('no npm platform package is known for this machine')
        native = executable(package / 'node_modules/@openai' / target[0] / 'vendor' / target[1] / 'bin/codex')
        self.assertEqual(self.wrapper()['mcp_servers']['t']['command'], str(native))
        (package / 'package.json').write_text(json.dumps({'name': 'something-else'}))
        with self.assertRaises(Refused):
            self.wrapper()

    @needs_codex
    def test_the_installed_codex_resolves_to_its_native_program(self):
        os.environ.pop('RSI_CODEX')
        command = self.wrapper()['mcp_servers']['t']['command']
        self.assertTrue(os.path.isabs(command) and os.access(command, os.X_OK), command)
        with open(command, 'rb') as stream:
            self.assertNotEqual(stream.read(2), b'#!')
        home = self.layout.root / 'version-home'; home.mkdir()
        versions = [subprocess.run([program, '--version'], capture_output=True, text=True, timeout=60,
                                   env={**os.environ, 'CODEX_HOME': str(home)}).stdout.strip()
                    for program in (command, shutil.which('codex'))]
        self.assertEqual(versions[0], versions[1]); self.assertTrue(versions[0].startswith('codex-cli '), versions)

    @needs_codex
    def test_a_local_tool_server_starts_and_answers_through_the_wrapper(self):
        os.environ.pop('RSI_CODEX')
        for folder in (self.layout.records, self.layout.env / 'bin', self.layout.work / '.tmp',
                       self.layout.work / 'tools' / 'probe'):
            folder.mkdir(parents=True, exist_ok=True)
        (self.layout.work / 'tools/probe/server.py').write_text(PROBE_SERVER)
        envelope = dict(self.envelope, features={'apps': False, 'plugins': False}, apps={}, plugins={})
        s = self.settings('[tool_servers.probe]\ncommand = "python3"\nargs = ["tools/probe/server.py"]\n')
        write_home_config(self.layout, envelope, s, resolve=no_lookup)   # as before a run: role copies, wrapper home
        model = ScriptedModel(); os.environ['RSI_TEST_MODEL_ENDPOINT'] = model.start(); self.addCleanup(model.stop)
        model.push(run_js('text(JSON.stringify(await tools.mcp__probe__rsi_probe_ping({})));\n'), say('done'))
        result = run_codex(home=self.layout.lab_home, cwd=self.layout.work, prompt='go', timeout=120,
                           config=lab_config(self.layout, envelope, s, trial=False, resolve=no_lookup),
                           record_dir=self.layout.root / 'call')
        stderr = (self.layout.root / 'call' / 'stderr.txt').read_text(errors='replace')
        self.assertEqual(result.status, 'finished', stderr[-3000:])
        log = (self.layout.work / 'tools/probe/probe.log').read_text()
        self.assertIn('PATH=' + self.lab_path + '\n', log)
        self.assertIn('records: denied', log); self.assertFalse((self.layout.records / 'x').exists())
        self.assertIn('method initialize', log); self.assertIn('method tools/list', log)
        self.assertIn('mcp__probe', json.dumps(model.requests[0]))        # listed as a deferred tool namespace
        self.assertIn('method tools/call', log); self.assertIn('pong', ' '.join(model.tool_outputs()))

    def test_temp_folders_are_closed(self):
        temps = temp_folders()
        self.assertTrue(set(temps) >= {os.path.realpath(p) for p in ('/tmp', '/var/tmp') if os.path.exists(p)})
        c = lab_config(self.layout, self.envelope, self.settings(), trial=False)
        for profile in [*c['permissions'].values(), meta_config(self.layout, self.envelope, {})['permissions']['meta']]:
            self.assertEqual([profile['filesystem'][folder] for folder in temps], ['deny'] * len(temps))

    def test_codex_under_a_temp_folder_or_in_another_letter_case_is_refused(self):
        temp = Path(tempfile.mkdtemp(dir=temp_folders()[0])); self.addCleanup(shutil.rmtree, temp, True)
        os.environ['RSI_CODEX'] = str(executable(temp / 'codex'))
        with self.assertRaises(Refused):
            self.wrapper()
        planted = executable(self.layout.env / 'bin' / 'codex')
        respelled = str(planted).swapcase()
        if not os.path.exists(respelled):
            self.skipTest('this file system tells letter cases apart')
        os.environ['RSI_CODEX'] = respelled
        with self.assertRaises(Refused):
            self.wrapper()
        with self.assertRaises(Refused):
            profiles(self.layout, dict(self.envelope, rsi={'extra_read': [str(self.layout.records).swapcase()]}))

    @needs_codex
    def test_the_real_sandbox_closes_the_temp_folders(self):
        os.environ.pop('RSI_CODEX')
        for folder in (self.layout.work / '.tmp', self.layout.meta / '.tmp', self.layout.records, self.layout.lab_home):
            folder.mkdir(parents=True, exist_ok=True)
        marker = Path(temp_folders()[0]) / ('rsi-probe-' + uuid.uuid4().hex)
        self.addCleanup(marker.unlink, missing_ok=True)
        codex = native_codex(self.layout)

        def run(config, profile, workspace, command, shell='/bin/sh'):
            flags = render_flags({'permissions': config['permissions']})
            return subprocess.run([codex, 'sandbox', *flags, '-P', profile, '--', shell, '-c', command],
                                  cwd=workspace, capture_output=True, text=True, timeout=60,
                                  env={**os.environ, 'CODEX_HOME': str(self.layout.lab_home),
                                       **config['shell_environment_policy']['set']})
        lab = lab_config(self.layout, self.envelope, self.settings(), trial=False)
        meta = meta_config(self.layout, self.envelope, {})
        for config, profile, workspace in ((lab, 'lab', self.layout.work), (meta, 'meta', self.layout.meta)):
            self.assertEqual(run(config, profile, workspace, 'echo hi').stdout, 'hi\n')
            self.assertNotEqual(run(config, profile, workspace, f'echo x > {marker}').returncode, 0)
            self.assertFalse(marker.exists(), profile)
            self.assertNotEqual(run(config, profile, workspace, f'ls {marker.parent}').returncode, 0)
            done = run(config, profile, workspace, 'echo t > "$TMPDIR/t.txt" && echo w > ' + str(workspace / 'w.txt'))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((workspace / '.tmp/t.txt').is_file() and (workspace / 'w.txt').is_file())
            done = run(config, profile, workspace, '/usr/bin/python3 -c "import tempfile; tempfile.TemporaryFile(); '
                                                   'print(1)"')
            self.assertEqual((done.returncode, done.stdout), (0, '1\n'), done.stderr[-2000:])
            done = run(config, profile, workspace, 'cat <<EOF\nhere\nEOF\ncat <<< string', shell='/bin/zsh')
            self.assertEqual((done.returncode, done.stdout), (0, 'here\nstring\n'), done.stderr[-2000:])

    @needs_codex
    def test_the_wrapper_confines_a_server_without_the_home_config(self):
        os.environ.pop('RSI_CODEX')
        marker = Path(temp_folders()[0]) / ('rsi-probe-' + uuid.uuid4().hex)
        self.addCleanup(marker.unlink, missing_ok=True)
        for folder in (self.layout.work / '.tmp', self.layout.records, self.layout.lab_home,
                       self.layout.work / 'tools' / 'confine'):
            folder.mkdir(parents=True, exist_ok=True)
        (self.layout.work / 'tools/confine/run.sh').write_text(
            'here=$(dirname "$0")\necho "$PATH" > "$here/path.txt"\necho t > "$TMPDIR/t.txt"\n'
            'echo r > "$here/../../../records/r.txt"\necho m > ' + str(marker) + '\n'
            '/bin/zsh -c \'cat > "$1/heredoc.txt" <<EOF\nok\nEOF\' zsh "$here"\necho done > "$here/done.txt"\n')
        s = self.settings('[tool_servers.confine]\ncommand = "/bin/sh"\nargs = ["tools/confine/run.sh"]\n')
        write_role_files(self.layout, s, trial=False)            # what a run does first; prepares the wrapper home
        server = lab_config(self.layout, self.envelope, s, trial=False, resolve=no_lookup)['mcp_servers']['confine']
        self.assertFalse((self.layout.lab_home / 'config.toml').exists())
        done = subprocess.run([server['command'], *server['args']], cwd=self.layout.work, capture_output=True,
                              text=True, timeout=60, env={'HOME': os.environ['HOME'], **server['env']})
        tools = self.layout.work / 'tools/confine'
        self.assertTrue((tools / 'done.txt').is_file(), done.stderr[-2000:])
        self.assertEqual((tools / 'path.txt').read_text(), self.lab_path + '\n')
        self.assertTrue((self.layout.work / '.tmp/t.txt').is_file())
        self.assertEqual((tools / 'heredoc.txt').read_text(), 'ok\n')
        self.assertFalse((self.layout.records / 'r.txt').exists()); self.assertFalse(marker.exists())
        self.assertFalse((self.layout.lab_home / 'config.toml').exists())

    def test_the_wrapper_home_is_kept_empty(self):
        home = self.layout.lab_home / 'rsi/wrapper-home'
        (home / 'tmp').mkdir(parents=True); (home / 'tmp/helper').write_text('codex')
        (home / 'config.toml').write_text('[permissions.lab.filesystem]\n"/" = "write"\n')
        (home / 'other').mkdir(); (home / 'other/x').write_text('x'); (home / 'note.txt').write_text('x')
        (home / 'link').symlink_to(self.layout.records)
        write_role_files(self.layout, self.settings(), trial=False)
        self.assertEqual(sorted(p.name for p in home.iterdir()), ['tmp'])
        self.assertTrue((home / 'tmp/helper').is_file())
        (home / 'tmp/helper').unlink(); (home / 'tmp').rmdir(); (home / 'tmp').symlink_to(self.layout.records)
        write_home_config(self.layout, self.envelope, self.settings(), resolve=no_lookup)
        self.assertEqual(list(home.iterdir()), [])
        self.assertTrue(self.layout.records.parent.is_dir())

    @needs_codex
    def test_the_wrapper_ignores_a_hostile_lab_home_and_a_trusted_workspace(self):
        os.environ.pop('RSI_CODEX')
        tools = self.layout.work / 'tools/confine'
        for folder in (self.layout.work / '.tmp', self.layout.work / '.codex', self.layout.records, tools):
            folder.mkdir(parents=True, exist_ok=True)
        (tools / 'run.sh').write_text('echo r > "$(dirname "$0")/../../../records/r.txt"\n'
                                      'echo done > "$(dirname "$0")/done.txt"\n')
        widen = f'[permissions.lab.filesystem]\n"{self.layout.records}" = "write"\n'
        s = self.settings('[tool_servers.confine]\ncommand = "/bin/sh"\nargs = ["tools/confine/run.sh"]\n')
        write_role_files(self.layout, s, trial=False)          # prepares the wrapper's own, empty home
        self.layout.lab_home.mkdir(exist_ok=True)
        (self.layout.lab_home / 'config.toml').write_text(
            f'default_permissions = "lab"\n[projects."{self.layout.work}"]\ntrust_level = "trusted"\n' + widen)
        (self.layout.work / '.codex/config.toml').write_text(widen)
        server = lab_config(self.layout, self.envelope, s, trial=False, resolve=no_lookup)['mcp_servers']['confine']

        def start(codex_home):
            (tools / 'done.txt').unlink(missing_ok=True); (self.layout.records / 'r.txt').unlink(missing_ok=True)
            done = subprocess.run([server['command'], *server['args']], cwd=self.layout.work, capture_output=True,
                                  text=True, timeout=60,
                                  env={'HOME': os.environ['HOME'], **server['env'], 'CODEX_HOME': codex_home})
            self.assertTrue((tools / 'done.txt').is_file(), done.stderr[-2000:])
            return (self.layout.records / 'r.txt').exists()
        self.assertTrue(start(str(self.layout.lab_home)))       # the hazard: the Lab home's config widens the profile
        self.assertFalse(start(server['env']['CODEX_HOME']))     # the wrapper's own home does not

    def test_a_linked_lab_home_is_never_emptied(self):
        target = self.outside / 'elsewhere'; target.mkdir(); (target / 'keep.txt').write_text('k')
        layouts = []
        for linked in ('codex-home', 'codex-home/rsi', 'codex-home/rsi/wrapper-home'):
            root = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, root, True)
            (root / linked).parent.mkdir(parents=True, exist_ok=True); (root / linked).symlink_to(target)
            layouts.append(Layout(root))
            with self.assertRaises(Refused, msg=linked) as refusal:
                write_role_files(Layout(root), self.settings(), trial=False)
            self.assertIn('符号链接', str(refusal.exception))
            self.assertEqual(sorted(p.name for p in target.iterdir()), ['keep.txt'], linked)

    def test_meta_settings_are_checked(self):
        for bad in ({'model': 'two words'}, {'model': 3}, {'model_reasoning_effort': 'loud'}):
            with self.assertRaises(Refused, msg=bad):
                meta_config(self.layout, self.envelope, bad)

    def test_disabling_twice_reports_once(self):
        s = self.settings('[disable]\nplugins = ["nope"]\n')
        for _ in range(2):
            lab_config(self.layout, self.envelope, s, trial=False)
        self.assertEqual(len([p for p in s.problems if 'nope' in p]), 1)

    def test_extra_read_cannot_open_the_root_or_the_homes(self):
        home = Path.home().resolve()
        for folder in (str(self.layout.records), str(self.layout.root), str(home / '.codex' / 'x'), 'relative/x'):
            envelope = dict(self.envelope, rsi={'extra_read': [folder]})
            with self.assertRaises(Refused, msg=folder):
                profiles(self.layout, envelope)

    def test_meta_everything(self):
        c = meta_config(self.layout, self.envelope, {})
        fs = c['permissions']['meta']['filesystem']; root = str(self.layout.root)
        self.assertEqual((c['approval_policy'], c['default_permissions'], c['web_search']), ('never', 'meta', 'live'))
        self.assertEqual((fs[':minimal'], fs[root + '/library'], fs[root + '/env']), ('read', 'read', 'read'))
        for path in ('/meta/.codex', '/candidate/.codex', '/meta/candidate', '/meta/candidate/.codex'):
            self.assertNotIn(root + path, fs)
        self.assertNotIn(root + '/tasks', fs); self.assertNotIn(root + '/trial', fs); self.assertNotIn('/opt/x', fs)
        self.assertEqual(c['permissions']['meta']['network'], {'enabled': False})
        self.assertEqual(c['shell_environment_policy']['set']['TMPDIR'], str(self.layout.meta / '.tmp'))
        self.assertEqual(c['shell_environment_policy']['set']['TMPPREFIX'], str(self.layout.meta / '.tmp/zsh'))
        self.assertNotIn('model', c); self.assertNotIn('model_reasoning_effort', c)
        self.assertEqual(c['project_doc_max_bytes'], 0)          # Meta cannot write its own standing instructions
        self.assertEqual(meta_config(self.layout, self.envelope, {'model': 'm'})['model'], 'm')
        tomllib.loads(render_file(c))

    @needs_codex
    def test_the_real_sandbox_keeps_trials_off_library_and_env(self):
        os.environ.pop('RSI_CODEX', None)
        for folder in (self.layout.trial / '.tmp', self.layout.work / '.tmp', self.layout.library, self.layout.env,
                       self.layout.lab_home):
            folder.mkdir(parents=True, exist_ok=True)
        config = lab_config(self.layout, self.envelope, self.settings(), trial=True)

        def sandboxed(profile, workspace, command):
            return subprocess.run([native_codex(self.layout), 'sandbox',
                                   *render_flags({'permissions': config['permissions']}), '-P', profile, '--',
                                   '/bin/sh', '-c', command], cwd=workspace, capture_output=True, timeout=60,
                                  env={**os.environ, 'CODEX_HOME': str(self.layout.lab_home)}).returncode
        for folder in (self.layout.library, self.layout.env):
            self.assertNotEqual(sandboxed('trial', self.layout.trial, f'echo x > {folder}/t.txt'), 0, folder)
            self.assertFalse((folder / 't.txt').exists(), folder)
            self.assertEqual(sandboxed('trial', self.layout.trial, f'ls {folder}'), 0, folder)
            self.assertEqual(sandboxed('lab', self.layout.work, f'echo x > {folder}/l.txt'), 0, folder)
        self.assertEqual(sandboxed('trial', self.layout.trial, 'echo x > t.txt'), 0)

    @needs_codex
    def test_the_real_sandbox_lets_meta_write_the_candidate_and_its_notes_only(self):
        os.environ.pop('RSI_CODEX', None)
        for folder in (self.layout.meta / '.tmp', self.layout.candidate / '.codex', self.layout.work,
                       self.layout.records, self.layout.lab_home):
            folder.mkdir(parents=True, exist_ok=True)
        config = meta_config(self.layout, self.envelope, {})

        def sandboxed(command):
            return subprocess.run([native_codex(self.layout), 'sandbox',
                                   *render_flags({'permissions': config['permissions']}), '-P', 'meta', '--',
                                   '/bin/sh', '-c', command], cwd=self.layout.meta, capture_output=True, timeout=60,
                                  env={**os.environ, 'CODEX_HOME': str(self.layout.lab_home)}).returncode
        self.assertEqual(sandboxed('echo c > ../candidate/c.txt && echo n > notes.txt'), 0)
        self.assertEqual(((self.layout.candidate / 'c.txt').read_text(), (self.layout.meta / 'notes.txt').read_text()),
                         ('c\n', 'n\n'))
        for target in ('../work/w.txt', '../candidate/.codex/config.toml', '../records/r.txt', '../trial.txt'):
            self.assertNotEqual(sandboxed(f'echo x > {target}'), 0, target)
            self.assertFalse((self.layout.meta / target).exists(), target)

    def test_home_config(self):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        write_home_config(self.layout, self.envelope, self.settings('network = true\n'))
        written = tomllib.loads((self.layout.lab_home / 'config.toml').read_text())
        expected = tomllib.loads(render_file(lab_config(self.layout, self.envelope, self.settings('network = true\n'),
                                                        trial=False, interactive=True)))
        self.assertEqual(written, expected)
        self.assertEqual((written['approval_policy'], written['default_permissions']), ('on-request', 'lab_net'))
        self.assertEqual(sorted(p.name for p in self.layout.lab_home.iterdir()), ['config.toml', 'rsi'])
        self.assertNotIn('projects', written)
        roles = self.layout.lab_home / 'rsi/roles/work'
        self.assertEqual(sorted(p.name for p in roles.iterdir()), ['literature.toml'])
        self.assertEqual(written['agents']['literature']['config_file'],
                         str(self.layout.lab_home / 'rsi/roles/work/literature.toml'))

    # The owner's own config.toml is never replaced without a verified copy.

    def write_home(self):
        write_home_config(self.layout, self.envelope, self.settings(), resolve=no_lookup)

    def home(self, name='config.toml') -> Path:
        return self.layout.lab_home / name

    def test_a_short_write_of_the_copy_leaves_no_copy_and_the_config_untouched(self):
        self.layout.lab_home.mkdir(); self.home().write_bytes(OWNER_CONFIG)
        real = compose._write_all

        def full_disk(fd, data):
            real(fd, data[:len(data) // 2])
            raise OSError(errno.ENOSPC, 'No space left on device')
        with mock.patch.object(compose, '_write_all', full_disk), self.assertRaises(Refused) as caught:
            self.write_home()
        self.assertIn('config.toml', str(caught.exception))
        self.assertEqual(self.home().read_bytes(), OWNER_CONFIG)
        self.assertEqual(sorted(p.name for p in self.layout.lab_home.iterdir()), ['config.toml', 'rsi'])
        self.write_home()                                                   # the next attempt succeeds
        self.assertEqual(self.home('config.toml.before-rsi').read_bytes(), OWNER_CONFIG)
        self.assertTrue(self.home().read_bytes().startswith(b'# generated by rsi\n'))

    def test_every_owner_config_is_kept_once_before_it_is_replaced(self):
        self.write_home()                                                   # rsi's own file: nothing to keep
        self.assertFalse(self.home('config.toml.before-rsi').exists())
        self.home().write_bytes(OWNER_CONFIG); self.write_home()            # one the owner put there after init
        self.assertEqual(self.home('config.toml.before-rsi').read_bytes(), OWNER_CONFIG)
        self.assertEqual(os.stat(self.home('config.toml.before-rsi')).st_mode & 0o777, 0o600)
        self.home().write_bytes(OTHER_CONFIG); self.write_home()            # a second, different one
        self.assertEqual(self.home('config.toml.before-rsi-2').read_bytes(), OTHER_CONFIG)
        self.home().write_bytes(OWNER_CONFIG)                               # the same bytes again: no new copy
        self.assertEqual(keep_owner_config(self.layout), (self.home('config.toml.before-rsi'), False))
        self.write_home()
        self.assertEqual(sorted(p.name for p in self.layout.lab_home.iterdir()),
                         ['config.toml', 'config.toml.before-rsi', 'config.toml.before-rsi-2', 'rsi'])
        self.assertEqual(self.home('config.toml.before-rsi').read_bytes(), OWNER_CONFIG)

    def test_an_empty_copy_left_by_an_older_rsi_is_passed_over(self):
        self.layout.lab_home.mkdir()
        self.home('config.toml.before-rsi').write_bytes(b''); self.home().write_bytes(OWNER_CONFIG)
        self.write_home()
        self.assertEqual((self.home('config.toml.before-rsi').read_bytes(),
                          self.home('config.toml.before-rsi-2').read_bytes()), (b'', OWNER_CONFIG))

    def test_a_copy_that_does_not_read_back_the_same_is_removed_and_nothing_replaced(self):
        self.layout.lab_home.mkdir(); self.home().write_bytes(OWNER_CONFIG)
        real = compose.read_regular

        def corrupted(path):
            data = real(path)
            return data + b'x' if data is not None and path.name.startswith('config.toml.before-rsi') else data
        with mock.patch.object(compose, 'read_regular', corrupted), self.assertRaises(Refused):
            self.write_home()
        self.assertEqual(self.home().read_bytes(), OWNER_CONFIG)
        self.assertFalse(self.home('config.toml.before-rsi').exists())

    def test_a_linked_config_is_replaced_as_a_link_and_its_target_never_touched(self):
        self.layout.lab_home.mkdir()
        target = self.outside / 'their-config.toml'; target.write_bytes(OWNER_CONFIG)
        self.home().symlink_to(target)
        self.write_home()
        self.assertFalse(self.home().is_symlink()); self.assertEqual(target.read_bytes(), OWNER_CONFIG)
        self.assertTrue(self.home().read_bytes().startswith(b'# generated by rsi\n'))
        self.assertFalse(self.home('config.toml.before-rsi').exists())

    def test_envelope(self):
        owner = self.layout.owner; owner.mkdir()
        (owner / 'lab-envelope.toml').write_text('approval_policy = "on-request"\n[features]\napps = true\n'
                                                 '[apps.conn_a]\nenabled = true\n[rsi]\nextra_read = ["/opt/x"]\n')
        envelope = load_envelope(owner)
        self.assertEqual(envelope, {'approval_policy': 'on-request', 'features': {'apps': True},
                                    'apps': {'conn_a': {'enabled': True}}, 'plugins': {},
                                    'rsi': {'extra_read': ['/opt/x']}})
        (owner / 'lab-envelope.toml').write_text('[rsi.tool_secrets]\nRSI_TOOL_X = "api.example.org"\n')
        self.assertEqual(load_envelope(owner)['rsi'], {'tool_secrets': {'RSI_TOOL_X': 'api.example.org'}})
        (owner / 'lab-envelope.toml').write_text(f'[projects."{self.layout.work}"]\ntrust_level = "trusted"\n')
        with self.assertRaises(Refused) as refusal:
            load_envelope(owner)
        self.assertIn('projects', str(refusal.exception))
        for text in ('sandbox_mode = "x"\n', 'not toml [[[', 'apps = 1\n', '[rsi]\nextra_read = "/opt/x"\n',
                     '[rsi]\ntool_secrets = "x"\n', '[rsi.tool_secrets]\nRSI_TOOL_X = 1\n'):
            (owner / 'lab-envelope.toml').write_text(text)
            with self.assertRaises(Refused, msg=text):
                load_envelope(owner)
        (owner / 'lab-envelope.toml').unlink()
        with self.assertRaises(Refused):
            load_envelope(owner)

    def role(self, name='literature', text=LITERATURE):
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True, exist_ok=True)
        (self.layout.work / 'codex' / 'agents' / (name + '.toml')).write_text(text)

    def test_role_files_are_copied_out_of_the_agents_reach(self):
        self.role(text=LITERATURE + 'model = "m"\n'); self.role('critic', LITERATURE.replace('literature', 'critic'))
        s = self.settings()
        in_workspace = self.layout.work / 'codex/agents/literature.toml'
        in_workspace.write_text(LITERATURE + 'sandbox_mode = "danger-full-access"\n')      # rewritten after the check
        write_role_files(self.layout, s, trial=False)
        copy = self.layout.lab_home / 'rsi/roles/work/literature.toml'
        allowed = {'name': 'literature', 'description': 'd', 'developer_instructions': 'i', 'model': 'm'}
        self.assertEqual(tomllib.loads(copy.read_text()), allowed)
        in_workspace.write_text(LITERATURE + 'sandbox_mode = "danger-full-access"\n')      # and after the copy
        self.assertEqual(tomllib.loads(copy.read_text()), allowed)
        c = lab_config(self.layout, self.envelope, s, trial=False)
        self.assertEqual(c['agents']['literature']['config_file'], str(copy))
        home = str(self.layout.lab_home)                  # under the denied root; only the plugin cache is opened

        def opened(profile):
            return [path for path in profile['filesystem'] if path == home or path.startswith(home + '/')]
        for profile in c['permissions'].values():
            self.assertEqual(opened(profile), [home + '/plugins/cache'])
        self.assertEqual(opened(meta_config(self.layout, self.envelope, {})['permissions']['meta']), [])

    def test_stale_role_copies_are_removed(self):
        self.role(); self.role('critic', LITERATURE.replace('literature', 'critic'))
        write_role_files(self.layout, self.settings(), trial=False)
        write_role_files(self.layout, self.settings(), trial=True)
        work, trial = self.layout.lab_home / 'rsi/roles/work', self.layout.lab_home / 'rsi/roles/trial'
        (work / 'old.toml').write_text('name = "old"\n')
        (self.layout.work / 'codex/agents/critic.toml').unlink()
        write_role_files(self.layout, self.settings(), trial=False)
        self.assertEqual(sorted(p.name for p in work.iterdir()), ['literature.toml'])
        self.assertEqual(sorted(p.name for p in trial.iterdir()), ['critic.toml', 'literature.toml'])
        (self.layout.work / 'codex/agents/literature.toml').write_text(LITERATURE + 'sandbox_mode = "x"\n')
        write_role_files(self.layout, self.settings(), trial=False)
        self.assertEqual(list(work.iterdir()), [])

    def test_url_servers_must_resolve_to_public_addresses(self):
        s = self.settings('[tool_servers.far]\nurl = "https://e:8443/mcp"\n[tool_servers.near]\ncommand = "c"\n')
        cases = {'127.0.0.1': True, '10.0.0.5': True, '169.254.169.254': True, '::1': True, '::ffff:127.0.0.1': True,
                 '192.168.1.2': True, '100.64.0.1': True, '224.0.0.1': True, '0.0.0.0': True, 'fe80::1%en0': True,
                 'fec0::1': True,
                 '93.184.215.14': False, '2606:4700::1111': False}
        for address, refused in cases.items():
            resolve = resolving_to(address)
            problems = tool_server_problems(self.envelope, s, resolve=resolve)
            self.assertEqual(bool(problems), refused, (address, problems))
            self.assertEqual(resolve.calls, [('e', 8443)])
            self.assertTrue(all('far' in p and '\n' not in p for p in problems), problems)
            c = lab_config(self.layout, self.envelope, s, trial=False, resolve=resolving_to(address))
            self.assertEqual(sorted(c['mcp_servers']), ['near'] if refused else ['far', 'near'], address)
        self.assertTrue(tool_server_problems(self.envelope, s, resolve=resolving_to('93.184.215.14', '10.0.0.5')))
        self.assertTrue(tool_server_problems(self.envelope, s, resolve=resolving_to()))
        problems = tool_server_problems(self.envelope, s, resolve=unresolvable)
        self.assertEqual(len(problems), 1); self.assertIn('far', problems[0])
        c = lab_config(self.layout, self.envelope, s, trial=False, resolve=unresolvable)
        self.assertNotIn('far', c['mcp_servers'])
        local = self.settings('[tool_servers.near]\ncommand = "c"\n')
        self.assertEqual(tool_server_problems(self.envelope, local, resolve=no_lookup), [])

    def test_bearer_tokens_go_only_to_the_owners_host(self):
        s = self.settings('[tool_servers.far]\nurl = "https://api.example.org/mcp"\n'
                          'bearer_token_env_var = "RSI_TOOL_X"\n')
        server = {'url': 'https://api.example.org/mcp', 'bearer_token_env_var': 'RSI_TOOL_X'}
        self.assertTrue(any('RSI_TOOL_X' in p for p in tool_server_problems(self.envelope, s, resolve=PUBLIC)))
        for secrets, refused in (({}, True), ({'RSI_TOOL_X': 'other.example.org'}, True),
                                 ({'RSI_TOOL_Y': 'api.example.org'}, True), ({'RSI_TOOL_X': 'API.Example.org'}, False)):
            envelope = dict(self.envelope, rsi={'extra_read': [], 'tool_secrets': secrets})
            problems = tool_server_problems(envelope, s, resolve=PUBLIC)
            self.assertEqual(bool(problems), refused, (secrets, problems))
            self.assertTrue(all('far' in p for p in problems), problems)
            c = lab_config(self.layout, envelope, s, trial=False, resolve=PUBLIC)
            self.assertEqual(c['mcp_servers'].get('far'), None if refused else server, secrets)

    @needs_codex
    def test_the_real_codex_accepts_whole_configurations(self):
        os.environ.pop('RSI_CODEX', None)
        (self.layout.work / 'codex' / 'agents').mkdir(parents=True)
        (self.layout.work / 'codex' / 'agents' / 'literature.toml').write_text(LITERATURE)
        s = self.settings('model = "m"\nmodel_reasoning_effort = "low"\nweb_search = "live"\nnetwork = true\n'
                          '[agents]\nmax_depth = 2\nmax_threads = 4\n[disable]\nplugins = ["pdf@x"]\n'
                          '[tool_servers.lit]\ncommand = "python3"\nargs = ["tools/lit/server.py"]\n'
                          '[tool_servers.far]\nurl = "https://e/mcp"\nbearer_token_env_var = "RSI_TOOL_FAR"\n')
        self.layout.meta.mkdir(); (self.layout.root / 'scratch-home').mkdir()
        envelope = dict(self.envelope, rsi={'extra_read': ['/opt/x'], 'tool_secrets': {'RSI_TOOL_FAR': 'e'}})
        write_role_files(self.layout, s, trial=False)
        task = lab_config(self.layout, envelope, s, trial=False, resolve=PUBLIC)
        self.assertEqual(sorted(task['mcp_servers']), ['far', 'lit'])
        for config in (task, lab_config(self.layout, envelope, s, trial=False, interactive=True, resolve=PUBLIC),
                       meta_config(self.layout, self.envelope, {'model': 'm', 'model_reasoning_effort': 'low'})):
            done = subprocess.run([codex_program(), 'sandbox', *render_flags(config), '--', '/usr/bin/true'],
                                  env={**os.environ, 'CODEX_HOME': str(self.layout.root / 'scratch-home')},
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == '__main__':
    unittest.main()
