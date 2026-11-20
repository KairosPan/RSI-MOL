## Goal

Advise the RSI-Lab experiment owner whether the October H0/H1 pilot justifies adopting the compact input format.
Write a short prospective protocol the owner can use to decide whether it saves Lab tokens while preserving
selection quality on fresh tasks. This is a study-design decision based on a real pilot.

## Input

`in/paired-validation/pilot.json` contains aggregate measurements from the archived comparison. H0 used expanded
JSON and H1 used a compact presentation of the same input. Each ran one adaptive Lab trajectory on the same
20-item candidate pool. The execution order was fixed (baseline, H0, H1). Each selected item had two scorer seeds.
The proposed validation harness supports freezing nonoverlapping fresh pools and running both arms independently
on each pool with the same model, limits and inputs. For this design exercise, assume a planning allowance of
12 complete arm runs. No new evaluation data are available.

## Requirements

- Delegate the methods research to the native `literature` sub-agent. Use the local `literature` MCP tools to
  find and read the bodies of at least two distinct methodological papers. Ground the experimental-unit argument
  and the paired-comparison or uncertainty argument in actual passages. Mark the application to this benchmark
  as your inference. Keep public queries about statistical methods.
- Calculate the observed H1 minus H0 token difference and percent change (H0 denominator), the quality
  difference and coverage, and the cost comparison after including the one-off Meta development call. Separate
  descriptive observations, what this pilot can establish, and the decision you recommend.
- Specify a feasible allocation of the 12 runs, what is independently assigned or replicated, what is paired,
  how to handle run order/time drift, and how scorer seeds and overlapping selections enter the analysis.
  Define the across-pool estimand and uncertainty calculation, the handling of failed runs or missing scores,
  and a prospective quality-and-cost decision rule that can return an inconclusive result. Label proposed
  thresholds as choices requiring scientific justification. This task produces a protocol and uses no new
  scoring runs.

## Completion criteria

Write a decision memo and prospective protocol in Simplified Chinese to `out/paired-validation/report.md`.
Identify the supporting paper for each methodological judgment. Write `out/paired-validation/evidence.json`
as a JSON array, one row per cited passage, with string fields `claim`, `doi`, `document_id`, `document_version`,
`source_sha256`, `locator` and `quote`. For each paper include a short, contiguous verbatim quotation from
a single body block actually returned by `read_document`, with its exact block locator. Report any
unresolved evidence or design assumption explicitly.
