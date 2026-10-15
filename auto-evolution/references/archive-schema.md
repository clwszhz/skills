# Event store, archive, and state schema

`events.sqlite3` is the authoritative append-only event store. The controller inserts events in SQLite transactions with `synchronous=FULL`; update and delete triggers prevent mutation of committed rows. Proposal creation/terminal state, candidate, protocol-evaluation, mode preview history, and Campaign transitions are events.

These files are materialized views and may be replaced during recovery:

```text
archive.jsonl                 development candidate records
protocol_evaluations.jsonl   validation and holdout records
proposals.jsonl              proposal lifecycle records
mode_state.json              state derived by replay
campaigns/*.json             Campaign views derived by replay
proposals/*.json             worker-facing immutable proposal views
```

Do not hand-edit a view to repair history. `doctor` compares every view with event replay. `rebuild-state` atomically regenerates the views without executing project commands or candidates. SQLite integrity failure is not repaired automatically; retain the database and restore it from a trusted backup.

## State schema

`mode_state.json` schema version 3 contains proposal/search state plus separate promotion and holdout state:

```json
{
  "schema_version": 3,
  "attempt_count": 0,
  "valid_candidate_count": 0,
  "global_best_id": null,
  "global_best_score": null,
  "development_best_id": null,
  "development_best_score": null,
  "development_protocol_fingerprint": null,
  "validation_evaluation_count": 0,
  "validation_protocol_fingerprint": null,
  "verified_best_id": null,
  "verified_best_score": null,
  "verified_evaluation_id": null,
  "last_validation": null,
  "holdout_status": "not_run",
  "holdout_evaluation_id": null,
  "holdout_candidate_id": null,
  "holdout_valid": null,
  "holdout_score": null,
  "holdout_protocol_fingerprint": null,
  "cell_elites": {},
  "cell_elite_scores": {},
  "cell_attempts": {},
  "mode_stats": {},
  "context_use_counts": {},
  "parent_use_counts": {},
  "target_cell_attempts": {},
  "target_cell_reservations": {},
  "proposal_counts": {
    "created": 0,
    "consumed": 0,
    "cancelled": 0
  },
  "valid_score_history": [],
  "active_campaign": null,
  "repeated_bottleneck_rounds": 0,
  "last_bottleneck": null,
  "last_mode": null,
  "last_mode_reason": null,
  "updated_at": null
}
```

`global_best_*` remains a compatibility alias for `development_best_*`; it is not a verified claim. Only `verified_best_*` represents a candidate that passed the validation protocol.

Cells are canonical JSON encodings of the configured descriptor keys. `target_cell_attempts` is charged when a proposal is consumed, including invalid attempts; archive `cell_attempts` and elites use actual evaluator descriptors. Only valid development candidates whose required hard constraints and descriptors pass may update cells or the development leader. Validation and holdout events never affect cell elites, plateau history, parents, or context.

`proposal_created` uses compare-and-swap against the immediately preceding event sequence. `candidate_recorded` and `proposal_consumed` are committed in one transaction; a unique proposal-terminal entity prevents duplicate consumption or cancellation races.

## Recovery and migration

Use:

```text
python scripts/evolution_cli.py doctor
python scripts/evolution_cli.py rebuild-state
```

If a process commits an event and exits before materializing all views, event replay restores the exact committed state. A failed SQLite transaction contributes no partial event.

Project schema v3 adds a pinned project-adapter executable and an explicit trust model; migrate schema v1/v2 with `migrate-project`, then run `rebuild-state` and `doctor`. Candidate schema v3 and Campaign schema v2 add proposal/scope binding and gates. Existing events continue to replay, but legacy candidates remain historical and cannot receive new validation/holdout status. Re-evaluate through a proposal. Active Campaign v1 state may only be closed as aborted before starting Campaign v2.
