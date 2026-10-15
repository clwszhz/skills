# Project adapter interface

The project adapter is a controller-only executable that translates project-specific source, build, evaluator, and result formats into Auto Evolution contracts. The project configuration pins its absolute path and SHA-256; changing the adapter starts a new comparable protocol or requires an explicit project migration.

The adapter, not the generic Skill, knows the main program and evaluator. A typical adapter provides:

```text
ADAPTER describe
ADAPTER prepare --source CODE_REF --workspace DIR --proof-output FILE
ADAPTER build --workspace DIR --output FILE
ADAPTER evaluate --workspace DIR --proposal FILE --candidate-id ID --result FILE
```

## Describe

`describe` returns JSON containing the adapter interface version, candidate executable template, evaluator path and digest, evaluator argument/result contract, exit-code meanings, supported protocols, and project capabilities. Paths containing `{workspace}`, `{candidate_id}`, `{evaluation_id}`, or `{run_dir}` are resolved by the adapter, never by candidate workers.

## Prepare

`prepare` creates one unused proposal workspace from the selected parent and writes the isolation/trust proof consumed by `plan-round`. It must not alter the parent, evaluator, datasets, state, or another workspace. In `trusted_process` mode it records `risk_acknowledged: true`; this is evidence of the chosen trust model, not OS isolation.

## Build

`build` operates only in the assigned workspace and emits a machine-readable build record. Generated artifacts remain in the workspace or its private run directory.

## Evaluate

`evaluate` invokes the project evaluator using its native black-box contract, classifies valid/invalid/infrastructure exit codes, and writes either a schema-v3 development candidate result or a validation/holdout result. It maps project scores into `primary_score`, samples, constraints, descriptors, provenance, scope evidence, and artifact paths without changing evaluator policy.

The evaluator may remain a sealed standalone program. Re-sign or change it only when its own implementation or fixed policy changes; adding or updating an external adapter does not require modifying the evaluator.
