# Proposal workflow

A proposal is a controller-created, immutable, single-use authorization for one candidate attempt. It closes the gap between search recommendations and accepted results.

## Controller and worker boundary

The project adapter declares:

- a worker workspace root inside mutable roots;
- controller-only roots containing the adapter and state;
- an explicit `trusted_process`, `os_acl`, `container`, or `remote` trust/enforcement mode;
- an isolation-preflight command.

The root runs that command for a fresh workspace and passes its JSON proof to `plan-round`. The proof binds the workspace, trust/enforcement mode, preflight command digest, controller-root digest, protected-input digest, and retained preflight artifact. The CLI validates this evidence but cannot create OS users, ACLs, containers, or remote sandboxes. `trusted_process` proof must acknowledge that limitation; it is suitable only when candidate code and workers are trusted not to evade the evaluator.

Workers must not receive write access to `.evolution`, `events.sqlite3`, evaluator code, datasets, references, baselines, or another workspace. They do not run `plan-round`, `update`, Campaign commands, or promotion commands.

## Create a proposal

Run:

```text
python scripts/evolution_cli.py plan-round \
  --workspace-root /absolute/isolated/workspace \
  --worker-proposal-output /absolute/isolated/workspace/proposal.json \
  --isolation-proof /absolute/preflight.json
```

The controller atomically records:

- event-store revision used for planning;
- default and selected mode;
- structured override reason and evidence when modes differ;
- target cell, intended descriptors, and descriptor policy;
- one parent ID and immutable code reference;
- complete context pack and canonical digest;
- assigned workspace and isolation proof;
- active Campaign and current milestone, when applicable.

Proposal creation uses compare-and-swap against the event-store revision. If another controller action commits first, planning fails and must be recomputed. Project configuration limits the number of open proposals; one Campaign may have only one open proposal. The controller retains an authoritative proposal view and writes a digest-identical worker copy only inside the assigned workspace.

`select-mode`, `select-parent`, and `select-context` remain read-only previews. Their output cannot authorize a candidate.

## Target cells and descriptors

Explore automatically selects an uncovered or least-attempted cell when every descriptor key has a finite `descriptor_space`. Otherwise the controller must pass `--target-descriptors` with a complete JSON object. Exploit and Research default to the parent cell. Campaign uses the current milestone target.

`target_cell_attempts` counts consumed proposals, including invalid candidates. Actual feasible candidates enter archive cells using evaluator-emitted descriptors. With `allow_novel`, an actual/expected mismatch is retained and marked `descriptor_match: false`; with `exact`, `update` rejects it before committing an event.

## Mode overrides

When `--mode` differs from the deterministic default, both `--override-reason` and one or more `--override-evidence` references are required. Active Campaign mode and stop decisions cannot be overridden. Override evidence is stored in the immutable proposal.

## Consume or cancel

The schema-v3 candidate result echoes `proposal_id`, `proposal_digest`, `context_digest`, and workspace, plus scope evidence. The controller rejects conflicting self-declared parent, mode, Campaign, milestone, target, or descriptors. It also requires the evaluator to report `scope_passed` and matching protected-input evidence.

`update` commits `candidate_recorded` and `proposal_consumed` in one SQLite transaction. A uniqueness constraint prevents double consumption, including concurrent attempts. An abandoned proposal must be closed with `cancel-proposal`; cancelled and consumed workspaces are not reused within the state root.

A project may pass `--lesson-index FILE` when creating a new proposal to bind compact historical or per-round performance lessons. This is supplemental evidence, not a replacement for evaluator results or a mutation of historical events. The same option is available on `select-context` previews. See [context sampling](context-sampling.md) for card fields and selection limits.
