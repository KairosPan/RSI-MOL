## Goal

Write a prospective analysis protocol for comparing expanded and compact software inputs.
The owner needs a rule for interpreting paired quality and token measurements on six fresh candidate pools.

## Input

The six pools are disjoint. Within each pool, the team runs one independent expanded-input trajectory
and one compact-input trajectory, with randomized order. Two fixed scorer seeds score the selected output
from each trajectory. Each trajectory has one total input-token count. A run can time out before producing
its output. No measurements are available yet.

## Requirements

- Define the unit of replication, a reproducible aggregation for the two scores, the direction of the
  two contrasts, and the summary across pools. State the conditions under which the rule applies.
- Specify how to report a failed arm and what the remaining measurements can establish.
- Support the methodological reasoning with a short, exact passage from a relevant methods paper.
  Give a traceable citation and distinguish the paper's observation from your application of it.

## Completion criteria

Write the Chinese protocol to `out/pool-analysis-protocol/plan.md`. Keep task-specific work products
in the task output directory. This task calls for planning only.
