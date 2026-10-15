---
name: auto-evolution
description: Run a bounded, evaluator-driven quality-diversity search over code candidates in any project that supplies an isolated candidate area, build/run commands, and machine-readable evaluation results. Use for automated multi-round code or algorithm evolution; do not use for ordinary one-off optimization or when no repeatable evaluator can establish correctness and quality.
---

# Auto Evolution

Use this skill to organize a long-running code optimization stage. The project supplies domain facts and execution; this skill supplies search policy, persistent state, context sampling, and promotion discipline.

## Preconditions

Before evolving code:

1. Read every applicable `AGENTS.md`.
2. Locate `.evolution/project.json`. If it is absent and the user authorized setup, initialize it with `scripts/evolution_cli.py init`; otherwise report the missing adapter.
3. Read [references/project-contract.md](references/project-contract.md) and [references/adapter-interface.md](references/adapter-interface.md). Verify the pinned project adapter with its `describe` command, then validate project metadata with `scripts/evolution_cli.py validate`.
4. Do not invent correctness thresholds, baseline commands, protected paths, dataset splits, or a completion target. Missing facts that affect validity are blockers.
5. Require an isolated mutable candidate root and an evaluator that emits the result contract in [references/metric-model.md](references/metric-model.md).
6. Run the project adapter's isolation preflight before assigning a worker. `trusted_process` may be used only with explicit acknowledgment and is not an adversarial boundary; `os_acl`, container, or remote enforcement is required when candidate code is untrusted.
7. If the archive is empty, evaluate the untouched candidate/baseline under the configured development protocol and ingest it with `seed`. Do not fabricate its score or descriptors. When the measured seed is invalid, continue only if the project declares a numeric repair metric; Repair mode may derive candidates from the best measured invalid parent but never promote it.

The Skill does not create a Goal. When used inside Goal mode, the Goal defines the current stage outcome, budget, and completion criteria; the Skill defines how to pursue it.

## State ownership

The root agent owns the append-only `events.sqlite3` event store, proposal creation and consumption, evaluator execution, promotion decisions, and stopping decisions. `archive.jsonl`, `proposals.jsonl`, `protocol_evaluations.jsonl`, `mode_state.json`, and Campaign files are materialized views; never edit them as facts. Project evaluators—not model opinion—decide deterministic correctness and measured quality.

Run `doctor` when state integrity is uncertain. If only a materialized view is missing or stale, run `rebuild-state`; it replays committed events without re-running a candidate. Use `migrate-state` once for a legacy state root that predates the event store.

Never let candidate workers read or modify controller-only state, modify the evaluator, baseline, datasets, references, global archive, or another candidate. Workers receive one immutable proposal plus one isolated workspace and return artifacts; they do not invoke global-state commands. Do not commit, publish, delete retained artifacts, weaken isolation, or expand scope unless the user or active Goal authorizes it.

## Operating loop

For each round:

1. Validate project and state.
2. Read [references/proposal-workflow.md](references/proposal-workflow.md), run the project isolation preflight for one unused workspace, then call `plan-round`. This atomically binds the default/selected mode, any override evidence, target cell, intended descriptors, one parent, exact context digest, workspace, Campaign milestone, and event-store revision.
3. Give the worker only the materialized proposal file and its isolated workspace. `select-mode`, `select-parent`, and `select-context` are previews and cannot authorize `update`.
4. Read [references/context-sampling.md](references/context-sampling.md). Load full relevant code only for the bound parent; load only the context recorded in the proposal.
5. Ask for a falsifiable mechanism implementation within the proposal boundary. A proposal is single-use; cancel it explicitly if abandoned.
6. Run the project-defined evaluation cascade: scope/static checks, build, smoke correctness, quick performance, development evaluation, and profiler only when useful. Read [references/evaluation-protocols.md](references/evaluation-protocols.md); development drives search, validation establishes the verified leader, and sealed holdout is recorded once at stage end.
7. Convert the development evaluator output into a schema-v3 candidate result containing the proposal/context digests and scope proof, then ingest it with `update`. Candidate recording and proposal consumption are one transaction. Record validation and holdout evidence separately with `record-evaluation`; never pass them to `update`.
8. If invalid, preserve a structured failure record. If valid, let the deterministic archive update decide cell elite and global-best status.
9. Recompute state and continue until the Goal completion criteria are actually met or the bounded exploration deliverable is complete. When the project supplies pre-edit plans and post-evaluation reviews, complete both in every mode (including Exploit) and use the review to justify the next design. Respect its benefit/retry gates; do not execute a prewritten mutation schedule in place of feedback.

Read [references/search-modes.md](references/search-modes.md) when selecting or overriding a mode. Read [references/campaign-workflow.md](references/campaign-workflow.md) before starting or continuing a multi-step coupled change. Read [references/archive-schema.md](references/archive-schema.md) when inspecting or repairing state.

## Long-running execution

The root agent remains responsible for authorized initialization, builds, and evaluations until their results have been checked. A tool returning a session ID or "still running" means execution is pending, not that the task is complete.

- Keep the current turn active while required work runs. Retain the execution handle and run ID, and use the available wait/poll tool to follow that same process. Use bounded waits of at most 60 seconds and report meaningful progress; do not launch duplicate runs merely because a call yielded.
- Do not end a turn solely with "running in the background" or require the user to repeatedly say "continue". Background execution is acceptable only while the agent continues supervising it, or when the user explicitly requests a detached handoff.
- After process exit, inspect the exit code, logs, machine-readable status, expected case/sample coverage, correctness checks, and required artifacts. Process exit or a success message alone is insufficient. Record the verified outcome through the existing project workflow before continuing authorized work.
- On failure, preserve evidence and diagnose within the authorized scope and budget. Do not retry indefinitely, silently skip cases, change scoring rules, or count an incomplete run as successful. Finish only when the requested stage and report are complete, the user stops/changes the task, or a concrete blocker or exhausted budget prevents further authorized progress; clearly identify any unfinished work.
- Persist run IDs, execution handles, artifact paths, and pending checks so an interrupted session can be resumed. On resumption, inspect existing processes and results before restarting. These instructions do not provide automatic recovery after client exit or disconnection; do not promise automatic wake-up or notifications unless such a mechanism is actually configured.

## CLI

The standard-library-only helper has these subcommands:

```text
python scripts/evolution_cli.py init
python scripts/evolution_cli.py migrate-project
python scripts/evolution_cli.py validate
python scripts/evolution_cli.py select-mode
python scripts/evolution_cli.py select-parent
python scripts/evolution_cli.py select-context
python scripts/evolution_cli.py seed
python scripts/evolution_cli.py plan-round
python scripts/evolution_cli.py cancel-proposal
python scripts/evolution_cli.py update
python scripts/evolution_cli.py record-evaluation
python scripts/evolution_cli.py doctor
python scripts/evolution_cli.py rebuild-state
python scripts/evolution_cli.py migrate-state
python scripts/evolution_cli.py summarize-failures
python scripts/evolution_cli.py campaign-start
python scripts/evolution_cli.py campaign-advance
python scripts/evolution_cli.py campaign-correct-budget
python scripts/evolution_cli.py campaign-close
```

Use `--help` on the relevant subcommand. Pass `--config /absolute/path/.evolution/project.json` when the current directory is not the project root.

For an explicitly user-approved evaluator-only compatibility change, `register-compatibility --record FILE` appends exact old/new protocol identities and hashed review evidence. It accepts only an evaluator digest change, requires no active proposal/Campaign, preserves actual provenance, and does not authorize semantic compatibility by itself. Never use it to admit dataset, command, metric, threshold, timing, or environment changes. Existing caches require their own project-side exact compatibility checks.

The CLI manages metadata only. It does not execute arbitrary build or evaluator commands, mutate source, merge candidates, or decide semantic compatibility.

## Multi-agent policy

Subagents are optional. Read [references/multi-agent-policy.md](references/multi-agent-policy.md) before delegation. Use them for independent research, context curation, failure analysis, or isolated candidate implementations. Prefer one agent for sequential Campaign work and any task that writes shared mutable state.

## Required handoff

At the end of a bounded stage, report:

- verified global best and its evidence;
- development leader and validation-verified leader as distinct identities;
- retained cell elites and archive coverage;
- proposal counts, target-cell attempts, cancellations, and open reservations;
- candidate/evaluation budget used;
- validated improvements versus noise;
- failed mechanisms and reusable lessons;
- holdout status;
- active Campaign state and remaining risks.

Do not call a local benchmark improvement a general improvement without the project-defined validation evidence.
