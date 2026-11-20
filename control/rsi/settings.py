"""The Lab's own settings: `codex/settings.toml` and the role files `codex/agents/<name>.toml` of a workspace.

Both agents write these files, so everything in them is untrusted. load_settings keeps only the free keys of the
plan (design §5.3) and turns everything else into a problem: one Chinese line naming the key or file. A bad part
falls back to its default and is listed; nothing in a file's content makes it raise. Files are read without
following symbolic links, and only when they are small regular files.
"""
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from rsi.paths import SAFE_ID

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
    """value for a one-line message: plain names as they are, anything else quoted, escaped and cut short."""
    text = value if isinstance(value, str) else repr(value)
    if PLAIN.fullmatch(text):
        return text
    quoted = repr(text)
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
        problems.append(f'{label}：读不了（{error.strerror}），可能是符号链接或特殊文件，按不存在处理')
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            problems.append(f'{label}：不是普通文件，按不存在处理')
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
        problems.append(f'{label}：超过 {MAX_BYTES} 字节，按不存在处理')
        return None
    try:
        return tomllib.loads(b''.join(chunks).decode('utf-8'))
    except UnicodeDecodeError:
        problems.append(f'{label}：不是 UTF-8 文本，按不存在处理')
    except tomllib.TOMLDecodeError as error:
        problems.append(f'{label}：不是合法的 TOML（{" ".join(str(error).split())}），按不存在处理')
    return None


def _linked(workspace: Path, folder: str, problems: list[str]) -> bool:
    """Whether a workspace folder is a symbolic link; then nothing below it is read."""
    if (workspace / folder).is_symlink():
        problems.append(f'{folder}：是符号链接，里面的内容不生效')
        return True
    return False


def is_workspace_path(arg: str, workspace: Path) -> bool:
    """An argument of a local tool server that names a workspace path: not a flag, not absolute, and either
    containing `/` or naming an existing workspace entry."""
    if not arg or arg.startswith('-') or os.path.isabs(arg):
        return False
    return '/' in arg or os.path.lexists(workspace / arg)


def inside(arg: str, workspace: Path) -> bool:
    """Whether a relative path stays in the workspace, by its spelling and after following links."""
    normal = os.path.normpath(arg)
    if normal == '..' or normal.startswith('../'):
        return False
    base = os.path.realpath(workspace)
    real = os.path.realpath(os.path.join(workspace, normal))
    return real == base or real.startswith(base + os.sep)


def _url_problem(url) -> str | None:
    """Why a tool server's url is refused, or None. Codex itself connects to it, outside every sandbox, so only
    https to a named host is allowed: no credentials in the url, no IP literal, no local name."""
    if not isinstance(url, str):
        return '必须是文本'
    if any(char <= ' ' or char in '\x7f\\' for char in url):
        return '不能含空白、控制字符或反斜杠'
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        return '不是合法的网址'
    if parts.scheme != 'https' or not host:
        return '必须是 https:// 开头、带主机名的网址'
    if '@' in parts.netloc:
        return '不能带用户名或密码'
    labels = host.removesuffix('.').split('.')
    if not all(HOST_LABEL.fullmatch(label) for label in labels):
        return '主机名只能是域名，不能是 IP 地址'
    last = labels[-1]
    if last.isdigit() or re.fullmatch(r'0x[0-9a-f]*', last):
        return '主机名只能是域名，不能是 IP 地址'
    if last in LOCAL_SUFFIXES:
        return '不能指向本机或局域网'
    return None


def _tool_server(name: str, server, workspace: Path, problems: list[str]) -> dict | None:
    """The checked tool server, or None after listing why it is left out."""
    where = f'{SETTINGS}：工具服务 {show(name)}'
    before = len(problems)
    if not isinstance(server, dict):
        problems.append(f'{where} 必须是一个表')
        return None
    for key in sorted(set(server) - LOCAL_KEYS - REMOTE_KEYS):
        problems.append(f'{where} 里不允许的键 {show(key)}')
    local, remote = set(server) & LOCAL_KEYS, set(server) & REMOTE_KEYS
    if local and remote:
        problems.append(f'{where} 不能同时写本地的键（{", ".join(sorted(local))}）和网址的键（{", ".join(sorted(remote))}）')
    elif 'command' not in server and 'url' not in server:
        problems.append(f'{where} 要么写 command（本地），要么写 url（按网址连接）')
    if len(problems) > before:
        return None
    if 'url' in server:
        reason = _url_problem(server['url'])
        if reason:
            problems.append(f'{where} 的 url {show(server["url"])} {reason}')
        result = {'url': server['url']}
        if 'bearer_token_env_var' in server:
            variable = server['bearer_token_env_var']
            if not (isinstance(variable, str) and BEARER.fullmatch(variable)):
                problems.append(f'{where} 的 bearer_token_env_var {show(variable)} 必须以 RSI_TOOL_ 开头，'
                                '只含大写字母、数字和 _')
            result['bearer_token_env_var'] = variable
        return None if len(problems) > before else result
    command, args, network = server['command'], server.get('args', []), server.get('network', False)
    if not _clean(command) or not command or command.startswith('-'):
        problems.append(f'{where} 的 command 必须是不以 - 开头的非空文本')
    elif '/' in command and not os.path.isabs(command) and not inside(command, workspace):
        problems.append(f'{where} 的 command {show(command)} 指向工作区以外')
    if not isinstance(args, list) or not all(_clean(arg) for arg in args):
        problems.append(f'{where} 的 args 必须是文本的列表')
    else:
        for arg in args:
            if os.path.isabs(arg):
                problems.append(f'{where} 的参数 {show(arg)} 是绝对路径；工作区里的文件请写相对路径')
            elif is_workspace_path(arg, workspace) and not inside(arg, workspace):
                problems.append(f'{where} 的参数 {show(arg)} 指向工作区以外')
    if not isinstance(network, bool):
        problems.append(f'{where} 的 network 必须是 true 或 false')
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
                problems.append(f'{SETTINGS}：model 必须是模型名（字母、数字和 ._:/@+-）')
        elif key == 'model_reasoning_effort':
            if value in EFFORTS:
                settings.effort = value
            else:
                problems.append(f'{SETTINGS}：model_reasoning_effort 只能是 {"、".join(EFFORTS)} 之一')
        elif key == 'web_search':
            if value in WEB_SEARCH:
                settings.web_search = value
            else:
                problems.append(f'{SETTINGS}：web_search 只能是 {"、".join(WEB_SEARCH)} 之一')
        elif key == 'network':
            if isinstance(value, bool):
                settings.network = value
            else:
                problems.append(f'{SETTINGS}：network 必须是 true 或 false')
        elif key == 'agents':
            _agents(settings, value)
        elif key == 'disable':
            _disable(settings, value)
        elif key == 'tool_servers':
            if not isinstance(value, dict):
                problems.append(f'{SETTINGS}：tool_servers 必须是一个表')
                continue
            for name in sorted(value, key=str):
                if not (isinstance(name, str) and SAFE_ID.fullmatch(name)):
                    problems.append(f'{SETTINGS}：工具服务的名字 {show(name)} 不合规，只能用字母、数字、_ 和 -，'
                                    '以字母或数字开头，最长 64 个字符')
                elif 'codex' in name.lower():
                    problems.append(f'{SETTINGS}：工具服务的名字 {show(name)} 含 codex，留给 Codex 自己用')
                else:
                    server = _tool_server(name, value[name], workspace, problems)
                    if server is not None:
                        settings.tool_servers[name] = server
        else:
            problems.append(f'{SETTINGS}：不允许的键 {show(key)}（只能改 model、model_reasoning_effort、web_search、'
                            'network、agents、disable、tool_servers）')


def _agents(settings: Settings, value) -> None:
    if not isinstance(value, dict):
        settings.problems.append(f'{SETTINGS}：agents 必须是一个表')
        return
    for key in sorted(value, key=str):
        number = value[key]
        allowed = {'max_depth': DEPTHS, 'max_threads': THREADS}.get(key)
        if allowed is None:
            settings.problems.append(f'{SETTINGS}：agents 里不允许的键 {show(key)}（只能是 max_depth、max_threads）')
        elif isinstance(number, bool) or not isinstance(number, int) or number not in allowed:
            settings.problems.append(f'{SETTINGS}：agents.{key} 必须是 {allowed.start} 到 {allowed.stop - 1} 的整数')
        else:
            setattr(settings, key, number)


def _disable(settings: Settings, value) -> None:
    if not isinstance(value, dict):
        settings.problems.append(f'{SETTINGS}：disable 必须是一个表')
        return
    for key in sorted(value, key=str):
        ids = value[key]
        target = {'connectors': settings.disabled_connectors, 'plugins': settings.disabled_plugins}.get(key)
        if target is None:
            settings.problems.append(f'{SETTINGS}：disable 里不允许的键 {show(key)}（只能是 connectors、plugins）')
        elif not isinstance(ids, list) or not all(_clean(item) and item for item in ids):
            settings.problems.append(f'{SETTINGS}：disable.{key} 必须是编号（文本）的列表')
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
        problems.append(f'{AGENTS}：读不了（{error.strerror}），角色不生效')
        return
    for file_name in names:
        if not file_name.endswith('.toml'):
            continue
        label = f'{AGENTS}/{show(file_name)}'
        name = file_name.removesuffix('.toml')
        if not ROLE.fullmatch(name) or name in RESERVED_ROLES:
            problems.append(f'{label}：角色名必须是小写字母开头的 [a-z0-9_-]，最长 32 个字符，'
                            '且不能是 default、explorer、worker 或 Codex 的 agents 设置名')
            continue
        before = len(problems)
        table = _read_table(folder / file_name, label, problems)
        if table is None:
            if len(problems) == before:
                problems.append(f'{label}：读不了，角色不生效')
            continue
        for key in sorted(set(table) - ROLE_KEYS, key=str):
            problems.append(f'{label}：不允许的键 {show(key)}（只能有 {"、".join(sorted(ROLE_KEYS))}）')
        for key in ROLE_REQUIRED:
            if not (_clean(table.get(key)) and table[key].strip()):
                problems.append(f'{label}：缺少 {key}，或它不是非空文本')
        if table.get('name') not in (None, name) and _clean(table.get('name')):
            problems.append(f'{label}：name 必须和文件名一致，是 {name}')
        if 'model' in table and not (isinstance(table['model'], str) and MODEL.fullmatch(table['model'])):
            problems.append(f'{label}：model 必须是模型名（字母、数字和 ._:/@+-）')
        if 'model_reasoning_effort' in table and table['model_reasoning_effort'] not in EFFORTS:
            problems.append(f'{label}：model_reasoning_effort 只能是 {"、".join(EFFORTS)} 之一')
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
    """load_settings(workspace).problems, plus a line when the workspace holds a `.codex` entry."""
    problems = load_settings(workspace).problems
    if os.path.lexists(workspace / '.codex'):
        problems.append('工作区里有 .codex')
    return problems
