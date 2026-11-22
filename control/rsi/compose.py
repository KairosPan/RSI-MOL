"""The owner's envelope plus the Lab's free settings, composed into complete Codex configurations.

Every configuration carries its own permission profiles. Each profile first denies the whole RSI root, then opens
only what that agent may touch (design §5.1 and appendix): records, tasks, the owner's files, the control program,
the version store and both Codex homes stay out of reach of the agents' commands and local tool servers.

Codex itself runs outside those profiles. So everything Codex reads or contacts on the agents' behalf is checked
here: role files are copies in the Lab home (which no agent can write), and a tool server reached by URL must
resolve only to public addresses and may carry a secret only to the host the owner tied it to.
"""
import copy
import errno
import ipaddress
import os
import shutil
import socket
import stat
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from rsi import programs, proposal_files
from rsi.paths import Layout, Refused
from rsi.programs import NATIVE_TARGETS, TEMP_FOLDERS, inside as _inside, temp_folders  # noqa: F401 (kept names)
from rsi.settings import EFFORTS, MODEL, ROLE, SETTINGS, Settings, is_workspace_path, show
from rsi.tomlout import render_file, toml_value

ENVELOPE = 'lab-envelope.toml'
ENVELOPE_TABLES = ('features', 'apps', 'plugins', 'rsi')
TOOL_FOLDERS = ('/opt/homebrew/bin', '/opt/homebrew/Cellar', '/opt/homebrew/opt', '/opt/homebrew/lib',
                '/opt/homebrew/share', '/opt/homebrew/etc/fonts')
SYSTEM_PATH = '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
# The shared temp folders (rsi.programs.temp_folders): `:minimal` leaves them writable, so every profile denies them
# by their real paths (agents would otherwise pass data through them); each run's TMPDIR is inside its own workspace.
# The PATH of the unsandboxed `codex sandbox` wrapper: nothing an agent can write.
WRAPPER_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
HOME_CONFIG = 'config.toml'
OWNER_BACKUP = 'config.toml.before-rsi'      # the owner's own config.toml, kept before rsi replaces it
BACKUP_NAMES = 100                           # config.toml.before-rsi, config.toml.before-rsi-2, ... -100
MARKER = render_file({}).encode()            # the first line of every config.toml rsi writes
NETWORK_OFF = {'enabled': False}
NETWORK_ON = {'enabled': True, 'mode': 'full', 'domains': {'*': 'allow'}}


def root_rule() -> str:
    """The rule every profile gives the whole RSI root before opening what an agent may touch: 'deny'. A function,
    so that the test of `rsi check` can weaken it and see the check fail."""
    return 'deny'


def _tool_secrets(envelope: dict, source: str = ENVELOPE) -> dict[str, str]:
    """envelope['rsi']['tool_secrets']: environment-variable name → the one host its value may be sent to."""
    secrets = envelope.get('rsi', {}).get('tool_secrets', {})
    if not isinstance(secrets, dict) or not all(isinstance(value, str) and value for value in secrets.values()):
        raise Refused(f'rsi.tool_secrets in {source} must be a table of "variable name = host name"')
    return secrets


def _extra_read_list(envelope: dict, source: str) -> list[str]:
    folders = envelope.get('rsi', {}).get('extra_read', [])
    if not isinstance(folders, list) or not all(isinstance(item, str) and os.path.isabs(item) for item in folders):
        raise Refused(f'rsi.extra_read in {source} must be a list of absolute paths')
    return folders


def load_envelope(owner: Path) -> dict:
    """owner/lab-envelope.toml: approval_policy (default on-request) and the tables features, apps, plugins, rsi.

    It is the owner's file, so anything unexpected is refused rather than guessed.
    """
    path = owner / ENVELOPE
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise Refused(f'{path} does not exist, so what the Lab may reach outside is not defined') from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
        raise Refused(f'{path} cannot be read: {" ".join(str(error).split())}') from None
    if 'projects' in data:
        raise Refused(f'{path} must not have a projects table: a trusted project lets the .codex/config.toml in that '
                      "folder override the Lab's configuration")
    unknown = sorted(set(data) - {'approval_policy', *ENVELOPE_TABLES})
    if unknown:
        raise Refused(f'{path} has unknown keys: {", ".join(unknown)}')
    envelope = {'approval_policy': data.get('approval_policy', 'on-request')}
    if not isinstance(envelope['approval_policy'], str):
        raise Refused(f'approval_policy in {path} must be text')
    for key in ENVELOPE_TABLES:
        envelope[key] = data.get(key, {})
        if not isinstance(envelope[key], dict):
            raise Refused(f'{key} in {path} must be a table')
    _extra_read_list(envelope, str(path))
    _tool_secrets(envelope, str(path))
    return envelope


def _homes() -> tuple[str, str]:
    home = Path.home().resolve()
    return str(home / '.codex'), str(home / '.codex-rsi-lab')


def _extra_read(layout: Layout, envelope: dict) -> list[str]:
    """The owner's extra read-only folders; one inside the RSI root or an everyday Codex home is refused."""
    out = []
    for folder in _extra_read_list(envelope, ENVELOPE):
        for forbidden in (str(layout.root), *_homes()):
            if _inside(folder, forbidden):
                raise Refused(f'{folder!r} in rsi.extra_read of {ENVELOPE} is inside {forbidden}, so it cannot be '
                              'opened')
        out.append(os.path.normpath(folder))
    return out


def _tool_folders() -> list[str]:
    return [folder for folder in TOOL_FOLDERS if os.path.exists(folder)]


def _filesystem(layout: Layout, envelope: dict, workspace: Path, shared: str) -> dict:
    """One Lab profile's filesystem table: deny the root, then open the workspace (write), and library and env with
    the access `shared`.

    There is no entry for <workspace>/.codex: Codex itself keeps an agent from creating .codex (or .agents) in a
    writable root, and Codex resolves an entry through a link, so `<workspace>/.codex = read` with a .codex link
    there would open the link's target (`rsi check` case dot_codex_link)."""
    table = {':minimal': 'read', str(layout.root): root_rule(), str(workspace): 'write',
             str(layout.library): shared, str(layout.env): shared, str(layout.lab_home / 'plugins' / 'cache'): 'read'}
    for folder in _tool_folders() + _extra_read(layout, envelope):
        table.setdefault(folder, 'read')
    for closed in temp_folders() + list(_homes()):
        table[closed] = 'deny'
    return table


def profiles(layout: Layout, envelope: dict) -> dict:
    """The `permissions` table: lab and trial (write work/ or trial/), each also as a _net variant.

    Only lab writes library/ and env/. A trial runs a candidate Meta has not released, or a probe text Meta wrote,
    so it may read them but not change the live Lab's shared environment (env/bin is first on its PATH) or library."""
    out = {}
    for name, workspace, shared in (('lab', layout.work, 'write'), ('trial', layout.trial, 'read')):
        filesystem = _filesystem(layout, envelope, workspace, shared)
        out[name] = {'filesystem': filesystem, 'network': copy.deepcopy(NETWORK_OFF)}
        out[name + '_net'] = {'filesystem': dict(filesystem), 'network': copy.deepcopy(NETWORK_ON)}
    return out


def _path(layout: Layout) -> str:
    return f'{layout.env}/bin:{SYSTEM_PATH}'


def _tmpprefix(workspace: Path) -> str:
    """zsh's TMPPREFIX: zsh puts here-documents, here-strings and =(…) under it (default /tmp/zsh, which every
    profile denies) and ignores TMPDIR."""
    return str(workspace / '.tmp' / 'zsh')


def _shell(layout: Layout, workspace: Path) -> dict:
    return {'inherit': 'core', 'set': {'TMPDIR': str(workspace / '.tmp'), 'PATH': _path(layout),
                                       'TMPPREFIX': _tmpprefix(workspace)}}


def native_codex(layout: Layout) -> str:
    """The real path of Codex's native program for this RSI root (rsi.programs.native_codex): what the tool-server
    wrappers, the check and every call start."""
    return programs.native_codex(layout.root)


def _local_server(layout: Layout, workspace: Path, profile: str, name: str, server: dict, codex: str,
                  permissions: dict, *, proposals: bool = False) -> dict:
    """A local tool server started through `codex sandbox` under the run's profile, or its _net variant when the
    server has network. Workspace paths in its command and arguments become absolute paths in this workspace.
    (load_settings has already left out a server whose paths leave the workspace.)

    The wrapper itself runs outside the sandbox, so it is the native program with a PATH no agent can write; the
    Lab's PATH (with env/bin first) is set by /usr/bin/env inside the sandbox. The four profiles (and the proxy
    feature) travel on the wrapper's own command line, and its CODEX_HOME is wrapper_home(), which never holds a
    config: `-c` is merged with a home's config.toml (it would keep any extra entries there), and a trusted
    project in it would load the workspace's .codex/config.toml."""
    command = server['command']
    if '/' in command and not os.path.isabs(command):
        command = str(workspace / os.path.normpath(command))
    args = [str(workspace / os.path.normpath(arg)) if is_workspace_path(arg, workspace) else arg
            for arg in server['args']]
    if name == 'memory':
        # Only a managed live run can stage proposals. The snapshot itself stays read-only for this server.
        permissions = copy.deepcopy(permissions)
        rules = permissions[profile]['filesystem']
        rules[str(workspace)] = 'read'
        rules[str(layout.env)] = 'read'
        rules.pop(str(layout.library), None)
        if proposals:
            rules[str(workspace / '.tmp' / proposal_files.DIRECTORY)] = 'write'
    elif name == 'literature':
        # Poppler and Python only read env/. Keep this change private to the wrapper; the Lab can still install
        # packages. Trial's shared library is already read-only.
        permissions = copy.deepcopy(permissions)
        for rules in permissions.values():
            rules['filesystem'][str(layout.env)] = 'read'
    sandbox = ['sandbox', '-c', 'permissions=' + toml_value(permissions)]
    if server['network']:
        sandbox += ['-c', 'features=' + toml_value({'network_proxy': True}), '-P', profile + '_net',
                    '--enable', 'network_proxy']
    else:
        sandbox += ['-P', profile]
    in_sandbox = ['/usr/bin/env', 'PATH=' + _path(layout), 'TMPPREFIX=' + _tmpprefix(workspace)]
    if name == 'memory' and proposals:
        in_sandbox.append(proposal_files.ENABLE_ENV + '=1')
    if name == 'literature':
        # macOS's CA bundle is readable under :minimal. Homebrew OpenSSL's default certificate folder is not.
        in_sandbox.append('SSL_CERT_FILE=/etc/ssl/cert.pem')
    return {'command': codex, 'args': [*sandbox, '--', *in_sandbox, command, *args],
            'env': {'CODEX_HOME': str(wrapper_home(layout)), 'TMPDIR': str(workspace / '.tmp'), 'PATH': WRAPPER_PATH},
            'default_tools_approval_mode': 'approve', 'startup_timeout_sec': 30}


def _disabled(table: dict, ids: list[str], key: str, problems: list[str]) -> dict:
    """A copy of an envelope table with each listed id set to enabled = false; an id it lacks becomes a problem."""
    out = copy.deepcopy(table)
    for item in ids:
        if isinstance(out.get(item), dict):
            out[item] = {**out[item], 'enabled': False}
        else:
            message = f'{SETTINGS}: {item!r} in disable.{key} is not among the allowed ones; it is ignored'
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


def wrapper_home(layout: Layout) -> Path:
    """The Codex home of the tool-server wrappers: inside the Lab home (denied to both agents), kept empty."""
    return layout.lab_home / 'rsi' / 'wrapper-home'


def prepare_wrapper_home(layout: Layout) -> None:
    """Create wrapper_home() and remove everything in it except a real `tmp/` folder (Codex's own helper folder).

    A config.toml found there is removed, not refused: only the control program can reach this folder, and the run
    must not depend on what an earlier Codex left behind. But if codex-home, codex-home/rsi or the wrapper home
    itself is a symbolic link, nothing is followed or emptied: Refused.
    """
    home = wrapper_home(layout)
    for folder in (layout.lab_home, home.parent, home):
        if folder.is_symlink():
            raise Refused(f'{folder} is a symbolic link; the control program does not follow it to empty another '
                          'folder. Make it a plain folder.')
        folder.mkdir(exist_ok=True)
    for entry in home.iterdir():
        if entry.name == 'tmp' and entry.is_dir() and not entry.is_symlink():
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def write_role_files(layout: Layout, settings: Settings, *, trial: bool) -> None:
    """Write each checked role, with only the allowed keys, to role_folder(); remove every other file there.
    Also prepare the wrappers' empty Codex home (prepare_wrapper_home).

    The content comes from what load_settings checked, never from the workspace file again. Call it before each
    run whose configuration lab_config builds.
    """
    prepare_wrapper_home(layout)
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
               or form.is_reserved or form.is_unspecified or getattr(form, 'is_site_local', False) for form in forms)


def _server_problems(envelope: dict, settings: Settings, resolve) -> dict[str, list[str]]:
    secrets = _tool_secrets(envelope)
    out = {}
    for name in sorted(settings.tool_servers):
        server = settings.tool_servers[name]
        if name == 'memory' and ('url' in server or server.get('network')):
            out[name] = [f'{SETTINGS}: the memory tool server must be a local server with network off']
            continue
        if 'url' not in server:
            continue
        where, problems = f'{SETTINGS}: tool server {show(name)}', []
        try:
            parts = urlsplit(server['url'])
            host, port = parts.hostname, parts.port or 443
        except ValueError:
            host, port = None, None
        if not host:
            problems.append(f'{where}: the url has no host name')
        else:
            try:
                addresses = sorted({str(info[4][0]) for info in resolve(host, port, type=socket.SOCK_STREAM)})
            except (OSError, UnicodeError, ValueError, TypeError, IndexError) as error:
                addresses = None
                problems.append(f'{where}: the host {show(host)} cannot be resolved ({" ".join(str(error).split())})')
            if addresses is not None and not addresses:
                problems.append(f'{where}: the host {show(host)} resolves to no address')
            local = [address for address in addresses or [] if _local_address(address)]
            if local:
                problems.append(f'{where}: the host {show(host)} resolves to an address of this machine, the local '
                                f'network or a reserved range ({show(", ".join(local))}); Codex connects to it outside '
                                'the sandbox, so this is not allowed')
        variable = server.get('bearer_token_env_var')
        if variable is not None:
            allowed = secrets.get(variable)
            if allowed is None:
                problems.append(f'{where} uses the secret {show(variable)}, but rsi.tool_secrets in {ENVELOPE} does '
                                'not tie it to a host')
            elif host is None or allowed.lower() != host.lower():
                problems.append(f'{where} uses the secret {show(variable)}, which may be sent only to '
                                f'{show(allowed)}, not to {show(host)}')
        if problems:
            out[name] = problems
    return out


def tool_server_problems(envelope: dict, settings: Settings, *, resolve=socket.getaddrinfo) -> list[str]:
    """Problems of the tool servers reached by URL, one English line each, naming the server: a host that does not
    resolve, or resolves to any loopback, private, link-local, multicast, reserved or unspecified address; a bearer
    token whose variable the owner did not tie to exactly that host in rsi.tool_secrets."""
    return [line for lines in _server_problems(envelope, settings, resolve).values() for line in lines]


def lab_config(layout: Layout, envelope: dict, settings: Settings, *, trial: bool, interactive: bool = False,
               resolve=socket.getaddrinfo, codex: str | None = None, proposals: bool = False) -> dict:
    """The complete Codex configuration of a Lab run in work/ (trial=False) or trial/ (trial=True).

    Disabled ids missing from the envelope are added to settings.problems (once each) and ignored. Tool servers
    named by tool_server_problems are left out. Roles point at the copies of write_role_files, which the caller
    writes before the run. The wrappers of local tool servers start `codex` (default native_codex(layout); `rsi
    check`, which composes for scratch roots inside the real one, passes the program it validated for the real root).
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
    if settings.network:
        # Only for the Lab's commands. A local tool server with network gets its proxy from its own wrapper
        # (_local_server), whatever this says: measured with Codex 0.170.0, and by `rsi check`.
        features['network_proxy'] = True
    agents = {'max_depth': settings.max_depth, 'max_concurrent_threads_per_session': settings.max_threads}
    for name, role in settings.roles.items():
        agents[name] = {'description': role['description'],
                        'config_file': str(role_folder(layout, trial=trial) / (name + '.toml'))}
    refused = _server_problems(envelope, settings, resolve)
    kept = {name: server for name, server in settings.tool_servers.items() if name not in refused}
    if any('url' not in server for server in kept.values()):
        codex = codex or native_codex(layout)
    permissions = profiles(layout, envelope)
    servers = {name: dict(server) if 'url' in server
               else _local_server(layout, workspace, base, name, server, codex, permissions,
                                  proposals=proposals and not (trial or interactive))
               for name, server in kept.items()}
    config.update({
        'features': features,
        'shell_environment_policy': _shell(layout, workspace),
        'agents': agents,
        'apps': _disabled(envelope.get('apps', {}), settings.disabled_connectors, 'connectors', settings.problems),
        'plugins': _disabled(envelope.get('plugins', {}), settings.disabled_plugins, 'plugins', settings.problems),
        'mcp_servers': servers,
        'permissions': permissions,
    })
    return config


def meta_config(layout: Layout, envelope: dict, meta_settings: dict) -> dict:
    """The complete Codex configuration of a Meta call: reads records, work, library and env; writes meta/ (its
    notes) and candidate/ only; no apps, no plugins, live web search, commands offline. Only model and
    model_reasoning_effort are taken from meta_settings. The envelope is the Lab's and does not widen Meta.

    No project document is loaded (project_doc_max_bytes = 0): Meta works in meta/, which it writes, so an AGENTS.md
    there would be standing instructions Meta wrote for itself; its duties are the owner's, sent in the prompt.
    As in the Lab profiles, there is no entry for meta/.codex or candidate/.codex (see _filesystem)."""
    filesystem = {':minimal': 'read', str(layout.root): root_rule(), str(layout.records): 'read',
                  str(layout.memory): 'read',
                  str(layout.work): 'read', str(layout.library): 'read', str(layout.env): 'read',
                  str(layout.meta): 'write', str(layout.candidate): 'write'}
    for folder in _tool_folders():
        filesystem.setdefault(folder, 'read')
    for closed in temp_folders() + list(_homes()):
        filesystem[closed] = 'deny'
    config = {'approval_policy': 'never', 'default_permissions': 'meta', 'project_doc_max_bytes': 0}
    for key in ('model', 'model_reasoning_effort'):
        value = meta_settings.get(key)
        if value is None:
            continue
        valid = (isinstance(value, str) and MODEL.fullmatch(value)) if key == 'model' else value in EFFORTS
        if not valid:
            raise Refused(f"{key} in the Meta settings is not valid: {show(value)} (the rules are those of the Lab's "
                          'settings.toml)')
        config[key] = value
    config.update({
        'features': {'apps': False, 'plugins': False},
        'web_search': 'live',
        'shell_environment_policy': _shell(layout, layout.meta),
        'permissions': {'meta': {'filesystem': filesystem, 'network': copy.deepcopy(NETWORK_OFF)}},
    })
    return config


# The owner's own codex-home/config.toml is never replaced without a verified copy.

def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def read_regular(path: Path) -> bytes | None:
    """The bytes of the regular file at path, opened without following a link; None when there is no entry by that
    name; OSError when it is a link or anything but a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(errno.EINVAL, 'not a regular file', str(path))
        return stream.read()


def _sync_folder(folder: Path) -> None:
    """Make a link or rename in folder durable (best effort)."""
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _temporary(folder: Path, data: bytes) -> str:
    """A new, complete and synced file holding data in folder (mode 0600); the caller links or renames it."""
    fd, name = tempfile.mkstemp(prefix='.rsi-', suffix='.tmp', dir=folder)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(name)
        raise
    os.close(fd)
    return name


def backup_names(layout: Layout) -> list[Path]:
    """Every name a copy of the owner's config may have, oldest first: config.toml.before-rsi, …-2, … -100."""
    return [layout.lab_home / (OWNER_BACKUP if number == 1 else f'{OWNER_BACKUP}-{number}')
            for number in range(1, BACKUP_NAMES + 1)]


def _copy_verified(source: Path, target: Path, data: bytes) -> None:
    """target as a new file holding data: written to a temporary file in the same folder and synced, linked to
    target (FileExistsError when the name is taken), then target and source are both read back and must equal data;
    otherwise target is removed again and OSError raised."""
    temporary = _temporary(target.parent, data)
    try:
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    try:
        if read_regular(target) != data or read_regular(source) != data:
            raise OSError(errno.EIO, 'the backup read back differs from the original', str(target))
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    _sync_folder(target.parent)


def keep_owner_config(layout: Layout) -> tuple[Path | None, bool]:
    """When codex-home/config.toml is the owner's (a regular file, never followed if a link, that does not start
    with rsi's MARKER line), make sure a verified copy of it exists: an existing backup with the same bytes, or a
    new one at the first free name of backup_names() (a name taken by other bytes, or by a link, is skipped).
    Returns (that copy, whether it was made now); (None, False) when there is nothing of the owner's to keep.
    Refused, naming the file, when no copy could be made and verified: the caller must then not replace it."""
    path = layout.lab_home / HOME_CONFIG
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, False
    if not stat.S_ISREG(info.st_mode):
        return None, False                      # a link is replaced as a link; its target is never touched
    try:
        data = read_regular(path)
    except OSError as error:
        raise Refused(f'{path} cannot be read ({error.strerror or error}), so it could not be backed up and was not '
                      'rewritten') from None
    if data is None or data.startswith(MARKER):
        return None, False
    for target in backup_names(layout):
        try:
            kept = read_regular(target)
        except OSError:
            continue
        if kept == data:
            return target, False
        if kept is not None:
            continue
        try:
            _copy_verified(path, target, data)
        except FileExistsError:
            continue
        except OSError as error:
            raise Refused(f"{path} is the owner's own configuration and could not be backed up reliably to {target} "
                          f'({error.strerror or error}), so it was not rewritten') from None
        return target, True
    raise Refused(f"{path} is the owner's own configuration, but {OWNER_BACKUP} to {OWNER_BACKUP}-{BACKUP_NAMES} are "
                  'all taken, so it could not be backed up and was not rewritten')


def _write_config(path: Path, text: str) -> None:
    """Replace path with text through a synced temporary file in the same folder (a link at path is replaced as a
    link, never followed)."""
    temporary = _temporary(path.parent, text.encode('utf-8'))
    try:
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise
    _sync_folder(path.parent)


def write_home_config(layout: Layout, envelope: dict, settings: Settings, *, resolve=socket.getaddrinfo,
                      codex: str | None = None) -> None:
    """codex-home/config.toml for opening the Lab Codex by hand: the task-run configuration, but interactive,
    with the role copies of work/ written first. codex: as for lab_config.

    It never holds a `projects` table: a trusted project would let a `.codex/config.toml` in that folder override
    this configuration. (Rewriting the file also drops any trust Codex itself recorded during manual use.) A
    config.toml of the owner's (see keep_owner_config) is copied and verified first; Refused, and nothing replaced,
    when that cannot be done."""
    config = lab_config(layout, envelope, settings, trial=False, interactive=True, resolve=resolve, codex=codex)
    if 'projects' in config:
        raise Refused("The Lab's Codex configuration must not have a projects table")
    text = render_file(config)
    write_role_files(layout, settings, trial=False)
    keep_owner_config(layout)
    _write_config(layout.lab_home / HOME_CONFIG, text)
