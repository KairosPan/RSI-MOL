"""Exercise the Windows edit, shell and MCP entry points on disposable role workspaces."""
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from rsi import check
from rsi.codexrun import codex_environment, run_codex
from rsi.compose import lab_config, meta_config, windows_offline, write_role_files
from rsi.mockmodel import LOCAL_DIAGNOSTICS, ScriptedModel, run_js, say
from rsi.seed import windows_seed
from rsi.settings import load_settings
from rsi.tomlout import render_flags


def _expectations(role, offline):
    a, b = check.ALLOWED, check.BLOCKED
    cases = {'exec_workspace_edit': a, 'exec_records_edit': b, 'exec_workspace_shell': a,
             'exec_records_shell': b, 'exec_project_instructions': b if role == 'meta' else a}
    if role != 'meta':
        cases.update(exec_memory_status=a, exec_literature_list=a)
        if offline:
            cases.update(offline_remote_mcp=b, offline_literature_network=b)
    return cases


def _wrapper_expectations(name, role, offline):
    a, b = check.ALLOWED, check.BLOCKED
    cases = {'snapshot_read': a, 'records_write': b, 'owner_read': b, 'local_tcp': b,
             'network_proxy': a if name == 'literature' and not offline else b}
    if name == 'literature':
        cases['library_write'] = a if role == 'lab' else b
    else:
        cases['proposals_write'] = a if name == 'memory_live' and role == 'lab' else b
    return cases


def required_cases(offline=False):
    for role in ('lab', 'trial', 'meta'):
        for name, expected in _expectations(role, offline).items():
            yield name, role, expected
        if role != 'meta':
            for service in ('memory', 'memory_live', 'literature'):
                for name, expected in _wrapper_expectations(service, role, offline).items():
                    yield service + '_' + name, role, expected


def _marker(path, token):
    return path.is_file() and path.read_text(encoding='utf-8-sig').strip() == token


def _exec_checks(real, layout, role, envelope, codex):
    workspace = check._workspace(layout, role)
    token = 'rsi-' + secrets.token_hex(12)
    instructions = 'instructions-' + secrets.token_hex(12)
    (workspace / 'AGENTS.md').write_text(instructions, encoding='utf-8')
    home = layout.meta_home if role == 'meta' else layout.lab_home
    services = {}
    offline = windows_offline(envelope)
    if role == 'meta':
        config = meta_config(layout, envelope, {})
    else:
        settings = load_settings(workspace)
        if offline:
            settings.tool_servers['remote_fixture'] = {'url': 'https://example.com/mcp'}
        write_role_files(layout, settings, trial=role == 'trial')
        # A public test address makes the URL valid for composition; no DNS request or connection is required.
        resolver = lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        config = lab_config(layout, envelope, settings, trial=role == 'trial', codex=codex, resolve=resolver)
        services = config['mcp_servers']
        if 'remote_fixture' in services:
            raise ValueError('Offline configuration retained a remote MCP endpoint.')
    edits, commands, targets = [], [], {}
    for prefix, folder in [('workspace', workspace), ('records', layout.records)]:
        patch_name, shell_name = 'exec_' + prefix + '_edit', 'exec_' + prefix + '_shell'
        patch_file = folder / (patch_name + '.txt')
        targets[patch_name] = patch_file
        edits.append((patch_name, check._patch(f'*** Add File: {patch_file.as_posix()}\n+{token}\n')))
        dest = targets[shell_name] = folder / (shell_name + '.txt')
        quoted = str(dest).replace("'", "''")
        commands.append((shell_name, f"[IO.File]::WriteAllText('{quoted}', '{token}')"))
    script = check._edit_script(edits, commands)
    if services:
        for label, tool, test in (
            ('exec_memory_status', 'memory__memory_status', "d.condition === 'none' && d.version === 0"),
            ('exec_literature_list', 'literature__list_providers', "d.status === 'ok' && d.data.providers.length > 0"),
        ):
            script += (f"try {{ const r = await tools.mcp__{tool}({{}}); const d = JSON.parse(r.content[0].text); "
                       f"if (!r.isError && ({test})) text('{label} {token}'); }} "
                       "catch (e) { text(String(e)); }\n")

    model = ScriptedModel()
    url = model.start()
    try:
        model.push(run_js(script), say('Windows role check complete'))
        with check._scripted(url):
            result = run_codex(home=home, cwd=workspace, prompt='Run the local RSI acceptance checks.',
                               config=config, record_dir=layout.root / 'call', timeout=check.EXEC_SECONDS,
                               root=real.root)
        outputs = '\n'.join(model.tool_outputs())
        complete = result.status == 'finished' and check.DONE in outputs
        detail = f'Codex {result.status}, exit {result.exit_code}, {len(model.requests)} local model requests.'
        rows = []
        for name, expected in _expectations(role, offline).items():
            if name in targets:
                found = os.path.lexists(targets[name])
                observed = check.ALLOWED if found else check.UNKNOWN
                if expected == check.ALLOWED and found and not _marker(targets[name], token):
                    observed = check.UNKNOWN
                if expected == check.BLOCKED and not found:
                    control = name.replace('records', 'workspace')
                    if complete and name in outputs and _marker(targets[control], token):
                        observed = check.BLOCKED
            elif name == 'exec_project_instructions':
                observed = check.ALLOWED if instructions in json.dumps(model.requests) else (
                    check.BLOCKED if complete else check.UNKNOWN)
            elif name == 'offline_remote_mcp':
                observed = check.BLOCKED  # the concrete remote configuration was excluded above
            elif name == 'offline_literature_network':
                args = services['literature']['args']
                observed = check.BLOCKED if ('-P' in args and args[args.index('-P') + 1] == role
                            and not config['permissions'][role]['network']['enabled']) else check.ALLOWED
            else:
                observed = check.ALLOWED if complete and name + ' ' + token in outputs else check.UNKNOWN
            rows.append(check._result(name, role, expected, observed, detail))
        return rows
    finally:
        model.stop()


# Split ordinary reads and writes into distinct observations. A missing fixture, program, or arbitrary OS
# error must not count as a permission denial. The controller independently checks the write destinations.
WRAPPER_PROGRAM = '''import json, os, socket
from pathlib import Path
data = json.loads(Path(__file__).with_name("fixture.json").read_text(encoding="utf-8"))
def observed(call):
    try:
        call()
        return "allowed"
    except PermissionError:
        return "blocked"
    except OSError:
        return "unknown"

def local():
    try:
        with socket.create_connection(("127.0.0.1", data["port"]), timeout=2):
            return "allowed"
    except (PermissionError, TimeoutError):
        return "blocked"
    except OSError:
        return "unknown"

results = {"network_proxy": "allowed" if any(os.environ.get(k) for k in
           ("HTTPS_PROXY","HTTP_PROXY","ALL_PROXY")) else "blocked", "local_tcp": local()}
for key in ("snapshot_read", "owner_read"):
    results[key] = observed(lambda: Path(data[key]).read_bytes())
for key in ("records_write", "proposals_write", "library_write"):
    if key not in data:
        continue
    results[key] = observed(lambda: Path(data[key]).write_bytes(b"rsi-check"))
print(json.dumps(results))
'''


def _wrapper_checks(real, layout, role, envelope, codex):
    workspace = check._workspace(layout, role)
    settings = load_settings(workspace)
    rows = []
    for name in ('memory', 'memory_live', 'literature'):
        component = 'memory' if name == 'memory_live' else name
        config = lab_config(layout, envelope, settings, trial=role == 'trial', proposals=name == 'memory_live',
                            codex=codex, resolve=check._no_lookup)
        server = config['mcp_servers'][component]
        script = workspace / 'tools' / component / 'server.py'
        script.write_text(WRAPPER_PROGRAM, encoding='utf-8')
        listener = check._Listener(socket.AF_INET, ('127.0.0.1', 0), b'rsi')
        try:
            with socket.create_connection(listener.address, timeout=2) as connection:
                connection.recv(512)  # prove the fixture can be reached by the controller
            fixture = {'port': listener.address[1], 'snapshot_read': str(workspace / 'memory/status.json'),
                       'owner_read': str(layout.owner / 'limits.toml'),
                       'records_write': str(layout.records / name)}
            if name == 'literature':
                fixture['library_write'] = str(layout.library / name)
            else:
                fixture['proposals_write'] = str(workspace / '.tmp/memory-proposals' / name)
            script.with_name('fixture.json').write_text(json.dumps(fixture), encoding='utf-8')
            env = codex_environment(layout.lab_home, secrets=False, root=real.root)
            env = {k: v for k, v in env.items() if k.upper() not in {s.upper() for s in check.PROXY_VARIABLES}}
            command = [server['command'], *render_flags(LOCAL_DIAGNOSTICS), *server['args']]
            code, out, err = check._run(command, cwd=workspace,
                                        env={**env, **server['env']}, timeout=check.PROBE_SECONDS)
        finally:
            connections = listener.close()
        observations = json.loads(out) if code == 0 else {}
        if connections > 1:
            observations['local_tcp'] = check.ALLOWED
        elif connections != 1:
            observations['local_tcp'] = check.UNKNOWN
        for case, expected in _wrapper_expectations(name, role, windows_offline(envelope)).items():
            observed = observations.get(case, check.UNKNOWN)
            if case.endswith('_write'):
                exists = os.path.lexists(fixture[case])
                if exists:
                    observed = check.ALLOWED
                elif observed == check.ALLOWED:
                    observed = check.UNKNOWN
            rows.append(check._result(name + '_' + case, role, expected, observed, check._tail(err)))
    return rows


def run_tool_checks(real_layout, scratch_dir, envelope, codex):
    # The local scripted endpoint exercises the real CLI. Account-specific plugins do not participate.
    envelope = {**envelope, 'features': {**envelope.get('features', {}), 'apps': False, 'plugins': False},
                'apps': {}, 'plugins': {}}
    rows = []
    for role in ('lab', 'trial', 'meta'):
        try:
            layout = check._build(scratch_dir / ('tool-' + role))
            workspace = check._workspace(layout, role)
            if role != 'meta':
                subprocess.run([sys._base_executable, '-I', '-m', 'venv', '--without-pip', str(layout.env)],
                               check=True, timeout=90, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with windows_seed(check.PROGRAM.parent / 'seed/work', scratch_dir) as seed:
                    shutil.copytree(seed, workspace, dirs_exist_ok=True)
                (workspace / 'memory').mkdir()
                status = {'condition': 'none', 'version': 0, 'counts': {}, 'excluded': {}, 'omitted': {},
                          'task_filter': None}
                (workspace / 'memory/status.json').write_text(json.dumps(status), encoding='utf-8')
                (workspace / '.tmp/memory-proposals').mkdir()
            rows.extend(_exec_checks(real_layout, layout, role, envelope, codex))
            if role != 'meta':
                rows.extend(_wrapper_checks(real_layout, layout, role, envelope, codex))
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError, check.Refused) as error:
            present = {(r['name'], r['profile']) for r in rows}
            for name, profile, expected in required_cases(windows_offline(envelope)):
                if profile == role and (name, profile) not in present:
                    rows.append(check._result(name, profile, expected, check.UNKNOWN, str(error)))
    return rows
