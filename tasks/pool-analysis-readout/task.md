## Goal

Analyze the results of the expanded-versus-compact software input comparison. The owner wants to know
what the observed data say about quality, token use and run failures.

## Input

`in/pool-analysis-readout/results.csv` is a synthetic validation dataset. The six candidate pools are
disjoint. Each arm ran once per pool in randomized order. Each available output was scored on the same
0–100 scale with two fixed scorer seeds; higher is better. Tokens count the whole trajectory's input.
One compact trajectory timed out before emitting an output or token telemetry.

## Requirements

- Give a reproducible pool-level analysis of both outcomes, including signs, units, denominators and
  numerical summaries. Quantify uncertainty or explain its limits.
- Account for the timeout in the recommendation to the owner. Explain which population the summary
  describes, and how informative missingness could affect interpretation.
- Give traceable sources for any existing analysis rule actually used. If a source has both an object
  identifier and a revision, cite it in the form `<id>@<revision>`.

## Completion criteria

Write a concise Chinese memo to `out/pool-analysis-readout/analysis.md`, including per-pool results
and the recommendation. Keep task-specific work products in the task output directory.
