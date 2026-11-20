# Plan 2 — Give the Lab's Literature agent a local tool server

**Stage L · Implementation plan · 2026-11-20**

The Lab already delegates literature questions to its native `literature` sub-agent. In R0008 that agent
returned an arXiv identifier and a page reference. This stage gives it tools to retrieve an original, read
addressable passages, follow citations and inspect a PDF page. The acceptance run must connect a passage
in the agent's answer to the bytes in `library/` and to the tool calls in that run's record.
[MOL design §§5.10 and 7](design.md#510-把-literature-放进-lab-codex) · [Stage L issue](https://github.com/KairosPan/RSI-MOL/issues/2)

## What to take from RSI-PABLO

Use the shared Literature package from the RSI-Lab repository used by RSI-PABLO. The earlier
`experiments/pablo/pablo_task/literature/adapter.py` exposes a single search operation. The later
`rsi_literature/` package contains the providers, document reader and PDF tools used for the real-paper
validation. Pin the port to commit **`801197e751cb6f64bcf395cbc1806a04b5357f14`**, package tree
`f20a91b63a034ca572649ad5143b79d2544359e1`. [Source tree](https://github.com/KairosPan/RSI-Lab/tree/801197e751cb6f64bcf395cbc1806a04b5357f14/rsi_literature)

| What RSI-PABLO established | How MOL will use it |
| --- | --- |
| The lead specifies the question, intended use and scope. Literature chooses queries, papers and citation branches, then returns claims, supporting passages, coverage and unresolved questions. | Keep the existing Lab role and handoff. Add instructions for citing the document, version and location actually returned by a read. The lead retains the research decision. |
| The upstream controller schedules Literature through `request_literature`, reserves resources per effect and checks evidence delivery against its ledger. | Codex already handles delegation in MOL. Expose the source operations over stdio MCP and collect their logs after each run. Keep the owner's decision of no per-task Literature call quota. The role instructions and audit trail are the evidence policy here. |
| In the November 15 SAMtools run, Europe PMC supplied JATS full text for `PMC2723002`. Literature read to the last cursor and followed citations in both directions. The PDF endpoint returned HTTP 403. A previously registered Harvard DASH copy supplied page 3 for Poppler. | Reuse the paper and identifiers for live acceptance. Test rendering separately with a known PDF. Report which source actually supplied each original. The published live result was obtained at `ae3dbea`; the pinned merge includes subsequent offline integration work. |

[Upstream role and tool contracts](https://github.com/KairosPan/RSI-Lab/blob/801197e751cb6f64bcf395cbc1806a04b5357f14/docs/literature-agent-design.md) · [Real-paper validation](https://github.com/KairosPan/RSI-Lab/blob/801197e751cb6f64bcf395cbc1806a04b5357f14/docs/literature-real-paper-validation.md)

## 1. Package the source and define the tool interface

- [x] Put the server and its private copy of `rsi_literature/` under `control/seed/work/tools/literature/`.
  This becomes `tools/literature/` in the versioned Lab workspace. Copy the pinned modules byte-for-byte,
  omitting `adapters.py`, which imports `rsi_lab`. Record the repository, commit and copied paths in that
  directory. Verify copied files against the Git blobs. Start the server in a clean temporary root with
  only the seed, the existing Python environment and Poppler available.
- [x] Write a small `server.py` for stdio JSON-RPC and a dispatch module alongside it. Implement the MCP
  initialization handshake, notifications, `tools/list` and `tools/call` using the protocol negotiated with
  the installed Codex. Keep stdout for protocol messages. Test request IDs, unknown methods/tools, malformed
  arguments, tool errors and a successful call on the same connection after an error. Use the real wrapper
  test in [test_compose.py](../control/tests/test_compose.py) as the Codex client example.
- [x] Publish the following eleven operations from the upstream catalog. Bind each schema to the providers
  that actually implement it. Use the input validation, format checks and result contracts in the omitted
  adapter as references. Expose explicit status, provenance, coverage, errors and continuation in tool
  results. An unavailable source must remain distinguishable from a successful query with zero hits.

| MCP tool names | Inputs and observable results |
| --- | --- |
| `list_providers` | Report the five Stage L entries, their operations and configuration status. A live request supplies evidence of connectivity. |
| `search_papers`, `get_paper`, `get_citations` | Use `ProviderClient` for Europe PMC, PubMed/PMC and OpenAlex. Accept a query or paper reference, and a cursor where supported. Map the public directions `references` and `citing` to the upstream client. Return identifiers, source and continuation. |
| `acquire_fulltext` | Given a paper reference, resolve provider candidates and fetch a bounded original. Support XML/PDF preference, validate response media type and bytes, and return document ID, version, original SHA-256 and coverage. Accept a predetermined local source ID only when explicitly registered with a hash, e.g. the PDF fixture. |
| `read_document`, `find_passages` | Use `DocumentStore.read` and `.find`. Accept document IDs and a locator, cursor or query as applicable. Return exact text, section/block or PDF location, document version, source hash, ranges and coverage. Validate a supplied version before reading. |
| `build_reading_package`, `render_document_page` | Use the upstream parser and Poppler on an acquired PDF. Bind a derived text document to its original. For a rendered page, read the bounded PNG produced by Poppler, verify its digest, and return an MCP image block plus page number, image hash and source PDF hash. |
| `provider_tools`, `call_provider_tool` | Use upstream `remote.MCPClient` for Asta at its configured endpoint. Limit discovery and calls to the upstream Asta allowlist. Preserve remote errors and cursors. |

Scite remains available through the existing Codex connector. The explicit five-provider configuration also
keeps Paper2Skill and the upstream `extract_evidence` and `run_review` operations outside the Stage L surface.
Use the existing PDF environment and Poppler executables. Treat credentials and rate limits as provider
conditions. The first release uses access available without adding a local-server secret channel. Preserve
per-request byte, time, page and redirect bounds. Only follow provider-returned public HTTPS locations
through the Codex proxy, validating each redirect and media type. Tool arguments cannot select paths,
endpoints, credentials or executables. [Upstream dispatch and acquisition](https://github.com/KairosPan/RSI-Lab/blob/801197e751cb6f64bcf395cbc1806a04b5357f14/rsi_literature/adapters.py)

## 2. Preserve originals and make cached documents usable in trials

- [x] Store original bytes once at `library/objects/sha256/<source_sha256>`. The upstream `doc_…`
  identifier also depends on the media type, metadata and parser version. Keep a separate
  `library/documents/<document_id>/` entry with the upstream manifest and normalized text. Publish
  the verified content object first and the complete document entry last, using atomic, idempotent writes.
  Verify existing entries before reuse. Record the parent document and original PDF hash for derivatives.
- [x] Run the upstream `DocumentStore` under the active workspace's `.tmp/literature/`. It initializes
  a cursor key there, and Poppler creates jobs, scratch files and images under its store root. On a cache
  hit, materialize the manifest, text and verified `source.bin` from the archive, so the upstream reader
  can serve the same document ID/version. Use the same temporary store across MCP processes in one run.
  Serialize first initialization and publication across processes. Define cursor lifetime as one run;
  a later run can reopen the document by ID and version. Keep PNGs and parser scratch in the workspace.
- [x] In a trial, allow searches, citation lookup and reads of shared cached originals. Render or derive
  from a cached PDF using the trial's own scratch. A request requiring a new shared original returns
  `unavailable` with a specific reason. A fresh, empty library must still permit server startup and
  metadata search. Task runs and manual Lab sessions in `work/` may publish to the shared archive.
  Per-run record collection applies to controller-managed tasks and trials. The next managed run starts
  with fresh scratch, so a manual session's logs cannot be attributed to it.
- [x] Test with a small JATS document and an identifiable two-page PDF. Follow at least two read pages
  and compare text and offsets against the fixture, including multibyte text. Test a wrong version,
  a cursor for another document, repeated and concurrent ingestion, and changed original bytes.
  Reopen a cached document in a fresh trial, check the rendered PNG against the requested page, and
  compare the shared library before and after. Upstream regression examples are under
  [tests/literature](https://github.com/KairosPan/RSI-Lab/tree/801197e751cb6f64bcf395cbc1806a04b5357f14/tests/literature).

## 3. Register the server and measure its actual permissions

- [x] Add this active table to the seed's `codex/settings.toml`. Update the English instructions in
  `codex/agents/literature.toml` to use the new operations and return source identifiers, quoted passages
  and locations. When a tool returns only an abstract, a snippet or an image descriptor, the role must
  describe the material it actually inspected.

  ```toml
  [tool_servers.literature]
  command = "python3"
  args = ["-B", "tools/literature/server.py"]
  network = true
  ```

- [x] Resolve paths from the active workspace selected by the controller. Reuse the existing native Codex
  wrapper, clean wrapper home and proxy in [compose.py](../control/rsi/compose.py). The service gets network
  independently of Lab commands. The general task profile permits writes to `env/`; Stage L's acceptance
  rule permits this service to write only to `work/` and `library/`. In the controller, narrow the
  filesystem table passed to this server's wrapper so `env/` is read-only. For trials its writable root
  is `trial/` alone. Apply this restriction in the composed permissions, independently of the server code.
- [x] Extend the offline isolation checks in [check.py](../control/rsi/check.py). Exercise the composed
  literature wrapper in task and trial mode. Verify allowed workspace writes, task-only library writes
  and denied writes to the other workspace, `env/`, `records/`, `tasks/`, `control/` and owner settings.
  Check local listeners remain unreachable with the proxy enabled. Use a scripted-model test to show that
  Codex discovers the real server and actually calls one tool through stdio.

## 4. Attach each run's tool evidence to its record

- [x] For each completed `tools/call`, append one JSON line under the active workspace's
  `.tmp/literature/calls/`. Use independent process files and unique invocation IDs so concurrent
  sub-agents cannot interleave lines. Include the tool, sanitized inputs, requested and contacted source,
  status/error, identifiers, source SHA-256 when available, and the document version and returned
  locators/ranges or page and PNG hash. Record unsuccessful calls, including attempts that acquired no
  bytes. Omit authorization headers, secret URL parameters, credentials and image payloads.
- [x] In [labrun.py](../control/rsi/labrun.py), collect from the **sealed** task or trial workspace
  before `_settle` discards `.tmp/` and before `Records.finish_call` hashes the call directory. Write the
  validated rows to `records/runs/<call_id>/literature.jsonl`. The controller alone writes records.
  Read only bounded, regular, single-link files through checked directory descriptors, stream rows and
  handle incomplete or malformed input explicitly in `problems`. The log is a service report from an
  agent-writable workspace. Cross-check it against the collected Codex session and the archived bytes when
  judging a live run.
- [x] Cover finished, failed and timed-out calls with partial results. On interruption or recovery, collect
  complete rows when the workspace can be sealed and attributed to that call. Otherwise mark the evidence
  unavailable before finalization. Preserve the original call status and usage. Check unused/disabled
  servers, concurrent calls, an incomplete final line and a second run after recovery. Collect no rows
  from an earlier run. Older records without this attachment must still load.

## 5. Roll out and accept Stage L

1. **Offline regression.** Run `python3 -m unittest discover -s control/tests -t control -v`. Exercise
   the tool list and all eleven operations through stdio with injected responses, actual Poppler for the
   local PDF, real Codex with a scripted model for discovery, and temporary RSI roots for task/trial
   collection. Cover an unavailable provider, Asta tool outside its allowlist, PDF mismatch/403, and
   source-backed pagination. These tests need neither model access nor public literature endpoints.
2. **Fresh and existing installations.** A new root gets the package, settings and role from the seed.
   An existing version store keeps its current workspace when `rsi init` runs. Provide a targeted upgrade
   using the controller's lock and existing seal, commit and restage operations. Capture any pending
   workspace edits first and record that version as the rollback target. Stage from that baseline, add the
   package and merge the settings table and role instructions. Preserve custom settings and roles. Report
   a conflict on differing files or an existing server of the same name before applying the upgrade. On
   success, version the upgrade separately and refresh the composed configuration. A second application
   must be a no-op. Test fresh install, upgrade and conflict in temporary roots. Roll back an upgraded root
   that had pending custom edits and compare its tree to the captured baseline. Run `./rsi check` after
   the controller change and before starting the live task.
3. **Live delegated task.** Provide a dedicated `lit-service` task and run
   `./rsi task lit-service --no-meta`. Keep the original `lit` task for its delegation smoke test.
   Ask the lead to dispatch Literature to find *The Sequence Alignment/Map format and SAMtools*
   (DOI `10.1093/bioinformatics/btp352`), acquire the open JATS original and answer a question about
   the format from a passage in its body. Request at least two successive reads and a citation lookup.
   Budget one live task run, with a second available for a focused verification. Preserve provider failures
   and identify any preloaded PDF separately.
4. **Independent verdict.** Check the child session for calls to the local `literature` tools. Recompute
   SHA-256 from the archived original, reopen the document at the recorded version and reproduce the
   passage and location cited in the answer. Confirm the subsequent read consumed the previous cursor,
   the citation response names its direction, and the controller's call record covers the log attachment
   by hash. Record the call ID, workspace version, paper ID, hashes, location and individual outcomes.
   Mark Stage L complete when this live chain and the offline PDF, trial and isolation checks pass.

## Acceptance record, November 20, 2026

The owner installation captured the existing workspace as **V0005** and installed the server in **V0006**.
A second installation reported the same version. All 334 tests passed in the full offline suite.
Codex CLI 0.170.0 passed 223 isolation checks, including 18
specific to the Literature wrapper. The 18 `literature_*` cases in `records/check.json` run a stand-in script through the
composed wrapper, not the server code. They allow writes in the workspace and library, and block writes to the other
workspace, `env/`, `records/`, `tasks/`, `control/` and the owner settings, and block local TCP. Paths outside the root
and `versions/`, `meta/` and `staging/` were not probed, so the limit to `work/` and `library/` is measured for the
named folders only. The service uses the macOS system CA file for TLS verification and
the managed Codex proxy for HTTPS name resolution when the sandbox restricts direct DNS.

| Live evidence | Observed in the installed workspace |
| --- | --- |
| Run | `R0009` on `V0006`, finished, with no collection or settings problems; the task check passed, and that check verifies the format of the answer only |
| Child session | Native `literature` role; its session file is in `sessions/` in that run's record folder |
| Paper | DOI `10.1093/bioinformatics/btp352`, PMCID `PMC2723002`, 22,402 bytes of Europe PMC XML |
| Original SHA-256 | `28d32f4470a5d2e31f2a17cc26715e997354f25e5ce44e34efd3002748b163cc` |
| Source passage | Section 2.1.1, `block:000013`, identifies the header and alignment sections and the header's leading `@` character |
| Reading and citations | Six logged invocations. The child followed the first read's cursor into its second read and retrieved “The human genome browser at UCSC.” in the references direction. |
| Log SHA-256 | `5991ab72b522fc990f0918d48831cc4fecb553271414f3411d6dc931dc196783` |

The audit (a script written in the same session, not an independent party) matched the child session's MCP responses to the invocation log, checked the controller's
attachment hashes, rehashed the original XML, and reproduced the cited passage from that document version. The audit's output was not stored in any record. Records and originals (`records/`,
`library/`) are kept only on the machine that ran them; they are not in this repository, so the run ids and hashes above
cannot be checked from a clone.
To replay it in the installation that holds the records, run `python3 tasks/lit-service/audit.py records/runs/R0009 library`.
Europe PMC supplied the article in the delegated run. In a separate model-free check through the same sandbox
wrapper, PubMed and OpenAlex each returned the SAMtools paper, and Asta listed four allowed tools. PDF
extraction and PNG delivery were tested with local fixtures and real Poppler.
