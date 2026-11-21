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
./rsi install-literature install the Literature server into an existing workspace, preserving its rollback version
./rsi install-memory     install Scientific Memory retrieval into an existing workspace
./rsi memory backfill    register eligible completed task records (optional arguments: run IDs)
./rsi memory status      inspect the ledger version, evidence counts and registrations
./rsi memory verify      verify the ledger against registration reports and original files
./rsi memory source-changed <sha256>    flag a registered original for review
./rsi memory withdraw <id> <revision>   withdraw one exact object revision
./rsi memory show <id>   inspect the revisions and deliveries of a memory object
./rsi check              measure the isolation rules with no model call (about 200 cases)
./rsi task <id>          run tasks/<id>/task.md with the Lab, then one Meta round (--no-meta: task only)
./rsi meta               one Meta round over task runs no round has looked at
./rsi status             current version, recent calls and releases, pending applications, usage
./rsi rollback <version> go back to a version
./rsi recover            after an interruption: stop what is left and tidy up
```

A task is `tasks/<id>/task.md` with four sections (`## Goal`, `## Input`, `## Requirements`,
`## Completion criteria`) and an optional `check.py` that neither agent can read.

## Literature

New workspaces include the local `literature` MCP server. For an existing installation run
`./rsi install-literature` and `./rsi check`. The Lab can delegate to its Literature sub-agent for
Europe PMC, PubMed/PMC, OpenAlex and Asta searches, full-text reading and citation lookup. Poppler extracts
PDF text and renders individual pages. The server uses the Codex network proxy independently of Lab commands.

The shared library keeps each original in `library/objects/sha256/` and its reading manifest and text in
`library/documents/`. Trial runs can read those originals. Each managed run with the service enabled gets a
`literature.jsonl` attachment in its record. For the Stage L acceptance task, run
`./rsi task lit-service --no-meta`, then `python3 tasks/lit-service/audit.py records/runs/<call_id> library`.
Records and originals (`records/`, `library/`) are kept only on the machine that ran them and are not in this repository.

Note: the literature server has network access by default and runs code from folders the Lab can write
(`tools/literature/`, `env/`). "Network off for Lab commands" therefore only means the Lab's own commands start
without network; it does not mean the Lab cannot reach the internet. The file rules are unchanged. The server's
call log (`literature.jsonl`) is its own record: the Lab's commands can write to it, so it is not proof that a
call happened. PDFs are read with Poppler only.

## Scientific Memory

The control program registers complete passages, paper identifiers and acquisition outcomes from finalized
Literature tool receipts in the local `memory/` ledger. Each delivery points to a specific revision. The Lab
searches a workspace snapshot with `lookup_sources`, `recall_passages`, `get_evidence` and `memory_status`.
Original-file hashes are checked at registration. The passage text retains its status as a historical tool return.

On an existing installation, run `./rsi memory backfill` followed by `./rsi install-memory` to put the populated
snapshot into the manual workspace, then run `./rsi check`. Later task runs backfill pending records automatically.
Named trials exclude evidence revisions available only from the same task. `./rsi task <id> --no-meta --memory none`
runs a task with an empty snapshot and skips registration of that run. See the [design](docs/design-memory.md).

On the installation holding the archived ledger, sessions and originals, compare the two completed runs with
`python3 -B tasks/paired-validation-followup/audit.py records/runs/R0015 records/runs/R0016`. The audit
matches the final quotes to actual tool returns and reports the calls, lead token usage and elapsed time for each run.

During a managed task with live memory, `propose_memory` accepts subquestions, judgments, decisions,
investigations and method experience. Cite a retrieved object by ID and revision, or cite a passage by its
address and exact quote. For a complete passage first read in that run, include `delivery_source="literature"`
and the service's `invocation_id`. The controller checks the read and commits the evidence and accepted
proposals in the same batch. Later tasks can use `recall_objects` or `recall_judgments` to retrieve them.

New snapshots carry review status down the chain of references. A changed source or a superseded citation
marks dependent interpretations `needs_review`. A missing original or withdrawn revision makes its pinned
references `unavailable`. The owner commands above return an independent registration receipt. An open
task keeps its starting snapshot. Run `./rsi install-memory` to refresh the manual workspace.

To replay both types of passage citation in a temporary ledger on that installation, run
`python3 -B tasks/paired-validation-followup/replay_judgment.py` and repeat it with `--current-literature`.
The [implementation plan](docs/plan-memory-proposals.md) records the recovery tests and their results.

## Requirements

macOS, Codex CLI 0.170.0 (the check must pass again after any Codex upgrade), Python 3.11 or newer, git. The paths
in the permission profiles assume Homebrew under `/opt/homebrew`.

## Documents

- `docs/design.md` — the design, in Chinese: what is decided, the permission table, what each stage verified, known
  limits. Section 11 lists what changed during the build; section 12 has the results of the first real runs.
- `docs/plan1.md` — the implementation plan the build followed.
- [Plan 2](docs/plan2.md) covers the Literature tool server, the RSI-PABLO code it reuses and the Stage L acceptance run.

## Tests

```bash
python3 -m unittest discover -s control/tests -t control -v
```

The tests drive the real Codex CLI against a scripted local model endpoint, so they make no model call.

## Status

Real-model runs cover the planted-fault recovery and the Literature service. The Stage L acceptance record in
[Plan 2](docs/plan2.md#acceptance-record-november-20-2026) identifies the child run, paper, passage and hashes.
The P4 audit matches each quoted passage to the archived original, the Literature child's
`read_document` response and the controller's copy of the tool log. Replay the five passages from the two
papers in the original run with `python3 tasks/lit-service/audit.py records/runs/R0010 library --task paired-validation`.
The decision memo and Meta's review are described in the [P4 acceptance record](docs/design.md#13-p4-真实科研任务验收2026-11-20).
Open work is tracked in the issues.
