# RSI-MOL

A small control program that runs two Codex CLI agents side by side in one folder:

- the **Lab** does a task the owner writes;
- **Meta** reads the record of that run, edits a candidate copy of the Lab's workspace (its instructions, skills,
  roles, settings, tool servers), asks for trial runs, and decides by itself whether to release the change for the
  next task.

The control program (`rsi`, Python standard library only) keeps the two apart with Codex's own permission profiles,
records every call, versions the Lab workspace, and before a release checks only that the trial records Meta cites
are real, finished, and ran exactly the candidate being released. It never claims a change helped: a release record's
benefit is always `not evaluated`.

## Commands

```text
./rsi init               create the folders, the version store and both Codex configurations
./rsi check              measure the isolation rules with no model call (about 200 cases)
./rsi task <id>          run tasks/<id>/task.md with the Lab, then one Meta round (--no-meta: task only)
./rsi meta               one Meta round over task runs no round has looked at
./rsi status             current version, recent calls and releases, pending applications, usage
./rsi rollback <version> go back to a version
./rsi recover            after an interruption: stop what is left and tidy up
```

A task is `tasks/<id>/task.md` with four sections (`## Goal`, `## Input`, `## Requirements`,
`## Completion criteria`) and an optional `check.py` that neither agent can read.

## Requirements

macOS, Codex CLI 0.170.0 (the check must pass again after any Codex upgrade), Python 3.11 or newer, git. The paths
in the permission profiles assume Homebrew under `/opt/homebrew`.

## Documents

- `docs/design.md` — the design, in Chinese: what is decided, the permission table, what each stage verified, known
  limits. Section 11 lists what changed during the build; section 12 has the results of the first real runs.
- `docs/plan1.md` — the implementation plan the build followed.

## Tests

```bash
python3 -m unittest discover -s control/tests -t control -v
```

The tests drive the real Codex CLI against a scripted local model endpoint, so they make no model call.

## Status

Built and reviewed task by task; installed and run once end to end with real models on a planted fault. Open work is
tracked in the issues.
