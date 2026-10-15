# Campaign workflow

A Campaign is a bounded sequence of coupled changes whose intermediate candidates may regress but must remain within declared gates.

## Campaign v2 specification

Start from `assets/campaign.template.json`. The specification fixes:

- hypothesis, target descriptors, descriptor policy, and external-contract digest;
- affected modules and rollback candidate;
- ordered milestone objects with IDs, objectives, target descriptors, required hard constraints, and allowed primary-score regression;
- total candidate, evaluator-run, and controller elapsed-time budgets;
- final hard constraints, score gate, and optional validation requirement.

`campaign-start` requires a valid archived head and stores its starting score. Only one Campaign may be active.

## Milestone loop

An active Campaign forces `plan-round` into Campaign mode. The proposal is bound to the current head, current milestone ID, target descriptors, and policy. A Campaign may have only one open proposal.

Every consumed proposal charges candidate and evaluator-run budgets, including invalid candidates. Invalid candidates remain failure evidence but cannot advance the head. Once any total budget is exhausted, new Campaign proposals are rejected.

Run:

```text
python scripts/evolution_cli.py campaign-advance --candidate-id <id>
```

The controller derives the expected milestone; callers cannot provide arbitrary milestone text. Advancement requires:

- a consumed proposal for the active Campaign and current milestone;
- parent equal to the current Campaign head;
- a valid candidate with every milestone hard constraint passing;
- primary score within the milestone regression allowance.

Successful advancement updates the head and moves exactly one milestone forward.

## Close and final gate

`campaign-close --status completed` succeeds only after every milestone and when the current head passes all final hard constraints, the start-relative score gate, optional absolute score, and optional validation requirement. Open Campaign proposals block closure.

`budget_exhausted` is accepted only when a declared budget is actually exhausted. `aborted` records an explicit reason without claiming success. Active Campaign v1 state from an older controller may only be closed as aborted.

Campaign events are authoritative. `campaigns/*.json` files are replayable views; use `rebuild-state` instead of editing them.

## Authorized accounting-unit corrections

`campaign-correct-budget --record FILE` appends a `campaign_budget_corrected`
event for an explicitly authorized accounting correction. The record includes
`campaign_id`, exact `old_budget` and `new_budget`, authorization, reason, and
hashed evidence references. Only `max_evaluator_runs` may change; candidate
limits, elapsed-time limits, and the original start time remain fixed. Historical
events and measured results are preserved. The command does not reopen an
expired Campaign or run an evaluator. Current accounting charges
`max(len(evaluation.samples), 1)` per consumed result; adapters emitting one
sample per matrix must budget in those units.
