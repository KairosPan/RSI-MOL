---
name: scientific-memory
description: Retrieve evidence and past interpretations, inspect their freshness, and propose scientific memory objects.
---

Call `memory_status` when a task starts. Check the condition and omitted counts and run
`recall_passages` with the task question. Use keywords or a paper identifier to narrow a search.
A `none` snapshot contains no historical evidence.

Before delegating literature research or acquiring a paper, call `lookup_sources` with its
DOI, PMID, PMCID, provider identifier, document ID or title. Check which request and source record
link the paper to a document. Different papers may share an original file. Use the verification
and conflict fields when judging the match. When a search returns `next_offset`, repeat it with
the same query and filters and pass that value as `offset`. A result containing `get_evidence`
gives the arguments for retrieving that revision.

Use `get_evidence` for a specific ID and revision. It shows up to three deliveries at a time.
Pass its `next_delivery_offset` as `delivery_offset` for older deliveries, or select a delivery
by `source_run`, `document_id`, `document_version` and/or `invocation_id`.
When a large revision is split into `object_part` results, read each part in the same thread,
starting at zero. Repeat the exact request with the returned `next_object_offset` as
`object_offset` until there is no continuation. The parts contain successive characters of the
revision's JSON. Read all parts before citing the object or its passage.

Cite a passage using the evidence ID and revision, source run, document ID and version, original
SHA-256, locator and an exact quote. Identify whether it was returned by memory, read by the
literature service in this run, or both. A historical delivery establishes what the tool returned.
Check the passage verification status and assess support for the present claim. When the task
requires a fresh read, pass the document ID, version and locator to `read_document` in this run.

Use `recall_judgments` for judgments and `recall_objects` to search all kinds or filter by kind.
Read the scope, freshness, supporting and challenging references. Check the original or reassess
the interpretation when a result is marked `needs_review` or `unavailable`.
For a challenge, check `challenged_revisions` to see which revisions it addressed. When the
result gives `next_counterevidence_offset`, pass it as `counterevidence_offset` to `get_evidence`
with the target ID and revision. In this list each `challenge` gives the challenger's ID and
revision; pass `next_offset` as `counterevidence_offset` to continue. Read the challenger itself
with its ID and revision before citing it. To inspect a withdrawn revision, pass its revision
number to `get_evidence`.
If `omitted_details` is present, the object still contains its exact source and object references.
Use `get_evidence` with their IDs and revisions to inspect the linked material separately.

Before sending the final answer in a managed live task, the lead reviews the report for a judgment
another task could apply. If the report draws a new conclusion with clear conditions of use and
grounds it in a passage or object retrieved in this run, call `propose_memory` with `kind=judgment`.
Put the conclusion in `statement`, its conditions in `scope`, and cite the exact references below.
Check the response for a pending proposal ID. If a retrieved judgment already covers the same
conclusion and scope, cite its ID and revision. If nothing qualifies, finish the task as usual.

In managed live tasks, `propose_memory` accepts the following kinds and fields.

| Kind | Required content | ID prefix |
| --- | --- | --- |
| `question` | `text` for the subquestion | `subquestion_` |
| `judgment` | `statement`, `scope` | `judgment_` |
| `decision` | `decision`, `scope` | `decision_` |
| `investigation` | `description`, `scope` | `investigation_` |
| `method_experience` | `change`, `rationale`, `outcome`, `scope` | `method_experience_` |

For method experience, choose `improved`, `degraded`, `unchanged` or `failed`. An empty scope means
the applicable conditions are unknown. Give the current task ID as `question_id`, or retrieve a
subquestion and pin its ID and revision in `object_refs`. When updating an object your task created,
set `expected_revision` to its current unwithdrawn revision. Use zero for a new object or when
all its revisions have been withdrawn.

Each interpretation other than a question needs at least one reference. For a previously retrieved
question or interpretation, provide its exact `id`, `revision` and `relation` in `object_refs`.
Choose `supports`, `challenges`, `background` or `derived_from` to describe the link.

For each entry in `source_refs`, quote one contiguous excerpt and include its `document_id`,
`document_version`, `source_sha256`, `locator` and `relation`.

- For a historical passage returned by `recall_passages` or `get_evidence` in this run, include its
  `evidence_id`, `revision` and `source_run`.
- For a passage returned by the literature service in this run, set `delivery_source` to `literature`.
  Use a complete passage (`block_complete: true`) from `read_document` or `find_passages`. Copy
  `invocation_id` from the top level of that response and the address and quote from its passage.

Submit after reading the referenced material. The tool returns a pending proposal ID. After the run,
the controller registers accepted proposals and newly delivered evidence together. Scientific support
remains pending review.
