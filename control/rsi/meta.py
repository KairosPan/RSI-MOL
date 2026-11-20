"""A Meta round (design §5.5 and §5.6), its release, and a rollback.

Meta reads records/ and may change a candidate copy of the Lab workspace; every Meta call ends with a decision
(rsi.decision): no_change, trial or release. The control program carries the decision out and answers on the same
Codex thread, until Meta decides no_change, a release goes through, two answers are not valid decisions, or the
round's limits are reached. Every answer to Meta is one more Meta call, counted against meta.calls_per_round, so a
round always ends. The round holds no lock: the command line does.

Meta is an adversary of the same kind as the Lab. It writes meta/ (its notes) and candidate/ by path, so the control
program never reads, walks, changes or runs git on either in place (the rule of rsi.labrun). candidate/ sits in the
RSI root beside work/ and trial/ and is handled as they are: when the program needs its content (a trial or a
release decision) it seals the entry by a rename in the root, makes the sealed, static tree storable, checks it, and
snapshots it on refs/rounds/<round>; then a checkout of that snapshot is staged and renamed in as candidate/ again,
so Meta goes on from exactly what was snapshotted. A tree that has settings problems is not snapshotted and is
renamed back, so Meta can fix it; one that cannot be stored at all (a link, a `.codex`, something that must be left
out but cannot be moved out) is kept under sealed/ and candidate/ starts again from the round's last snapshot or the
current version. A round that releases nothing keeps its candidate all the same: snapshotted on the round's ref
(final_candidate in round.json) when it can be and is new, kept under sealed/ when it cannot be. Of meta/ the program
only creates meta/.tmp and meta/notes when missing.

Everything in a decision is untrusted: ids are checked before any path or git command is built from them. A release
needs real evidence: Lab runs that ended (finished, failed or timed_out) on exactly the tree being released. Whether
the evidence shows an improvement is Meta's call, so a release record always says benefit 未评估.
"""
import hashlib
import json
import os
import re
import socket
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rsi.codexrun import run_codex
from rsi.compose import load_envelope, meta_config, tool_server_problems, write_home_config
from rsi.decision import SCHEMA, Decision, parse_decision
from rsi.labrun import (WORKSPACE_ERRORS, admit, discard, dispose, end_interrupted, move_in, noting_refusals, now,
                        open_store, problem, process_recorder, refusing, rename_in, restore_work, root_descriptor,
                        run_trial, stage, stamp, take_work, unrecovered, usable_codex_version)
from rsi.limits import Limits
from rsi.paths import Layout, Refused
from rsi.records import CALL_ID, Records
from rsi.sessions import collect_sessions
from rsi.settings import load_settings, show, workspace_problems
from rsi.versions import AUTHOR, VERSION, VersionStore
from rsi.workspace import has_entry, kind, open_folder, prepare_for_commit, seal

INSTRUCTIONS = Path('meta', 'AGENTS.md')        # under owner/: Meta's standing instructions, the first prompt
META_SETTINGS = Path('meta', 'settings.toml')   # under owner/: Meta's model and reasoning effort
META_KEYS = ('model', 'model_reasoning_effort')
RUN_ENDED = ('finished', 'failed', 'timed_out')  # a Lab run that counts as evidence; an interrupted one does not
BENEFIT = '未评估'
RELEASE_FILE = re.compile(r'V[0-9]{4,}\.json')
TEXT_KEPT = 50_000                              # characters of an invalid answer kept in the round record
WRITE_TRIES = 3                                 # tries to claim an application id
DEFAULT_INSTRUCTIONS = """你是 Meta Codex：在任务之间改进 Lab Codex 的工作区（它的指示、技能、设置、子 agent 角色和工具服务）。

- 先核对记录再下判断，判断里引用记录编号。
- 单次现象不当作规律；依据不足就不改。
- 一次只做一个小改动。
- 不把某个任务的答案写进 Lab 的指示。
- 写明可能的退化和拿不准的地方；不声称收益。
- 要扩大 Lab 的外部范围（连接器、插件、工作区以外的文件、预算上限），写申请，由主人决定。
"""
DECISION_GUIDE = """每次调用最后只交回一个 JSON 对象（格式见输出约束），decision 三选一：

- no_change：不改，本轮结束。
- trial：在 trials 里列出要试跑的项。每项给 task（已有任务的编号）或 probe（你写的一段测试文字，没有检查脚本，\
结果由你自己看）二者之一，version 写 candidate（现在的候选版）或 current（当前版本）。控制程序先把候选版存成快照，\
再在 trial/ 里逐项运行，然后在同一会话里告诉你新的调用编号、状态和检查结果。
- release：发布现在的候选版，在 evidence 里列出作为依据的运行记录。控制程序只核对：本轮还没发布过；候选版是从当前\
版本改出来的，并且确实有改动；设置文件和角色文件里只有允许改的键，没有 .codex；引用的每条记录都是已经结束的 Lab \
运行，跑的内容和现在的候选版完全一样（改了候选版之后，之前的试跑就不再算）。结果好不好由你判断。

observations 的 record、evidence 和 applications 的 records 都只能是调用编号（R 加数字，例如 R0002）。\
要扩大 Lab 的外部范围，写在 applications 里，由主人决定。"""


def _owner_text(path: Path) -> str | None:
    """A file of owner/ (which no agent can reach) as text; None when it is missing."""
    try:
        return path.read_bytes().decode('utf-8')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise Refused(f'{path} 读不了：{error}') from None


def _instructions(layout: Layout) -> str:
    text = _owner_text(layout.owner / INSTRUCTIONS)
    return DEFAULT_INSTRUCTIONS if text is None or not text.strip() else text


def _meta_settings(layout: Layout) -> dict:
    """owner/meta/settings.toml: model and model_reasoning_effort (meta_config checks the values); {} if missing."""
    path = layout.owner / META_SETTINGS
    text = _owner_text(path)
    if text is None:
        return {}
    try:
        table = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise Refused(f'{path} 不是合法的 TOML：{" ".join(str(error).split())}') from None
    unknown = sorted(set(table) - set(META_KEYS))
    if unknown:
        raise Refused(f'{path} 里有不认识的键：{"、".join(unknown)}（只能有 {"、".join(META_KEYS)}）')
    return table


def _check_text(check) -> str:
    if not isinstance(check, dict):
        return '没有检查脚本'
    message = check.get('message')
    return ('通过' if check.get('passed') is True else '没通过') + (f'（{message}）' if message else '')


def _bullets(title: str, lines: list[str]) -> str:
    return '\n'.join([title, *(f'- {line}' for line in lines)])


def _recordable(value):
    """value with every text UTF-8 cannot hold (a lone surrogate) escaped as \\udXXX, so that it can be recorded."""
    if isinstance(value, str):
        return value.encode('utf-8', 'backslashreplace').decode('utf-8')
    if isinstance(value, dict):
        return {_recordable(key): _recordable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_recordable(item) for item in value]
    return value


def _write(records: Records, relative: str, data: dict, problems: list[str]) -> bool:
    """records/<relative> with data; whether it was written. It runs after main may have moved, so it never raises:
    text UTF-8 cannot hold is escaped (with a problem line), and any other failure is a problem line (and, best
    effort, an `unrecorded` index note)."""
    for attempt in range(2):
        try:
            records.write_json(relative, data)
            return True
        except UnicodeEncodeError:
            if attempt:
                problems.append(f'没能写记录 {relative}：转义以后仍不能存成 UTF-8')
                break
            problems.append(f'{relative} 里有不能存成 UTF-8 的字符，已转义后保存')
            data = _recordable(data)
        except Exception as error:
            problems.append(f'没能写记录 {relative}：{type(error).__name__}: {error}')
            break
    try:
        records.note('unrecorded', {'path': relative, 'reason': problems[-1]})
    except Exception:
        pass
    return False


def _save_patch(records: Records, version: str, text: str) -> tuple[str, str]:
    """records/releases/<version>.patch, created once; its path relative to records/ and its sha256."""
    folder = records.root / 'releases'
    try:
        os.mkdir(folder)
    except FileExistsError:
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f'{folder} 不是普通文件夹') from None
    data = text.encode('utf-8')
    with open(folder / f'{version}.patch', 'xb') as stream:
        stream.write(data)
    return f'releases/{version}.patch', hashlib.sha256(data).hexdigest()


def _released_in(records: Records, round_id: str) -> str | None:
    """The version a release record of this round names, if there is one."""
    folder = records.root / 'releases'
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return None
    for name in names:
        if not RELEASE_FILE.fullmatch(name):
            continue
        try:
            data = json.loads((folder / name).read_bytes())
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(data, dict) and data.get('round') == round_id:
            return name.removesuffix('.json')
    return None


def release_problems(store: VersionStore, records: Records, *, evidence: list[str], tree: str, base: str,
                     round_id: str, problems: list[str] = ()) -> list[str]:
    """Why the candidate whose tree is `tree` may not be released: the four checks of design §5.6, one Chinese line
    per problem, each naming the version, key or record it is about; empty means release.

    base is main's head commit when the round cut the candidate; problems are the candidate's settings and
    tool-server problems (check 3), found on its sealed tree. Evidence is compared by tree id: a Lab run counts
    when it ended (finished, failed or timed_out) on exactly this tree."""
    out = []
    done = _released_in(records, round_id)                                         # 1
    if done is not None:
        out.append(f'本轮（{round_id}）已经发布过 {done}，一轮只能发布一次')
    head = store.head()                                                            # 2
    if tree == store.tree_of(head):
        out.append(f'候选版和当前版本 {head} 相比没有改动')
    if store.resolve(head) != base:
        out.append(f'候选版不是从现在的当前版本 {head} 改出来的（本轮开始以后版本变了）')
    out += list(problems)                                                          # 3
    if not evidence:                                                               # 4
        out.append('没有引用任何运行记录，至少要引用一条')
    checked = []
    for call_id in evidence:
        if call_id in checked:                                                     # each id once, in order
            continue
        checked.append(call_id)
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            out.append(f'{show(call_id)} 不是调用编号')
            continue
        try:
            call = records.load_call(call_id)
        except KeyError:
            out.append(f'{call_id} 不存在')
            continue
        except ValueError:
            out.append(f'{call_id} 的记录读不出来')
            continue
        if call.get('role') != 'lab':
            out.append(f'{call_id} 不是 Lab 的运行（是 {show(call.get("role"))} 的调用），不能作为依据')
        elif call.get('status') not in RUN_ENDED:
            out.append(f'{call_id} 的状态是 {show(call.get("status"))}，不是 {"、".join(RUN_ENDED)} 之一，不能作为依据')
        elif call.get('tree') != tree:
            out.append(f'{call_id} 跑的内容和现在的候选版不一样（改了候选版之后要重新试跑），不能作为依据')
    return out


def _put_candidate(layout: Layout, records: Records, store: VersionStore, root_fd: int, source: str,
                   found: str | None, context: dict, problems: list[str]) -> None:
    """Stage `source` (a version or commit) and rename it in as candidate/, after sealing whatever sits there: it
    is removed when found is None (the previous round's candidate), else kept under sealed/ with `found` as the
    reason. The staged folder is removed if this fails; Refused when `source` holds `.codex`."""
    staged, _ = stage(layout, store, source)
    try:
        label = stamp()
        entry = seal(root_fd, 'candidate', label)
        if entry is not None:
            dispose(layout, records, root_fd, entry, 'candidate', label, [] if found is None else [found], context,
                    problems)
        rename_in(staged, root_fd, 'candidate')
    except BaseException:
        discard(staged)
        raise


@dataclass
class _Taken:
    """The candidate as the program took it for a decision."""
    snapshot: str | None = None                            # its commit on refs/rounds/<round>
    tree: str | None = None                                # its tree, when it could be stored
    problems: list[str] = field(default_factory=list)     # why there is no snapshot
    notes: list[str] = field(default_factory=list)        # what taking it changed, for Meta


@dataclass
class _Round:
    layout: Layout
    records: Records
    store: VersionStore
    root_fd: int
    envelope: dict
    config: dict
    limits: Limits
    resolve: Callable
    codex: str
    round_id: str
    version: str                    # main's head when the candidate was cut
    base: str                       # its commit
    problems: list[str]
    context: dict = field(default_factory=dict)
    seen: set = field(default_factory=set)
    thread: str | None = None
    last: str | None = None         # the round's last snapshot
    released: str | None = None
    final: str | None = None        # the candidate kept at the end of a round without a release (a snapshot)
    calls: list[str] = field(default_factory=list)
    trials: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    applications: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)   # for the next prompt
    trials_run: int = 0

    def run(self, trigger: list[str], instructions: str) -> dict:
        """Call Meta, carry out each decision and answer, until the round ends; write and return its record."""
        started = now()
        schema = self.records.write_json(f'rounds/{self.round_id}/schema.json', SCHEMA)
        self.seen = {call['task'] for call in self.records.calls() if isinstance(call.get('task'), str)}
        first = f'{instructions.rstrip()}\n\n{self._brief(trigger)}'
        prompt, outcome, reason, invalid = first, None, None, 0
        while outcome is None:
            if len(self.calls) >= self.limits.meta_calls:
                outcome = 'limit_reached'
                reason = f'本轮的 Meta 调用到了 {self.limits.meta_calls} 次的上限（owner/limits.toml 的 meta.calls_per_round）'
                break
            refused = self._refusal()
            if refused is not None:
                outcome, reason = 'limit_reached', refused
                break
            if self.notices:                               # what the program had to do before this call
                prompt = f'{prompt}\n\n{_bullets("另外：", self.notices)}'
                self.notices = []
            call, text = self._call(prompt, schema)
            entry = {'call': call['id'], 'status': call['status'], 'decision': None}
            self.decisions.append(entry)
            try:
                decision = parse_decision(text)
            except ValueError as error:
                invalid += 1
                entry.update(invalid=str(error), text=text[:TEXT_KEPT])
                if invalid >= 2:
                    outcome, reason = 'invalid_decision', f'Meta 两次交回的都不是合规的决定（{call["id"]}：{error}）'
                    break
                reply = f'你交回的不是合规的决定：{error}。请只交回一个符合格式的 JSON 决定。'
            else:
                entry['decision'] = decision.as_dict()
                entry['applications'] = self._applications(decision, call['id'])
                outcome, reason, reply = self._carry_out(decision, call['id'], entry)
            if outcome is None:
                entry['reply'] = reply
                reply = f'{reply}\n\n{self._left()}'
                prompt = reply if self.thread is not None else f'{first}\n\n{reply}'
        if self.released is None:
            self._keep_final()
        record = {'id': self.round_id, 'trigger': list(trigger), 'started': started, 'ended': now(),
                  'outcome': outcome, 'reason': reason, 'version_before': self.version,
                  'version_after': self.released, 'final_candidate': self.final, 'calls': self.calls,
                  'decisions': self.decisions,
                  'trials': self.trials, 'applications': self.applications,
                  'limits': {'calls': self.limits.meta_calls, 'trials': self.limits.meta_trials,
                             'seconds_per_call': self.limits.meta_seconds},
                  'problems': self.problems}
        _write(self.records, f'rounds/{self.round_id}/round.json', record, self.problems)
        return record

    # The prompts.

    def _trigger_line(self, call_id: str) -> str:
        try:
            call = self.records.load_call(call_id)
        except KeyError:
            return f'- {call_id}：没有这条记录'
        except ValueError:
            return f'- {call_id}：记录读不出来'
        status = call.get('status')
        if status not in RUN_ENDED:                   # interrupted (or not ended): its status only
            return f'- {call_id}：状态 {show(status)}'
        task = call.get('task')
        what = f'任务 {task}' if isinstance(task, str) else '测试文字' if call.get('probe') else '没有任务'
        return f'- {call_id}：{what}，状态 {status}，检查：{_check_text(call.get("check"))}'

    def _brief(self, trigger: list[str]) -> str:
        layout = self.layout
        lines = [f'# Meta 轮 {self.round_id}', '', f'当前版本：{self.version}。', '']
        if trigger:
            lines += ['触发本轮的调用：', *(self._trigger_line(call_id) for call_id in trigger)]
        else:
            lines.append('这一轮是单独运行的，没有触发它的调用。')
        lines += ['', f'你的工作目录是 meta/，即 {layout.meta}。',
                  f'- 记录：{layout.records}（只读）。每次 Codex 调用在 runs/<编号>/，每轮 Meta 在 rounds/<编号>/，'
                  '发布和回退在 releases/，申请在 applications/。',
                  f'- Lab 工作区：{layout.work}（只读），就是当前版本。',
                  f'- 候选版：../candidate，即 {layout.candidate}（可写）。本轮开始时它是当前版本的副本；'
                  '试跑和发布用的都是它。',
                  f'- 你的笔记：notes/，即 {layout.notes}（可写），各轮之间保留。', '', self._left(), '',
                  DECISION_GUIDE]
        return '\n'.join(lines)

    def _left(self) -> str:
        calls = max(self.limits.meta_calls - len(self.calls), 0)
        trials = max(self.limits.meta_trials - self.trials_run, 0)
        return (f'本轮还剩：Meta 调用 {calls} 次（含读到这段话的这一次），试跑 {trials} 次；'
                f'每次 Meta 调用最长 {self.limits.meta_seconds} 秒。')

    # One Meta call.

    def _refusal(self) -> str | None:
        """Why no further Meta call may start, noted; or None. Checked before every call: the gate and the owner's
        daily caps (the first call was admitted when the round started), then meta/ and candidate/ as real folders.
        Meta's profile grants both by path, so a link Meta left at either name could point the next call's writes
        elsewhere; when one cannot be made a real folder again, no further call starts."""
        reason = None
        try:
            if self.calls:
                admit(self.layout, self.records)
        except (Refused, ValueError) as error:                 # ValueError: owner/limits.toml has become invalid
            reason = str(error)
        for name, step in (('meta/', self._meta_folder), ('candidate/', self._candidate_folder)):
            if reason is not None:
                break
            try:
                step()
            except (Refused, *WORKSPACE_ERRORS) as error:
                reason = f'{name} 不是可用的文件夹，也没能换成一个（{type(error).__name__}: {error}），不再启动 Meta'
        if reason is not None:
            self.records.note('refusal', {'reason': reason, **self.context})
        return reason

    def _candidate_folder(self) -> None:
        """candidate/ as a real folder before a Meta call. A link or file there is sealed and kept under sealed/,
        and (as when it is missing) candidate/ starts again from the round's last snapshot or the current version;
        the next prompt tells Meta. Raises when that cannot be done: an entry that cannot be sealed (a lock flag on
        a link), a failed checkout."""
        root_fd = self.root_fd
        found = kind('candidate', root_fd)
        if found == 'folder':
            return
        what = '不见了' if found is None else '被换成了符号链接' if found == 'link' else '被换成了文件'
        kept = ''
        if found is not None:
            label = stamp()
            entry = seal(root_fd, 'candidate', label)
            if entry is not None:
                dispose(self.layout, self.records, root_fd, entry, 'candidate', label,
                        [f'candidate/ 在两次 Meta 调用之间{what}'], self.context, self.problems)
                kept = '控制程序没有跟着它走，已把它留档；'
        _put_candidate(self.layout, self.records, self.store, root_fd, self.last or self.base,
                       '它在控制程序处理候选版时出现，没有用它', self.context, self.problems)
        source = '本轮上一次存的快照' if self.last is not None else f'当前版本（{self.version}）'
        notice = f'candidate/ {what}，{kept}candidate/ 已换成{source}'
        self.notices.append(notice)
        self.problems.append(notice)

    def _meta_folder(self) -> None:
        """meta/ as a real folder, with meta/.tmp and meta/notes created when missing, by mkdir relative to
        descriptors (never through a link); nothing in meta/ is read. A link or file at meta/ is sealed and kept
        under sealed/: Codex would start Meta wherever it points. OSError if meta/ cannot be opened."""
        root_fd = self.root_fd
        if kind('meta', root_fd) not in (None, 'folder'):
            label = stamp()
            entry = seal(root_fd, 'meta', label)
            if entry is not None:
                dispose(self.layout, self.records, root_fd, entry, 'meta', label, ['meta/ 不是文件夹（可能是符号链接）'],
                        self.context, self.problems)
        try:
            os.mkdir('meta', 0o755, dir_fd=root_fd)
        except FileExistsError:
            pass
        fd = open_folder('meta', root_fd)
        try:
            for name, mode in (('.tmp', 0o700), ('notes', 0o755)):
                try:
                    os.mkdir(name, mode, dir_fd=fd)
                except FileExistsError:
                    pass
        finally:
            os.close(fd)

    def _call(self, prompt: str, schema: Path) -> tuple[dict, str]:
        """One Meta call in meta/, on the round's thread once there is one; its finished record and Meta's answer."""
        records = self.records
        call_id = records.new_id('R')
        call_dir = records.call_dir(call_id)
        records.start_call(call_id, {'role': 'meta', 'purpose': 'meta', 'cost_class': 'meta', 'round': self.round_id,
                                     'task': None, 'probe': None, 'workspace': 'meta', 'codex_version': self.codex,
                                     'timeout_seconds': self.limits.meta_seconds, 'version_before': self.version,
                                     'resume': self.thread, 'started': now()})
        self.calls.append(call_id)
        try:
            result = run_codex(home=self.layout.meta_home, cwd=self.layout.meta, prompt=prompt, config=self.config,
                               record_dir=call_dir, timeout=self.limits.meta_seconds, resume=self.thread,
                               schema=schema, on_start=process_recorder(records, call_id), root=self.layout.root)
        except BaseException:
            end_interrupted(records, call_id)
            raise
        end = {'status': result.status, 'exit_code': result.exit_code, 'thread_id': result.thread_id,
               'usage': result.usage, 'command': result.command, 'ended': now(), 'subagent_threads': []}
        problems = []
        try:
            if result.thread_id is not None:
                with problem(problems, '没能取出会话记录'):
                    end['subagent_threads'] = collect_sessions(self.layout.meta_home, result.thread_id,
                                                               call_dir / 'sessions')
        except BaseException as error:                  # only an interruption gets here
            problems.append(f'取会话记录时被打断（{type(error).__name__}）')
            try:
                records.finish_call(call_id, {**end, 'problems': problems})
            except Exception:
                pass
            raise
        call = records.finish_call(call_id, {**end, 'problems': problems})
        if self.thread is None:
            self.thread = result.thread_id
        return call, result.last_message

    # What a decision leads to.

    def _applications(self, decision: Decision, call_id: str) -> list[str]:
        """records/applications/<id>.json for each application of the decision, status pending; their ids."""
        ids = []
        for application in decision.applications:
            fields = _recordable(application)          # parse_decision already refuses what UTF-8 cannot hold
            for _ in range(WRITE_TRIES):
                app_id = self.records.new_id('A')
                try:
                    self.records.write_json(f'applications/{app_id}.json',
                                            {'id': app_id, 'status': 'pending', 'round': self.round_id,
                                             'call': call_id, **fields})
                except FileExistsError:
                    continue
                except Exception as error:
                    self.problems.append(f'{call_id} 的申请“{show(fields["what"])}”没能存下：{type(error).__name__}: '
                                         f'{error}')
                    break
                ids.append(app_id)
                break
            else:
                self.problems.append(f'{call_id} 的申请“{show(fields["what"])}”没能存下')
        self.applications += ids
        return ids

    def _carry_out(self, decision: Decision, call_id: str, entry: dict) -> tuple[str | None, str | None, str]:
        """(outcome when the round ends here, its reason, the reply to Meta otherwise)."""
        if decision.kind == 'no_change':
            entry['result'] = {'ended': 'no_change'}
            return 'no_change', None, ''
        if decision.kind == 'trial':
            if len(self.calls) >= self.limits.meta_calls:
                entry['result'] = {'ended': 'limit_reached'}
                return 'limit_reached', (f'Meta 要求试跑，但本轮的 Meta 调用已经用完（{self.limits.meta_calls} 次），'
                                         '没有运行任何试跑'), ''
            return None, None, self._trials(decision, call_id, entry)
        reply = self._release(decision, call_id, entry)
        if self.released is not None:
            return 'released', None, ''
        return None, None, reply

    def _trials(self, decision: Decision, call_id: str, entry: dict) -> str:
        taken = self._take_candidate(call_id)
        if taken.snapshot is None:
            entry['result'] = {'refused': taken.problems, 'notes': taken.notes}
            return _bullets('候选版有问题，这次的试跑一项也没有运行：', taken.problems + taken.notes) + '\n请改好以后再给出决定。'
        lines, ran, not_run = [], [], []
        for number, trial in enumerate(decision.trials, start=1):
            what = f'任务 {trial["task"]}' if trial['task'] is not None else '测试文字'
            label = f'第 {number} 项（{what}，{"候选版" if trial["version"] == "candidate" else "当前版本"}）'
            if self.trials_run >= self.limits.meta_trials:
                not_run.append(label)
                continue
            try:
                commit = taken.snapshot if trial['version'] == 'candidate' else self.store.resolve(self.store.head())
                call = run_trial(self.layout, commit=commit, round_id=self.round_id, task_id=trial['task'],
                                 probe=trial['probe'], resolve=self.resolve)
            except (Refused, *WORKSPACE_ERRORS) as error:
                lines.append(f'{label}没有运行：{error}')
                continue
            self.trials_run += 1
            seen = 'probe' if trial['probe'] is not None else 'seen' if trial['task'] in self.seen else 'new'
            self.trials.append({'id': call['id'], 'task': trial['task'], 'probe': trial['probe'],
                                'version': trial['version'], 'commit': commit, 'tree': call.get('tree'),
                                'status': call.get('status'), 'check': call.get('check'), 'task_seen': seen})
            ran.append(call['id'])
            lines.append(f'{call["id"]}：{label}，状态 {call.get("status")}，检查：{_check_text(call.get("check"))}')
        if not_run:
            lines.append(f'本轮的试跑次数（{self.limits.meta_trials} 次）用完了，没有运行：{"；".join(not_run)}')
        entry['result'] = {'trials': ran, 'lines': lines, 'notes': taken.notes}
        return _bullets('试跑结果（记录在 records/runs/<编号>/）：', lines + taken.notes)

    def _release(self, decision: Decision, call_id: str, entry: dict) -> str:
        taken = self._take_candidate(call_id)
        problems = taken.problems
        if taken.tree is not None:
            try:
                problems = release_problems(self.store, self.records, evidence=decision.evidence, tree=taken.tree,
                                            base=self.base, round_id=self.round_id, problems=taken.problems)
            except WORKSPACE_ERRORS as error:
                problems = [f'没能核对发布的条件（{type(error).__name__}: {error}）']
        if not problems:
            problems = self._publish(decision, call_id, taken)
        if not problems:
            entry['result'] = {'released': self.released}
            return ''
        entry['result'] = {'refused': problems, 'notes': taken.notes}
        return _bullets('没有发布，原因：', problems + taken.notes)

    def _publish(self, decision: Decision, call_id: str, taken: _Taken) -> list[str]:
        """Make the snapshot the next version, put it into work/, and write the release record; [] when that
        happened, else why not. What sits in work/ is sealed and versioned first (as before a task run): if that
        makes a new version, the candidate is no longer cut from the current one and is not released."""
        layout, store = self.layout, self.store
        try:
            take_work(layout, self.records, store, self.root_fd, self.records.calls(), self.context, self.problems)
        except Refused as refused:
            return [f'{refused}，所以没有发布']
        reasons, version = [], None
        try:
            head = store.head()
            if store.resolve(head) != self.base:
                reasons.append(f'work/ 里有本轮开始以后的改动，已存成版本 {head}；候选版不再是从当前版本改出来的')
            else:
                version = store.commit_tree(taken.snapshot, f'meta {self.round_id}',
                                            f'Meta 轮 {self.round_id} 发布（决定 {call_id}，依据 '
                                            f'{"、".join(decision.evidence)}）')
                if version is None:
                    reasons.append(f'候选版和当前版本 {head} 相比没有改动')
        except WORKSPACE_ERRORS as error:
            reasons.append(f'没能存成新版本（{type(error).__name__}: {error}）')
        settings = None
        with problem(self.problems, 'work/ 没能换成最新版本'):
            staged, settings = stage(layout, store, store.head())
            move_in(layout, self.records, self.root_fd, staged, 'work', self.context, self.problems)
        if version is None:
            return reasons
        self.released = version
        if settings is not None:
            with problem(self.problems, 'Lab 的 Codex 配置（codex-home/config.toml）没能更新'):
                write_home_config(layout, self.envelope, settings, resolve=self.resolve)
        diff = digest = None
        with problem(self.problems, f'没能保存 {self.version} 到 {version} 的差异'):
            diff, digest = _save_patch(self.records, version, store.diff(self.version, version))
        record = {'kind': 'release', 'version': version, 'parent': self.version, 'round': self.round_id,
                  'call': call_id, 'author': f'meta {self.round_id}', 'time': now(), 'snapshot': taken.snapshot,
                  'tree': taken.tree, 'diff': diff, 'diff_sha256': digest, 'decision': decision.notes,
                  'evidence': decision.evidence, 'trials': self.trials, 'benefit': BENEFIT}
        _write(self.records, f'releases/{version}.json', record, self.problems)
        return []

    # The candidate.

    def _take_candidate(self, call_id: str) -> _Taken:
        """Seal candidate/ and snapshot it when it can be stored and has no settings or tool-server problems; then
        candidate/ is a fresh checkout of the snapshot. A tree with problems is renamed back as candidate/ (Meta
        fixes it); one that cannot be stored is kept under sealed/, and candidate/ starts again from the round's
        last snapshot or the current version."""
        layout, root_fd, label = self.layout, self.root_fd, f'{self.round_id}-{call_id}'
        context, taken, keep = {**self.context, 'call': call_id}, _Taken(), []      # keep: reasons to keep the tree
        try:
            entry = seal(root_fd, 'candidate', label)
        except OSError as error:
            taken.problems.append(f'candidate/ 封存不了（{error.strerror or error}），没有用它；'
                                  '请检查 candidate/ 本身的标志和权限')
            return taken
        if entry is None:
            taken.problems.append('candidate/ 不在了')
            self._start_over(taken, context)
            return taken
        path, found = layout.root / entry, kind(entry, root_fd)
        if found != 'folder':
            reason = 'candidate/ 被换成了符号链接' if found == 'link' else 'candidate/ 不是文件夹'
            keep.append(reason)
            taken.problems.append(f'{reason}；控制程序不会跟着它走，也不会把它存成快照')
        else:
            try:
                lines, moved, blocked = prepare_for_commit(path, layout.sealed / label / 'left-out',
                                                           f'{layout.sealed.name}/{label}/left-out')
                taken.notes += lines
                if moved:
                    keep.append(f'有 {moved} 处内容没有存进快照，已移到 {layout.sealed.name}/{label}/left-out/')
                if blocked:
                    keep.append(f'有 {blocked} 处应排除在快照之外的内容没能移出去')
                    taken.problems.append(f'候选版里有 {blocked} 处应排除在版本之外的内容没能移出去，没有存成快照')
                elif os.path.lexists(path / '.codex'):
                    keep.append('里面有 .codex')
                    taken.problems.append('候选版里有 .codex，不能存成快照，也不能试跑或发布')
                else:
                    found_problems = workspace_problems(path) + tool_server_problems(
                        self.envelope, load_settings(path), resolve=self.resolve)
                    if found_problems:
                        taken.tree = self.store.tree_of_dir(path)
                        taken.problems += found_problems
                    else:
                        taken.snapshot = self.store.snapshot(path, self.round_id,
                                                             f'Meta 轮 {self.round_id} 的候选版（决定 {call_id}）')
                        taken.tree = self.store.tree_of(taken.snapshot)
            except WORKSPACE_ERRORS as error:
                taken.tree = taken.snapshot = None
                reason = f'候选版没能存成快照（{type(error).__name__}: {error}）'
                keep.append(reason)
                taken.problems.append(reason)
        if taken.snapshot is not None:
            self.last = taken.snapshot
            self._restage(taken.snapshot, taken.notes, context)
            dispose(layout, self.records, root_fd, entry, 'candidate', label, keep, context, taken.notes)
        elif taken.tree is None or not self._rename_back(entry, label, keep, context, taken):
            dispose(layout, self.records, root_fd, entry, 'candidate', label, keep, context, taken.notes)
            self._start_over(taken, context)
        return taken

    def _rename_back(self, entry: str, label: str, keep: list[str], context: dict, taken: _Taken) -> bool:
        """Put the sealed tree (which has problems but could be stored) back as candidate/, so Meta can fix it;
        whether that worked. keep: what was left out of it, now under sealed/<label>/left-out/ (noted); when the
        rename fails, the reason is added and the caller keeps the tree under sealed/."""
        try:
            os.rename(entry, 'candidate', src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)
        except OSError as error:
            keep.append(f'没能放回 candidate/（{error.strerror or error}）')
            return False
        if keep:
            self.records.note('sealed', {'workspace': 'candidate', 'reasons': keep, **context,
                                         'path': f'{self.layout.sealed.name}/{label}/left-out'})
        taken.notes.append('candidate/ 还是你改过的样子（没有存成快照），改好上面的问题再给出决定')
        return True

    def _restage(self, source: str, lines: list[str], context: dict) -> bool:
        """candidate/ as a fresh checkout of source; whether that worked (else a line says why)."""
        try:
            _put_candidate(self.layout, self.records, self.store, self.root_fd, source,
                           '它在控制程序处理候选版时出现，没有用它', context, lines)
            return True
        except Exception as error:
            lines.append(f'candidate/ 没能换成新的内容：{type(error).__name__}: {error}')
            return False

    def _keep_final(self) -> None:
        """Keep the candidate of a round that released nothing (§5.5: 没有发布的候选版也留档). It is sealed; when it
        passes the checks a trial snapshot needs and its tree differs from main's head and from the round's last
        snapshot, it is snapshotted on refs/rounds/<round> (final_candidate); then it is renamed back as candidate/.
        One that cannot be snapshotted is kept under sealed/ with a note. Never fails the round: a problem line."""
        layout, store, label = self.layout, self.store, f'final-{self.round_id}'
        context, left, unstorable, lines = {**self.context, 'final': True}, [], [], []
        try:
            entry = seal(self.root_fd, 'candidate', label)
            if entry is None:
                return
            path = layout.root / entry
            if kind(entry, self.root_fd) != 'folder':
                unstorable.append('本轮结束时 candidate/ 不是文件夹（可能是符号链接），没有跟着它走')
            else:
                try:
                    lines, moved, blocked = prepare_for_commit(path, layout.sealed / label / 'left-out',
                                                               f'{layout.sealed.name}/{label}/left-out')
                    if moved:
                        left.append(f'有 {moved} 处内容没有存进快照，已移到 {layout.sealed.name}/{label}/left-out/')
                    if blocked:
                        found = [f'有 {blocked} 处应排除在版本之外的内容没能移出去']
                    elif has_entry(path, '.codex'):
                        found = ['里面有 .codex']
                    else:
                        found = workspace_problems(path) + tool_server_problems(
                            self.envelope, load_settings(path), resolve=self.resolve)
                    if found:
                        unstorable.append(f'本轮结束时的候选版存不成快照：{"；".join(found)}')
                    elif store.tree_of_dir(path) not in {store.tree_of(store.head()),
                                                         *([store.tree_of(self.last)] if self.last else [])}:
                        self.final = store.snapshot(path, self.round_id, f'Meta 轮 {self.round_id} 结束时没有发布的候选版')
                except WORKSPACE_ERRORS as error:
                    unstorable.append(f'本轮结束时的候选版存不成快照（{type(error).__name__}: {error}）')
            self.problems.extend(lines)
            if unstorable or not self._rename_back(entry, label, left, context, _Taken()):
                dispose(layout, self.records, self.root_fd, entry, 'candidate', label, left + unstorable, context,
                        self.problems)
        except Exception as error:
            self.problems.append(f'本轮结束时没能把候选版留档：{type(error).__name__}: {error}')

    def _start_over(self, taken: _Taken, context: dict) -> None:
        """candidate/ from the round's last snapshot, or the current version when there is none."""
        what = '本轮上一次存的快照' if self.last is not None else f'当前版本（{self.version}）'
        if self._restage(self.last or self.base, taken.notes, context):
            taken.problems.append(f'candidate/ 已换成{what}，请在它上面重新改')


def run_round(layout: Layout, trigger: list[str], *, resolve=socket.getaddrinfo) -> dict:
    """Run one Meta round and return its record, written to records/rounds/<id>/round.json.

    trigger: the ids of the calls that started the round (none for a round run on its own). Refused (noted) or
    ValueError before any call. resolve looks up the hosts of URL tool servers (socket.getaddrinfo; tests pass a
    stand-in)."""
    if not isinstance(trigger, list):
        raise ValueError('触发本轮的调用必须是调用编号的列表')
    records = Records(layout.records)
    for call_id in trigger:
        records.call_dir(call_id)                       # ValueError for anything but a call id
    context = {'purpose': 'meta'}
    with noting_refusals(records, context), root_descriptor(layout) as root_fd:
        unrecovered(layout, records)
        limits, calls = admit(layout, records)
        store = open_store(layout)
        envelope = load_envelope(layout.owner)
        config = meta_config(layout, envelope, _meta_settings(layout))
        instructions = _instructions(layout)
        codex = usable_codex_version(layout)
        problems = []
        # What sits in work/ becomes a version first (as before a task run), so the candidate is cut from it.
        take_work(layout, records, store, root_fd, calls, context, problems)
        try:
            with refusing('版本库用不了'):
                version = store.head()
                base = store.resolve(version)
                staged, _ = stage(layout, store, version)
            with refusing('准备好的工作区放不进 work/'):
                move_in(layout, records, root_fd, staged, 'work', context, problems)
        except Refused as refused:
            raise Refused(f'{refused}{restore_work(layout, records, store, root_fd, context)}') from None
        with refusing('候选版准备不了'):
            _put_candidate(layout, records, store, root_fd, base, None, context, problems)
        round_id = records.new_id('M')
        state = _Round(layout, records, store, root_fd, envelope, config, limits, resolve, codex, round_id, version,
                       base, problems, context={'purpose': 'meta', 'round': round_id})
        return state.run(trigger, instructions)


def rollback(layout: Layout, version: str, who: str, *, resolve=socket.getaddrinfo) -> str:
    """A new version on main with the content of an earlier version, by `who` (the author, e.g. owner); work/ then
    holds it, and records/releases/<new version>.json says kind rollback. What sits in work/ is versioned first (as
    before a task run). ValueError for a bad version name or author, a version the store lacks, or one whose content
    is the current version's; Refused (noted) when the store cannot take a version."""
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError(f'不是版本名：{show(version)}。版本名形如 V0001。')
    if not isinstance(who, str) or not AUTHOR.fullmatch(who):
        raise ValueError(f'回退人的名字不合规：{show(who)}')
    records = Records(layout.records)
    context = {'command': 'rollback', 'version': version}
    with noting_refusals(records, context), root_descriptor(layout) as root_fd:
        unrecovered(layout, records)
        store = open_store(layout)
        store.resolve(version)
        envelope = load_envelope(layout.owner)
        problems = []
        take_work(layout, records, store, root_fd, records.calls(), context, problems)
        settings = None
        try:
            parent = store.head()
            new = store.commit_tree(version, who, f'回退到 {version}（{who}）')
        finally:                                        # work/ was sealed: main's head goes back into it
            with problem(problems, 'work/ 没能换成最新版本'):
                staged, settings = stage(layout, store, store.head())
                move_in(layout, records, root_fd, staged, 'work', context, problems)
        if new is None:
            raise ValueError(f'当前版本 {parent} 的内容已经和 {version} 一样，不需要回退')
        if settings is not None:
            with problem(problems, 'Lab 的 Codex 配置（codex-home/config.toml）没能更新'):
                write_home_config(layout, envelope, settings, resolve=resolve)
        diff = digest = tree = None                     # main has moved: from here on nothing raises
        with problem(problems, f'没能保存 {parent} 到 {new} 的差异'):
            diff, digest = _save_patch(records, new, store.diff(parent, new))
        with problem(problems, f'没能读出 {new} 的树'):
            tree = store.tree_of(new)
        _write(records, f'releases/{new}.json', {
            'kind': 'rollback', 'version': new, 'parent': parent, 'target': version, 'round': None, 'who': who,
            'author': who, 'time': now(), 'tree': tree, 'diff': diff, 'diff_sha256': digest,
            'decision': None, 'evidence': [], 'trials': [], 'benefit': BENEFIT, 'problems': problems}, problems)
    return new
