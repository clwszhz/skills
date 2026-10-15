# Multi-agent policy

Subagents are temporary bounded workers. The root agent retains authority over global state, evaluator execution, promotion, and stopping.

## Good delegation targets

- independent literature or codebase research;
- semantic curation of a controller-prefiltered context list;
- mechanism-distinct candidates in isolated directories/worktrees;
- independent failure or profiler hypotheses;
- independent tests for a component.

## Keep with the root or one owner

- mode and final parent approval;
- global archive and mode-state writes;
- deterministic correctness/performance decisions;
- global-best promotion;
- sequential Campaign implementation;
- one shared GPU or other bottleneck evaluator.

Candidate workers receive one materialized proposal plus a bounded hypothesis. The proposal fixes their workspace, parent, mode, target, intended descriptors, context digest, and optional Campaign milestone. They may write only that workspace and private run directory. Return a schema-v3 result artifact; do not invoke controller/state commands.

Instruction-level role separation is not a permission boundary. The project adapter must use ACLs, a distinct user, a container, or a remote worker so candidate workers cannot write controller-only roots or read protected results. The root verifies the project isolation-preflight artifact before creating each proposal.

Prefer no subagents for a short Exploit round. Explore and Research can use two or three independent workers when parallelism materially improves coverage. Do not add agents merely to mirror pipeline stages.
