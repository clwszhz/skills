# Evaluation protocols

Keep search evidence, promotion evidence, and final audit evidence separate. A score is comparable only within one protocol fingerprint.

## Development

Development evaluation is repeatable and visible to the search loop. Its schema-v3 result uses `assets/candidate-result.template.json`, echoes an open proposal and scope proof, declares `evaluation.protocol` as `development`, and is ingested with `update`.

Only development candidates populate archive cells, choose parents and inspirations, update plateau history, or become `development_best_id`. Validation and holdout results never enter those mechanisms.

## Validation

Validation is a promotion gate for an archived development candidate. Use `assets/protocol-evaluation.template.json` with protocol `validation`, then run:

```text
python scripts/evolution_cli.py record-evaluation --result /absolute/path/validation.json
```

A passing validation result may update `verified_best_id`, `verified_best_score`, and `verified_evaluation_id`. It does not replace the development score or add a candidate to the archive.

## Sealed holdout

Holdout is a one-time stage-end audit. It must target the validation-verified leader when validation is required, otherwise the development leader. Record it only after the stage has stopped changing:

```text
python scripts/evolution_cli.py record-evaluation \
  --result /absolute/path/holdout.json \
  --stage-end-confirmed
```

After a holdout event is committed, the stage is closed: mode selection stops and `update` rejects new candidates. Start a new state root for subsequent search. Holdout scores are stored for handoff but never used for parent, context, cell, or global development selection.

## Required evidence

Every development, validation, and holdout result includes:

- a stable `protocol_id`;
- configured dataset ID and content digest;
- evaluator ID/version and content digest;
- normalized command digest;
- hardware/runtime environment evidence;
- raw numeric measurement samples;
- aggregation method and aggregate value;
- hard constraints, failure details, and artifact references.

The aggregate value must equal `primary_score` for a valid result. Invalid results may have no samples and must set `primary_score` to null.

The controller fingerprints protocol ID, dataset, evaluator, and command identity. It refuses comparable results whose fingerprint changes within one state root. Environment evidence is retained for audit; the project evaluator is responsible for declaring which environment differences invalidate a run.
