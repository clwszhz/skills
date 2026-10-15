# Metric model

Keep four roles separate.

## Hard constraints

Hard constraints are boolean gates such as build success, tests, numerical correctness, API compatibility, timeout, and resource limits. A failed required gate makes a candidate invalid regardless of speed.

## Primary quality

For development results, `primary_score` determines the best feasible candidate within an archive cell and the development best. Validation uses its own comparable scores to establish the verified leader. The project declares whether higher or lower is better. Prefer an end-to-end or contest-aligned measure over a proxy.

## Auxiliary metrics

Auxiliary metrics diagnose mechanisms and guide hypotheses. They do not override the primary quality unless the project explicitly includes them in its primary score. Examples include phase timings, fill ratio, memory, utilization, code size, and profiler counters.

## Behavior descriptors

Descriptors partition feasible candidates into qualitatively different cells. They are categories, not quantities to maximize. Examples include algorithm family, representation, execution model, parallelization, and reuse strategy.

When `descriptor_space` declares allowed values, the evaluator must emit one of those values. Expand the declared space deliberately when a genuinely new mechanism appears; do not create arbitrary labels to inflate archive coverage.

## Development candidate result contract

The schema-v3 JSON consumed by `update` is development evidence bound to one open proposal:

```json
{
  "schema_version": 3,
  "candidate_id": "c0007",
  "proposal_id": "p0007",
  "proposal_digest": "sha256:...",
  "context_digest": "sha256:...",
  "workspace_root": "/isolated/workspaces/c0007",
  "code_ref": "branch-or-path-or-commit",
  "hypothesis": "Mechanism being tested",
  "changed_files": ["src/example.cpp"],
  "tags": ["memory-layout"],
  "bottlenecks": ["global-memory-latency"],
  "scope": {
    "workspace_root": "/isolated/workspaces/c0007",
    "manifest_digest": "sha256:...",
    "protected_inputs_digest": "sha256:..."
  },
  "evaluation": {
    "protocol": "development",
    "protocol_id": "development-v1",
    "valid": true,
    "primary_score": 1.18,
    "samples": [1.17, 1.18, 1.19],
    "aggregation": {
      "method": "median",
      "value": 1.18
    },
    "provenance": {
      "dataset_id": "development-manifest",
      "dataset_digest": "sha256:...",
      "evaluator_id": "project-evaluator-v3",
      "evaluator_digest": "sha256:...",
      "command_digest": "sha256:...",
      "environment": {
        "hardware": "stable hardware/runtime identity"
      }
    },
    "hard_constraints": {
      "build_passed": true,
      "correctness_passed": true,
      "scope_passed": true
    },
    "auxiliary_metrics": {},
    "descriptors": {},
    "failure": null,
    "artifacts": {}
  }
}
```

For invalid candidates, set `valid` to false, set `primary_score` to null, and provide `failure.category` plus `failure.message`. Samples may be empty when a hard constraint fails before measurement. The evaluator must not reward cached answers, weakened tests, changed timing boundaries, or skipped work.

The controller copies parent, mode, Campaign, milestone, target, intended descriptors, and context identity from the proposal into the archived record. Conflicting self-declared values are rejected. Candidate recording and proposal consumption are atomic.

The first archive entry is a measured seed candidate, normally the untouched writable baseline. It uses `proposal_id: null` and is ingested with `seed`; `update` never accepts an unbound candidate.

## Validation and holdout result contract

Validation and holdout use `assets/protocol-evaluation.template.json` and `record-evaluation`. They identify an existing archived `candidate_id` plus a unique `evaluation_id`; their nested evaluation evidence uses the same provenance, samples, aggregation, constraints, failure, and artifacts fields as development. They do not contain behavior descriptors and never create or replace archive candidates.

Read [evaluation-protocols.md](evaluation-protocols.md) for promotion and sealed-holdout rules. Do not translate a validation or holdout result into a development candidate result.
