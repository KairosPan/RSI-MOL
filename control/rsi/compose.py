"""The owner's envelope plus the Lab's free settings, composed into complete Codex configurations.

Every configuration carries its own permission profiles. Each profile first denies the whole RSI root, then opens
only what that agent may touch (design §5.1 and appendix): records, tasks, the owner's files, the control program,
the version store and both Codex homes stay out of reach of the agents' commands and local tool servers.

Codex itself runs outside those profiles. So everything Codex reads or contacts on the agents' behalf is checked
here: role files are copies in the Lab home (which no agent can write), and a tool server reached by URL must
resolve only to public addresses and may carry a secret only to the host the owner tied it to.
"""
import copy
import ipaddress
import os
import shutil
import socket
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from rsi.codexrun import codex_program
from rsi.paths import Layout, Refused
from rsi.settings import ROLE, SETTINGS, Settings, is_workspace_path, show
from rsi.tomlout import render_file

ENVELOPE = 'lab-envelope.toml'
ENVELOPE_TABLES = ('features', 'apps', 'plugins', 'rsi')
TOOL_FOLDERS = ('/opt/homebrew/bin', '/opt/homebrew/Cellar', '/opt/homebrew/opt', '/opt/homebrew/lib',
                '/opt/homebrew/share', '/opt/homebrew/etc/fonts')
SYSTEM_PATH = '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
NETWORK_OFF = {'enabled': False}
NETWORK_ON = {'enabled': True, 'mode': 'full', 'domains': {'*': 'allow'}}


def _tool_secrets(envelope: dict, source: str = ENVELOPE) -> dict[str, str]:
    """envelope['rsi']['tool_secrets']: environment-variable name → the one host its value may be sent to."""
    secrets = envelope.get('rsi', {}).get('tool_secrets', {})
    if not isinstance(secrets, dict) or not all(isinstance(value, str) and value for value in secrets.values()):
        raise Refused(f'{source} 的 rsi.tool_secrets 必须是“环境变量名 = 主机名”的表')
    return secrets


def _extra_read_list(envelope: dict, source: str) -> list[str]:
    folders = envelope.get('rsi', {}).get('extra_read', [])
    if not isinstance(folders, list) or not all(isinstance(item, str) and os.path.isabs(item) for item in folders):
        raise Refused(f'{source} 的 rsi.extra_read 必须是绝对路径的列表')
    return folders


def load_envelope(owner: Path) -> dict:
    """owner/lab-envelope.toml: approval_policy (default on-request) and the tables features, apps, plugins, rsi.

    It is the owner's file, so anything unexpected is refused rather than guessed.
    """
    path = owner / ENVELOPE
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise Refused(f'找不到 {path}，Lab 的对外范围没有定义') from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise Refused(f'{path} 读不了：{" ".join(str(error).split())}') from None
    unknown = sorted(set(data) - {'approval_policy', *ENVELOPE_TABLES})
    if unknown:
        raise Refused(f'{path} 里有不认识的键：{"、".join(unknown)}')
    envelope = {'approval_policy': data.get('approval_policy', 'on-request')}
    if not isinstance(envelope['approval_policy'], str):
        raise Refused(f'{path} 的 approval_policy 必须是文本')
    for key in ENVELOPE_TABLES:
        envelope[key] = data.get(key, {})
        if not isinstance(envelope[key], dict):
            raise Refused(f'{path} 的 {key} 必须是一个表')
    _extra_read_list(envelope, str(path))
    _tool_secrets(envelope, str(path))
    return envelope


def _homes() -> tuple[str, str]:
    home = Path.home().resolve()
    return str(home / '.codex'), str(home / '.codex-rsi-lab')


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip('/') + '/')


def _extra_read(layout: Layout, envelope: dict) -> list[str]:
    """The owner's extra read-only folders; one inside the RSI root or an everyday Codex home is refused."""
    out = []
    for folder in _extra_read_list(envelope, ENVELOPE):
        forms = {os.path.normpath(folder), os.path.realpath(folder)}
        for forbidden in (str(layout.root), *_homes()):
            limits = {os.path.normpath(forbidden), os.path.realpath(forbidden)}
            if any(_within(form, limit) for form in forms for limit in limits):
                raise Refused(f'{ENVELOPE} 的 rsi.extra_read 里 {folder!r} 在 {forbidden} 里面，不能放开')
        out.append(os.path.normpath(folder))
    return out


def _tool_folders() -> list[str]:
    return [folder for folder in TOOL_FOLDERS if os.path.exists(folder)]


def _filesystem(layout: Layout, envelope: dict, workspace: Path) -> dict:
    """One Lab profile's filesystem table: deny the root, then open the workspace, library and env."""
    table = {':minimal': 'read', str(layout.root): 'deny', str(workspace): 'write', str(workspace / '.codex'): 'read',
             str(layout.library): 'write', str(layout.env): 'write', str(layout.lab_home / 'plugins' / 'cache'): 'read'}
    for folder in _tool_folders() + _extra_read(layout, envelope):
        table.setdefault(folder, 'read')
    for home in _homes():
        table[home] = 'deny'
    return table


def profiles(layout: Layout, envelope: dict) -> dict:
    """The `permissions` table: lab and trial (write work/ or trial/), each also as a _net variant."""
    out = {}
    for name, workspace in (('lab', layout.work), ('trial', layout.trial)):
        filesystem = _filesystem(layout, envelope, workspace)
        out[name] = {'filesystem': filesystem, 'network': copy.deepcopy(NETWORK_OFF)}
        out[name + '_net'] = {'filesystem': dict(filesystem), 'network': copy.deepcopy(NETWORK_ON)}
    return out


def _path(layout: Layout) -> str:
    return f'{layout.env}/bin:{SYSTEM_PATH}'


def _shell(layout: Layout, workspace: Path) -> dict:
    return {'inherit': 'core', 'set': {'TMPDIR': str(workspace / '.tmp'), 'PATH': _path(layout)}}


def _codex_path() -> str:
    """The Codex program as an absolute path, looked up in the control program's own PATH (the wrapper's PATH
    starts with Homebrew, which may hold another Codex); the bare name when it cannot be found."""
    return shutil.which(codex_program()) or codex_program()


def _local_server(layout: Layout, workspace: Path, profile: str, server: dict) -> dict:
    """A local tool server started through `codex sandbox` under the run's profile, or its _net variant when the
    server has network. Workspace paths in its command and arguments become absolute paths in this workspace.
    (load_settings has already left out a server whose paths leave the workspace.)"""
    command = server['command']
    if '/' in command and not os.path.isabs(command):
        command = str(workspace / os.path.normpath(command))
    args = [str(workspace / os.path.normpath(arg)) if is_workspace_path(arg, workspace) else arg
            for arg in server['args']]
    if server['network']:
        sandbox = ['sandbox', '-P', profile + '_net', '--enable', 'network_proxy']
    else:
        sandbox = ['sandbox', '-P', profile]
    return {'command': _codex_path(), 'args': [*sandbox, '--', command, *args],
            'env': {'CODEX_HOME': str(layout.lab_home), 'TMPDIR': str(workspace / '.tmp'), 'PATH': _path(layout)},
            'default_tools_approval_mode': 'approve', 'startup_timeout_sec': 30}


def _disabled(table: dict, ids: list[str], key: str, problems: list[str]) -> dict:
    """A copy of an envelope table with each listed id set to enabled = false; an id it lacks becomes a problem."""
    out = copy.deepcopy(table)
    for item in ids:
        if isinstance(out.get(item), dict):
            out[item] = {**out[item], 'enabled': False}
        else:
            message = f'{SETTINGS}：disable.{key} 里的 {item!r} 不在允许的范围里，忽略'
            if message not in problems:
                problems.append(message)
    return out


def role_folder(layout: Layout, *, trial: bool) -> Path:
    """Where the checked copies of the role files of work/ or trial/ live: inside the Lab home, which every
    profile denies, so no agent can change a role while Codex may read it."""
    return layout.lab_home / 'rsi' / 'roles' / ('trial' if trial else 'work')


def _replace(path: Path, text: str) -> None:
    """Write path through a temporary name, so it is never seen half-written."""
    temporary = path.with_name(path.name + '.rsi-new')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def write_role_files(layout: Layout, settings: Settings, *, trial: bool) -> None:
    """Write each checked role, with only the allowed keys, to role_folder(); remove every other file there.

    The content comes from what load_settings checked, never from the workspace file again. Call it before each
    run whose configuration lab_config builds.
    """
    folder = role_folder(layout, trial=trial)
    folder.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for name, role in settings.roles.items():
        if not ROLE.fullmatch(name):
            raise ValueError(f'role name {name!r} was not checked')
        table = {'name': name, 'description': role['description'],
                 'developer_instructions': role['developer_instructions']}
        if role.get('model') is not None:
            table['model'] = role['model']
        if role.get('effort') is not None:
            table['model_reasoning_effort'] = role['effort']
        _replace(folder / (name + '.toml'), render_file(table))
        wanted.add(name + '.toml')
    for entry in folder.iterdir():
        if entry.name not in wanted:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()


def _local_address(text: str) -> bool:
    """Whether a resolved address is anything but a plain public one."""
    try:
        address = ipaddress.ip_address(text.split('%', 1)[0])
    except ValueError:
        return True
    forms = [address] + ([address.ipv4_mapped] if getattr(address, 'ipv4_mapped', None) else [])
    return any(not form.is_global or form.is_loopback or form.is_private or form.is_link_local or form.is_multicast
               or form.is_reserved or form.is_unspecified for form in forms)


def _url_server_problems(envelope: dict, settings: Settings, resolve) -> dict[str, list[str]]:
    secrets = _tool_secrets(envelope)
    out = {}
    for name in sorted(settings.tool_servers):
        server = settings.tool_servers[name]
        if 'url' not in server:
            continue
        where, problems = f'{SETTINGS}：工具服务 {show(name)}', []
        try:
            parts = urlsplit(server['url'])
            host, port = parts.hostname, parts.port or 443
        except ValueError:
            host, port = None, None
        if not host:
            problems.append(f'{where} 的网址没有主机名')
        else:
            try:
                addresses = sorted({str(info[4][0]) for info in resolve(host, port, type=socket.SOCK_STREAM)})
            except (OSError, UnicodeError, ValueError, TypeError, IndexError) as error:
                addresses = None
                problems.append(f'{where} 的主机 {show(host)} 解析不了（{" ".join(str(error).split())}）')
            if addresses is not None and not addresses:
                problems.append(f'{where} 的主机 {show(host)} 解析不到任何地址')
            local = [address for address in addresses or [] if _local_address(address)]
            if local:
                problems.append(f'{where} 的主机 {show(host)} 解析到本机、局域网或保留地址 {show(", ".join(local))}；'
                                'Codex 在沙箱外连它，不允许')
        variable = server.get('bearer_token_env_var')
        if variable is not None:
            allowed = secrets.get(variable)
            if allowed is None:
                problems.append(f'{where} 用了密钥 {show(variable)}，但 {ENVELOPE} 的 rsi.tool_secrets 没有把它绑定到主机')
            elif host is None or allowed.lower() != host.lower():
                problems.append(f'{where} 用了密钥 {show(variable)}，它只能发给 {show(allowed)}，不能发给 {show(host)}')
        if problems:
            out[name] = problems
    return out


def tool_server_problems(envelope: dict, settings: Settings, *, resolve=socket.getaddrinfo) -> list[str]:
    """Problems of the tool servers reached by URL, one Chinese line each, naming the server: a host that does not
    resolve, or resolves to any loopback, private, link-local, multicast, reserved or unspecified address; a bearer
    token whose variable the owner did not tie to exactly that host in rsi.tool_secrets."""
    return [line for lines in _url_server_problems(envelope, settings, resolve).values() for line in lines]


def lab_config(layout: Layout, envelope: dict, settings: Settings, *, trial: bool, interactive: bool = False,
               resolve=socket.getaddrinfo) -> dict:
    """The complete Codex configuration of a Lab run in work/ (trial=False) or trial/ (trial=True).

    Disabled ids missing from the envelope are added to settings.problems (once each) and ignored. Tool servers
    named by tool_server_problems are left out. Roles point at the copies of write_role_files, which the caller
    writes before the run.
    """
    workspace, base = (layout.trial, 'trial') if trial else (layout.work, 'lab')
    config = {'approval_policy': envelope['approval_policy'] if interactive else 'never',
              'default_permissions': base + ('_net' if settings.network else '')}
    if settings.model is not None:
        config['model'] = settings.model
    if settings.effort is not None:
        config['model_reasoning_effort'] = settings.effort
    if settings.web_search is not None:
        config['web_search'] = settings.web_search
    features = copy.deepcopy(envelope.get('features', {}))
    if settings.network or any(server.get('network') for server in settings.tool_servers.values()):
        features['network_proxy'] = True
    agents = {'max_depth': settings.max_depth, 'max_concurrent_threads_per_session': settings.max_threads}
    for name, role in settings.roles.items():
        agents[name] = {'description': role['description'],
                        'config_file': str(role_folder(layout, trial=trial) / (name + '.toml'))}
    refused = _url_server_problems(envelope, settings, resolve)
    servers = {name: dict(server) if 'url' in server else _local_server(layout, workspace, base, server)
               for name, server in settings.tool_servers.items() if name not in refused}
    config.update({
        'features': features,
        'shell_environment_policy': _shell(layout, workspace),
        'agents': agents,
        'apps': _disabled(envelope.get('apps', {}), settings.disabled_connectors, 'connectors', settings.problems),
        'plugins': _disabled(envelope.get('plugins', {}), settings.disabled_plugins, 'plugins', settings.problems),
        'mcp_servers': servers,
        'permissions': profiles(layout, envelope),
    })
    return config


def meta_config(layout: Layout, envelope: dict, meta_settings: dict) -> dict:
    """The complete Codex configuration of a Meta call: reads records, work, library and env; writes meta/ only;
    no apps, no plugins, live web search, commands offline. Only model and model_reasoning_effort are taken from
    meta_settings. The envelope is the Lab's and does not widen Meta."""
    filesystem = {':minimal': 'read', str(layout.root): 'deny', str(layout.records): 'read', str(layout.work): 'read',
                  str(layout.library): 'read', str(layout.env): 'read', str(layout.meta): 'write',
                  str(layout.meta / '.codex'): 'read', str(layout.candidate / '.codex'): 'read'}
    for folder in _tool_folders():
        filesystem.setdefault(folder, 'read')
    for home in _homes():
        filesystem[home] = 'deny'
    config = {'approval_policy': 'never', 'default_permissions': 'meta'}
    for key in ('model', 'model_reasoning_effort'):
        if meta_settings.get(key) is not None:
            config[key] = meta_settings[key]
    config.update({
        'features': {'apps': False, 'plugins': False},
        'web_search': 'live',
        'shell_environment_policy': _shell(layout, layout.meta),
        'permissions': {'meta': {'filesystem': filesystem, 'network': copy.deepcopy(NETWORK_OFF)}},
    })
    return config


def write_home_config(layout: Layout, envelope: dict, settings: Settings, *, resolve=socket.getaddrinfo) -> None:
    """codex-home/config.toml for opening the Lab Codex by hand: the task-run configuration, but interactive,
    with the role copies of work/ written first."""
    text = render_file(lab_config(layout, envelope, settings, trial=False, interactive=True, resolve=resolve))
    write_role_files(layout, settings, trial=False)
    _replace(layout.lab_home / 'config.toml', text)
