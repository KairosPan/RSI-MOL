## Goal

Write a short analysis plan for the owner of the expanded-versus-compact input comparison. The owner needs a
concrete decision rule for a validation run with six fresh candidate pools. Address what to do when one arm fails.

## Input

Each pool receives one independent Lab trajectory using the expanded input and one using the compact input.
The owner can randomize the arm order within each pool. Each selected candidate receives two scorer seeds.
The run budget is exactly twelve trajectories. No measurements from the validation run are available yet.

## Requirements

- Define the experimental unit and the paired quality and token differences. Explain how to handle repeated
  scoring, run order, one missing arm and uncertainty across the six pools.
- Specify a prospective decision rule with an inconclusive outcome. Identify assumptions and thresholds
  the owner must choose before seeing results.
- Cite at least two distinct methodological papers. Support one decision about experimental units and one
  about paired analysis using short verbatim quotes from body passages. Previously returned passages from
  Scientific Memory and passages read from the literature service in this run are both eligible.

## Completion criteria

Write a concise Chinese memo to `out/paired-validation-followup/report.md` and a JSON array to
`out/paired-validation-followup/evidence.json`. Each citation row must contain `claim`, `doi`, `quote`,
`document_id`, `document_version`, `source_sha256`, `locator` and the delivery source for this run
(`delivery_source` equal to `memory`, `literature` or `both`). For a passage delivered by memory include its
`evidence_id`, integer `revision` and historical `source_run`. Use one contiguous quotation per row. Label
your application of each paper to this design as an inference.
