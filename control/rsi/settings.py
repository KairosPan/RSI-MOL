"""The Lab's own settings: `codex/settings.toml` and the role files `codex/agents/<name>.toml` of a workspace.

Both agents write these files, so everything in them is untrusted. load_settings keeps only the free keys of the
plan (design §5.3) and turns everything else into a problem: one English line naming the key or file. A bad part
falls back to its default and is listed; nothing in a file's content makes it raise. Files are read without
following symbolic links, and only when they are small regular files.
"""
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from rsi import fs as os
from rsi.paths import SAFE_ID, is_link

SETTINGS = 'codex/settings.toml'
AGENTS = 'codex/agents'
MAX_BYTES = 1 << 20
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')
WEB_SEARCH = ('live', 'cached', 'disabled')
DEPTHS, THREADS = range(1, 4), range(1, 9)
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}')
ROLE = re.compile(r'[a-z][a-z0-9_-]{0,31}')
# Codex's built-in roles, and the scalar keys of Codex's [agents] table that a role of the same name would collide with.
RESERVED_ROLES = {'default', 'explorer', 'worker', 'max_threads', 'max_concurrent_threads_per_session', 'max_depth',
                  'default_subagent_model', 'default_subagent_reasoning_effort', 'job_max_runtime_seconds',
                  'interrupt_message'}
ROLE_KEYS = {'name', 'description', 'developer_instructions', 'model', 'model_reasoning_effort'}
ROLE_REQUIRED = ('name', 'description', 'developer_instructions')
LOCAL_KEYS, REMOTE_KEYS = {'command', 'args', 'network'}, {'url', 'bearer_token_env_var'}
# A secret named here is sent to an agent-chosen address, so only variables the owner set aside for tools qualify.
BEARER = re.compile(r'RSI_TOOL_[A-Z0-9_]{1,64}')
HOST_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
LOCAL_SUFFIXES = ('localhost', 'local', 'internal', 'lan', 'home', 'arpa')
PLAIN = re.compile(r'[A-Za-z0-9_.@/:+-]{1,80}')


@dataclass
class Settings:
    model: str | None = None
    effort: str | None = None
    web_search: str | None = None
    network: bool = False
    max_depth: int = 1
    max_threads: int = 3
    disabled_connectors: list[str] = field(default_factory=list)
    disabled_plugins: list[str] = field(default_factory=list)
    tool_servers: dict[str, dict] = field(default_factory=dict)  # name → {command, args, network} or {url, bearer…}
    # name → {description, file, model, effort, developer_instructions}: what was checked, for compose to copy
    roles: dict[str, dict] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def show(value) -> str:
    """value for a one-line message: plain names as they are, other text quoted, escaped and cut short; for
    anything that is not text only its type (a repr could be huge or recurse too deep)."""
    if not isinstance(value, str):
        return f'<{type(value).__name__}>'
    if PLAIN.fullmatch(value):
        return value
    quoted = repr(value)
    return quoted if len(quoted) <= 80 else quoted[:77] + '...'


def _clean(value) -> bool:
    return isinstance(value, str) and '\x00' not in value


def _read_table(path: Path, label: str, problems: list[str]) -> dict | None:
    """The TOML table in path, or None. A missing file is None without a problem; anything odd is a problem.

    The file is opened without following a link and without blocking (a FIFO must not hang the control program).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        problems.append(f'{label}: cannot be read ({error.strerror}); it may be a symbolic link or a special file, and '
                        'is taken as missing')
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            problems.append(f'{label}: not a regular file; taken as missing')
            return None
        chunks, size = [], 0
        while size <= MAX_BYTES:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk); size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_BYTES:
        problems.append(f'{label}: more than {MAX_BYTES} bytes; taken as missing')
        return None
    try:
        text = b''.join(chunks).decode('utf-8')
    except UnicodeDecodeError:
        problems.append(f'{label}: not UTF-8 text; taken as missing')
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        problems.append(f'{label}: not valid TOML ({" ".join(str(error).split())}); taken as missing')
    except Exception as error:  # e.g. RecursionError on deeply nested arrays or tables: still only a problem
        problems.append(f'{label}: could not be parsed ({type(error).__name__}; it may be nested too deeply); taken as '
                        'missing')
    return None


def _linked(workspace: Path, folder: str, problems: list[str]) -> bool:
    """Whether a workspace folder is a symbolic link; then nothing below it is read."""
    if is_link(workspace / folder):
        problems.append(f'{folder}: a symbolic link; nothing in it takes effect')
        return True
    return False


def is_workspace_path(arg: str, workspace: Path) -> bool:
    """An argument of a local tool server that names a workspace path: not a flag, not absolute, and either
    containing `/` or naming an existing workspace entry."""
    if not arg or arg.startswith('-') or os.path.isabs(arg):
        return False
    return has_path_component(arg) or os.path.lexists(workspace / arg)


def has_path_component(arg: str) -> bool:
    return '/' in arg or (os.name == 'nt' and ('\\' in arg or bool(os.path.splitdrive(arg)[0])))


def inside(arg: str, workspace: Path) -> bool:
    """Whether a relative path stays in the workspace, by its spelling and after following links."""
    normal = os.path.normpath(arg)
    if normal == '..' or normal.startswith('..' + os.sep) or (os.name == 'nt' and os.path.splitdrive(arg)[0]):
        return False
    base = os.path.normcase(os.path.realpath(workspace))
    real = os.path.normcase(os.path.realpath(os.path.join(workspace, normal)))
    return real == base or real.startswith(base + os.sep)


def _url_problem(url) -> str | None:
    """Why a tool server's url is refused, or None. Codex itself connects to it, outside every sandbox, so only
    https to a named host is allowed: no credentials in the url, no IP literal, no local name."""
    if not isinstance(url, str):
        return 'must be text'
    if any(char <= ' ' or char in '\x7f\\' for char in url):
        return 'must not contain white space, control characters or backslashes'
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        return 'is not a valid URL'
    if parts.scheme != 'https' or not host:
        return 'must be a URL that starts with https:// and names a host'
    if '@' in parts.netloc:
        return 'must not carry a user name or a password'
    labels = host.removesuffix('.').split('.')
    if not all(HOST_LABEL.fullmatch(label) for label in labels):
        return 'must name its host by a domain name, not by an IP address'
    last = labels[-1]
    if last.isdigit() or re.fullmatch(r'0x[0-9a-f]*', last):
        return 'must name its host by a domain name, not by an IP address'
    if last in LOCAL_SUFFIXES:
        return 'must not point to this machine or the local network'
    return None


def _tool_server(name: str, server, workspace: Path, problems: list[str]) -> dict | None:
    """The checked tool server, or None after listing why it is left out."""
    where = f'{SETTINGS}: tool server {show(name)}'
    before = len(problems)
    if not isinstance(server, dict):
        problems.append(f'{where} must be a table')
        return None
    for key in sorted(set(server) - LOCAL_KEYS - REMOTE_KEYS):
        problems.append(f'{where}: the key {show(key)} is not allowed')
    local, remote = set(server) & LOCAL_KEYS, set(server) & REMOTE_KEYS
    if local and remote:
        problems.append(f'{where} cannot have both local keys ({", ".join(sorted(local))}) and URL keys '
                        f'({", ".join(sorted(remote))})')
    elif 'command' not in server and 'url' not in server:
        problems.append(f'{where} needs either command (a local server) or url (a server reached by URL)')
    if len(problems) > before:
        return None
    if 'url' in server:
        reason = _url_problem(server['url'])
        if reason:
            problems.append(f'{where}: the url {show(server["url"])} {reason}')
        result = {'url': server['url']}
        if 'bearer_token_env_var' in server:
            variable = server['bearer_token_env_var']
            if not (isinstance(variable, str) and BEARER.fullmatch(variable)):
                problems.append(f'{where}: bearer_token_env_var {show(variable)} must start with RSI_TOOL_ and hold '
                                'only capital letters, digits and _')
            result['bearer_token_env_var'] = variable
        return None if len(problems) > before else result
    command, args, network = server['command'], server.get('args', []), server.get('network', False)
    if not _clean(command) or not command or command.startswith('-'):
        problems.append(f'{where}: command must be non-empty text that does not start with -')
    elif has_path_component(command) and not Path(command).is_absolute() and not inside(command, workspace):
        problems.append(f'{where}: the command {show(command)} points outside the workspace')
    if not isinstance(args, list) or not all(_clean(arg) for arg in args):
        problems.append(f'{where}: args must be a list of texts')
    else:
        for arg in args:
            if os.path.isabs(arg):
                problems.append(f'{where}: the argument {show(arg)} is an absolute path; name a file in the '
                                'workspace by a relative path')
            elif is_workspace_path(arg, workspace) and not inside(arg, workspace):
                problems.append(f'{where}: the argument {show(arg)} points outside the workspace')
    if not isinstance(network, bool):
        problems.append(f'{where}: network must be true or false')
    if len(problems) > before:
        return None
    return {'command': command, 'args': list(args), 'network': network}


def _apply(settings: Settings, table: dict, workspace: Path) -> None:
    problems = settings.problems
    for key in sorted(table, key=str):
        value = table[key]
        if key == 'model':
            if isinstance(value, str) and MODEL.fullmatch(value):
                settings.model = value
            else:
                problems.append(f'{SETTINGS}: model must be a model name (letters, digits and ._:/@+-)')
        elif key == 'model_reasoning_effort':
            if value in EFFORTS:
                settings.effort = value
            else:
                problems.append(f'{SETTINGS}: model_reasoning_effort must be one of {", ".join(EFFORTS)}')
        elif key == 'web_search':
            if value in WEB_SEARCH:
                settings.web_search = value
            else:
                problems.append(f'{SETTINGS}: web_search must be one of {", ".join(WEB_SEARCH)}')
        elif key == 'network':
            if isinstance(value, bool):
                settings.network = value
            else:
                problems.append(f'{SETTINGS}: network must be true or false')
        elif key == 'agents':
            _agents(settings, value)
        elif key == 'disable':
            _disable(settings, value)
        elif key == 'tool_servers':
            if not isinstance(value, dict):
                problems.append(f'{SETTINGS}: tool_servers must be a table')
                continue
            for name in sorted(value, key=str):
                if not (isinstance(name, str) and SAFE_ID.fullmatch(name)) or (
                        os.name == 'nt' and Path(name).is_reserved()):
                    problems.append(f'{SETTINGS}: the tool server name {show(name)} is not valid: use only letters, '
                                    'digits, _ and -, start with a letter or a digit, and use at most 64 characters')
                elif 'codex' in name.lower():
                    problems.append(f'{SETTINGS}: the tool server name {show(name)} contains codex, which is kept '
                                    'for Codex itself')
                else:
                    server = _tool_server(name, value[name], workspace, problems)
                    if server is not None:
                        settings.tool_servers[name] = server
        else:
            problems.append(f'{SETTINGS}: the key {show(key)} is not allowed (only model, model_reasoning_effort, '
                            'web_search, network, agents, disable and tool_servers may be set)')


def _agents(settings: Settings, value) -> None:
    if not isinstance(value, dict):
        settings.problems.append(f'{SETTINGS}: agents must be a table')
        return
    for key in sorted(value, key=str):
        number = value[key]
        allowed = {'max_depth': DEPTHS, 'max_threads': THREADS}.get(key)
        if allowed is None:
            settings.problems.append(f'{SETTINGS}: the key {show(key)} is not allowed in agents (only max_depth and '
                                     'max_threads)')
        elif isinstance(number, bool) or not isinstance(number, int) or number not in allowed:
            settings.problems.append(f'{SETTINGS}: agents.{key} must be an integer from {allowed.start} to '
                                     f'{allowed.stop - 1}')
        else:
            setattr(settings, key, number)


def _disable(settings: Settings, value) -> None:
    if not isinstance(value, dict):
        settings.problems.append(f'{SETTINGS}: disable must be a table')
        return
    for key in sorted(value, key=str):
        ids = value[key]
        target = {'connectors': settings.disabled_connectors, 'plugins': settings.disabled_plugins}.get(key)
        if target is None:
            settings.problems.append(f'{SETTINGS}: the key {show(key)} is not allowed in disable (only connectors and '
                                     'plugins)')
        elif not isinstance(ids, list) or not all(_clean(item) and item for item in ids):
            settings.problems.append(f'{SETTINGS}: disable.{key} must be a list of ids (texts)')
        else:
            target.extend(ids)


def _roles(settings: Settings, workspace: Path) -> None:
    problems = settings.problems
    folder = workspace / AGENTS
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return
    except OSError as error:
        problems.append(f'{AGENTS}: cannot be read ({error.strerror}); no role takes effect')
        return
    for file_name in names:
        if not file_name.endswith('.toml'):
            continue
        label = f'{AGENTS}/{show(file_name)}'
        name = file_name.removesuffix('.toml')
        if not ROLE.fullmatch(name) or name in RESERVED_ROLES or (os.name == 'nt' and Path(name).is_reserved()):
            problems.append(f'{label}: a role name must be [a-z0-9_-] starting with a small letter, at most 32 '
                            "characters, and not default, explorer, worker or a setting name of Codex's agents table")
            continue
        before = len(problems)
        table = _read_table(folder / file_name, label, problems)
        if table is None:
            if len(problems) == before:
                problems.append(f'{label}: cannot be read; the role does not take effect')
            continue
        for key in sorted(set(table) - ROLE_KEYS, key=str):
            problems.append(f'{label}: the key {show(key)} is not allowed (only {", ".join(sorted(ROLE_KEYS))})')
        for key in ROLE_REQUIRED:
            if not (_clean(table.get(key)) and table[key].strip()):
                problems.append(f'{label}: {key} is missing, or it is not non-empty text')
        if table.get('name') not in (None, name) and _clean(table.get('name')):
            problems.append(f'{label}: name must match the file name, that is {name}')
        if 'model' in table and not (isinstance(table['model'], str) and MODEL.fullmatch(table['model'])):
            problems.append(f'{label}: model must be a model name (letters, digits and ._:/@+-)')
        if 'model_reasoning_effort' in table and table['model_reasoning_effort'] not in EFFORTS:
            problems.append(f'{label}: model_reasoning_effort must be one of {", ".join(EFFORTS)}')
        if len(problems) == before:
            settings.roles[name] = {'description': table['description'], 'file': folder / file_name,
                                    'model': table.get('model'), 'effort': table.get('model_reasoning_effort'),
                                    'developer_instructions': table['developer_instructions']}


def load_settings(workspace: Path) -> Settings:
    """The free settings and roles of a workspace; a bad or missing part becomes its default plus a problem."""
    settings = Settings()
    if _linked(workspace, 'codex', settings.problems):
        return settings
    table = _read_table(workspace / SETTINGS, SETTINGS, settings.problems)
    if table:
        _apply(settings, table, workspace)
    if not _linked(workspace, AGENTS, settings.problems):
        _roles(settings, workspace)
    return settings


def workspace_problems(workspace: Path) -> list[str]:
    """load_settings(workspace).problems, the lines of rsi.skills.scan (what of skills/ would not reach
    .agents/skills/ when the workspace is staged for the Lab; nothing is copied here), plus a line when the file
    system finds a `.codex` entry in the workspace, in whatever spelling (rsi.workspace.has_entry)."""
    from rsi.skills import scan             # imported here: rsi.skills and rsi.workspace import this module
    from rsi.workspace import has_entry
    problems = load_settings(workspace).problems + scan(workspace)
    if has_entry(workspace, '.codex'):
        problems.append('The workspace holds .codex')
    return problems
