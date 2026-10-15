# Project contract

The generic Skill discovers project-specific behavior through schema-v3 `.evolution/project.json`. Use `assets/project.template.json` as the field reference and `scripts/evolution_cli.py init` to create a starting configuration.

For an existing schema-v1 or schema-v2 configuration, use `migrate-project` with a pinned adapter path, explicit trust/enforcement choice, worker workspace root, controller-only roots, and isolation-preflight command. The command retains a versioned backup; it does not invent project behavior or permission boundaries.

## Required project facts

- `project.name`: stable project identifier.
- `project.execution_environment`: `native`, `wsl`, `container`, or `remote`.
- `project.candidate_root`: absolute mutable source root.
- `project.state_root`: absolute or project-relative directory for archive and generated search state.
- `project.mutable_roots`: paths candidate workers may modify.
- `project.protected_roots`: baseline, evaluator, datasets, references, or other protected paths.
- `adapter.interface_version`: currently `1`.
- `adapter.path` and `adapter.sha256`: absolute controller-only project adapter and its pinned digest. Read [adapter-interface.md](adapter-interface.md).
- `isolation.enforcement`: `trusted_process`, `os_acl`, `container`, or `remote`.
- `isolation.trusted_process_acknowledged`: explicit acknowledgment required only for trusted-process development.
- `isolation.worker_workspace_root`: absolute root for non-overlapping proposal workspaces inside mutable roots.
- `isolation.controller_only_roots`: absolute roots containing the adapter, event store, evaluator control plane, and other worker-inaccessible state.
- `commands.isolation_preflight`: project command that proves the assigned workspace and protected inputs are isolated before `plan-round`.
- `commands.build`: candidate build command.
- `commands.smoke`: fast correctness/evaluation command.
- `commands.development`: development-suite evaluator command.
- `evaluation.primary_metric`: field represented by `evaluation.primary_score` in candidate results.
- `evaluation.direction`: `maximize` or `minimize`.
- `evaluation.hard_constraints`: required boolean result keys.
- `evaluation.descriptor_keys`: keys used to form archive cells.
- `evaluation.repair_metric` and `repair_direction`: optional numeric auxiliary metric used only to choose among build- and scope-passing invalid parents when no valid candidate exists.
- `datasets.smoke` and `datasets.development`: required dataset manifests or stable identifiers. Validation and holdout entries are required when their completion flags are enabled. Keep these partitions disjoint.
- `search` and `completion`: budgets, plateau/noise policy, and target.

## Command boundary

Commands are declarative strings. The generic CLI validates and reports them but does not execute them. The root agent executes them using the project's approved environment and permissions.

Each evaluator invocation must write evidence that conforms to [metric-model.md](metric-model.md) and [evaluation-protocols.md](evaluation-protocols.md). Development emits a schema-v3 proposal-bound candidate result for `update`; validation and holdout emit protocol-evaluation results for `record-evaluation`. Every result identifies the code revision or candidate, exact protocol, dataset/evaluator/command digests, environment, measurement samples, aggregation, scope proof, and artifacts.

## Path boundary

All paths must be explicit. The project root containing `.evolution/project.json` is an orchestration root, not automatically the candidate source root. `init` therefore requires `--candidate-root`.

Candidate workers may write only within their proposal workspace and private run directory. The adapter, `state_root`, controller-only roots, and protected roots must be outside every mutable root. In `trusted_process` mode this is an audited convention, not a security boundary; never describe its results as adversarially isolated. Use OS permissions, containers, or remote execution when candidate code is untrusted.

The isolation preflight result must bind the assigned workspace, configured enforcement, normalized command digest, controller-root digest, protected-input digest, and retained artifact digest. `plan-round` rejects missing or mismatched proof. Development evaluation must report boolean `scope_passed`; include it in `evaluation.hard_constraints`.

The validator recognizes native Windows and POSIX/WSL absolute paths. It cannot prove permissions inside a remote environment; project-specific preflight checks remain required.

## State files

The configured state root must be on a controller-local filesystem that supports SQLite locking and durable replacement. It contains:

```text
events.sqlite3
archive.jsonl
protocol_evaluations.jsonl
proposals.jsonl
mode_state.json
context_pack.json
failure_summary.json
campaigns/
proposals/
```

`events.sqlite3` is authoritative; the JSON/JSONL files and Campaign files are replayable views. Only the root agent or deterministic controller writes global state. Candidate workers return result artifacts for the root to ingest and never receive write access to the event store or views.
