"""Native Windows acceptance checks. All agent operations target disposable, owner-created fixtures."""
import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from rsi.check import (ALLOWED, BLOCKED, UNKNOWN, PROBE_SECONDS, PROFILES, _Listener, _build, _make_scratch,
                       _remove_scratch, _result, _run, _workspace)
from rsi.codexrun import PROXY_VARIABLES, codex_environment
from rsi.compose import lab_config, meta_config, native_codex
from rsi.paths import Refused
from rsi.mockmodel import LOCAL_DIAGNOSTICS
from rsi.settings import Settings
from rsi.tomlout import render_file


def check_scope(layout, offline=False):
    # Registry/OS permissions and ACLs vary by host, account and install location.
    identity = [platform.node(), str(sys.getwindowsversion()), os.environ.get('USERDOMAIN'),
                os.environ.get('USERNAME'), str(layout.root.resolve())]
    return {'suite': 'windows-native-v1', 'mode': 'offline' if offline else 'full',
            'installation': hashlib.sha256(json.dumps(identity).encode()).hexdigest()}


# Required read/write rights under lab, trial and meta, respectively. These are the expected contract and are
# independent of the production profile generator. "r" and "rw" are also positive controls for the denied cases.
RIGHTS = {
    'work': ('rw', '', 'r'), 'trial': ('', 'rw', ''), 'meta': ('', '', 'rw'),
    'candidate': ('', '', 'rw'), 'library': ('rw', 'r', 'r'), 'env': ('rw', 'r', 'r'),
    'memory': ('', '', 'r'), 'records': ('', '', 'r'), 'owner': ('', '', ''),
    'control': ('', '', ''), 'tasks': ('', '', ''), 'versions': ('', '', ''),
    'lab_home': ('', '', ''), 'meta_home': ('', '', ''), 'staging': ('', '', ''), 'sealed': ('', '', ''),
    'shared_temp': ('', '', ''), 'private_temp': ('rw', 'rw', 'rw'),
}

PROBE = '''
import json, os, socket, sys
from pathlib import Path
spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
out = {}
for name, area in spec['areas'].items():
    for action in ('read', 'write'):
        try:
            path = Path(area) / (action + '-' + spec['token'])
            if action == 'read':
                observed = 'allowed' if path.read_bytes() == spec['token'].encode() else 'unknown'
            else:
                with path.open('xb') as stream:
                    stream.write(spec['token'].encode())
                observed = 'allowed'
            detail = ''
        except PermissionError as error:
            observed, detail = 'blocked', str(error)
        except OSError as error:
            observed, detail = 'unknown', str(error)
        out[action + '_' + name] = [observed, detail]
try:
    with socket.create_connection(('127.0.0.1', spec['port']), timeout=3):
        out['local_tcp'] = ['allowed', 'connected to fixture']
except (PermissionError, TimeoutError) as error:
    out['local_tcp'] = ['blocked', str(error)]
except OSError as error:
    out['local_tcp'] = ['unknown', str(error)]
proxy_set = any(os.environ.get(name) for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'))
out['proxy_environment'] = ['allowed' if proxy_set else 'blocked', 'proxy variables present' if proxy_set else 'none']
print('RSI-CHECK-WINDOWS ' + json.dumps({'token': spec['token'], 'results': out}))
'''


def _expected(profile):
    column = 2 if profile == 'meta' else 1 if profile.startswith('trial') else 0
    rules = {action + '_' + area: ALLOWED if action[0] in rights[column] else BLOCKED
             for area, rights in RIGHTS.items() for action in ('read', 'write')}
    return {**rules, 'local_tcp': BLOCKED, 'proxy_environment': ALLOWED if profile.endswith('_net') else BLOCKED}


def _profiles(offline):
    return ('lab', 'trial', 'meta') if offline else PROFILES


def required_cases(offline=False):
    from rsi.win_tool_checks import required_cases as tool_cases
    for profile in _profiles(offline):
        for name, expected in _expected(profile).items():
            yield name, profile, expected
        yield 'native_mxc', profile, ALLOWED
        if offline and profile != 'meta':
            yield 'requested_network', profile, BLOCKED
    yield from tool_cases(offline=offline)


def covers_required(results, offline=False):
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        return False
    verified = set()
    for item in results:
        if not all(isinstance(item.get(key), str) for key in ('name', 'profile', 'expected')):
            return False
        if item.get('ok') is not True or item.get('observed') != item['expected']:
            return False
        verified.add((item['name'], item['profile'], item['expected']))
    return set(required_cases(offline)).issubset(verified)


def _probe_output(command, cwd, env, token):
    code, stdout, stderr = _run(command, cwd=cwd, env=env, timeout=PROBE_SECONDS)
    if code is None:
        return {}, f'codex sandbox timed out after {PROBE_SECONDS} seconds'
    if code:
        return {}, f'codex sandbox exited {code}: {stderr[-700:]}'
    for line in stdout.splitlines():
        if line.startswith('RSI-CHECK-WINDOWS '):
            payload = json.loads(line.removeprefix('RSI-CHECK-WINDOWS '))
            if isinstance(payload, dict) and payload.get('token') == token and isinstance(payload.get('results'), dict):
                return payload['results'], ''
    return {}, 'sandbox did not return a completed fixture result'


def _profile(real, scratch, outside, envelope, codex, profile):
    from rsi.compose import windows_offline
    fixture = _build(scratch / profile)
    workspace = _workspace(fixture, profile)
    home = fixture.meta_home if profile == 'meta' else fixture.lab_home
    networked = profile.endswith('_net')
    if profile == 'meta':
        config = meta_config(fixture, envelope, {})
    else:
        config = lab_config(fixture, envelope, Settings(network=networked), trial=profile.startswith('trial'),
                            codex=codex)
    backend = config.get('windows', {}).get('sandbox')
    config_checks = [_result('native_mxc', profile, ALLOWED, ALLOWED if backend == 'mxc' else BLOCKED,
                             f'composed sandbox is {backend}')]
    if windows_offline(envelope) and profile != 'meta':
        requested = lab_config(fixture, envelope, Settings(network=True), trial=profile == 'trial', codex=codex)
        confined = (requested['default_permissions'] == profile and set(requested['permissions']) == {'lab', 'trial'}
                    and all(policy['network']['enabled'] is False for policy in requested['permissions'].values()))
        config_checks.append(_result('requested_network', profile, BLOCKED, BLOCKED if confined else ALLOWED,
                                     'checked all selectable profiles with a network request'))
    # Use the same profile, Windows sandbox selection and features as the role. A fresh Codex home carries no
    # credentials. The explicit selector includes managed requirements from the host.
    (home / 'config.toml').write_text(render_file({**config, **LOCAL_DIAGNOSTICS}), encoding='utf-8')
    env = {key: value for key, value in codex_environment(home, secrets=False, root=real.root).items()
           if key.upper() not in PROXY_VARIABLES}
    env.update(config['shell_environment_policy']['set'])
    token = secrets.token_hex(12)
    areas = {name: getattr(fixture, name) for name in RIGHTS if name not in ('private_temp', 'shared_temp')}
    areas.update(private_temp=workspace / '.tmp', shared_temp=outside)
    for area in areas.values():
        area.mkdir(parents=True, exist_ok=True)
        (area / ('read-' + token)).write_bytes(token.encode())
    script, spec = workspace / 'check.py', workspace / 'check-input.json'
    script.write_text(PROBE, encoding='utf-8')
    listener = _Listener(socket.AF_INET, ('127.0.0.1', 0), token.encode())
    try:
        # Confirm the owner can reach this listener before attributing a timeout to sandbox filtering.
        with socket.create_connection(listener.address, timeout=3) as control:
            with control.makefile('rb') as reply:
                if not reply.read().endswith(token.encode()):
                    raise Refused('the local TCP fixture did not answer its owner control')
        spec.write_text(json.dumps({'areas': {name: str(area) for name, area in areas.items()}, 'token': token,
                                    'port': listener.address[1]}), encoding='utf-8')
        command = [codex, 'sandbox', '--include-managed-config', '--permission-profile', profile,
                   '--', sys._base_executable, '-I', '-B', str(script), str(spec)]
        answer, detail = _probe_output(command, workspace, env, token)
    finally:
        connections = listener.close()
    if connections > 1:
        answer['local_tcp'] = [ALLOWED, 'owner observed an additional connection to the fixture']
    results = []
    for name, expected in _expected(profile).items():
        observed, why = answer.get(name, [UNKNOWN, detail or 'no observation'])
        if name.startswith('write_'):
            marker = areas[name.removeprefix('write_')] / ('write-' + token)
            if marker.exists():
                observed, why = ALLOWED, 'owner observed the write'
            elif observed == ALLOWED:
                observed, why = UNKNOWN, 'owner could not confirm the write'
        results.append(_result(name, profile, expected, observed, why))
    return results + config_checks


def run_windows_cases(layout, envelope, problems, notes):
    from rsi.compose import windows_offline
    offline = windows_offline(envelope)
    results = []
    scratch = None
    notes.append(f'Native Windows {"offline" if offline else "full"} suite: named-profile file access, '
                 'process environment, local TCP and agent tools. '
                 'macOS-specific shell, Unix-socket and platform-specific adversarial tests are not measured here.')
    try:
        codex = native_codex(layout)
        scratch = _make_scratch(layout)
        with tempfile.TemporaryDirectory(prefix='rsi-check-temp-') as outside:
            for profile in _profiles(offline):
                batch = _profile(layout, scratch, Path(outside), envelope, codex, profile)
                results.extend(batch)
                if all(item['observed'] == UNKNOWN for item in batch if item['name'] in _expected(profile)):
                    problems.append(f'The {profile} sandbox did not complete its fixture command: '
                                    f'{batch[0]["detail"]}. Remaining checks were not run.')
                    break
            else:
                from rsi.win_tool_checks import run_tool_checks
                results.extend(run_tool_checks(layout, scratch, envelope, codex))
    except (OSError, ValueError, RuntimeError, Refused, subprocess.SubprocessError) as error:
        problems.append(f'Windows isolation check could not finish: {error}')
    finally:
        if scratch is not None:
            cleanup_problem = _remove_scratch(layout, scratch)
            if cleanup_problem:
                problems.append(cleanup_problem)
    seen = {(item['name'], item['profile'], item['expected']) for item in results}
    for name, profile, expected in required_cases(offline):
        if (name, profile, expected) not in seen:
            why = problems[0] if problems else 'required check returned no measurement'
            results.append(_result(name, profile, expected, UNKNOWN, why))
    return results
