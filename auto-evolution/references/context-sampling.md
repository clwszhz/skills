# Context sampling

Each mutation round has exactly one parent. Inspirations are evidence and mechanisms, not additional merge bases.

## Context pack

`plan-round` records a context pack in the proposal containing:

- mode and target cell;
- parent ID and record;
- optional global-best quality anchor;
- two to four complementary valid inspirations;
- up to three relevant invalid-candidate lessons;
- selection reasons and source artifact references.

Load full relevant source for the parent. For inspirations, prefer hypothesis, descriptors, measured effects, targeted diff/snippets, and compatibility notes. Do not dump entire candidate trees into the prompt.

## Mode-conditioned sampling

- Exploit: prioritize quality, bottleneck/tag relevance, and descriptor proximity.
- Repair: use the selected invalid parent, related failure lessons, and evidence tied to the configured repair metric; do not treat repair progress as feasible quality.
- Explore: prioritize descriptor distance, novelty, underused cells, and a quality anchor.
- Campaign: use the current Campaign head, milestone ancestor, and compatible component donors.
- Research: use the development best, representative cell elites, failure clusters, profiler evidence, and targeted literature cards.

The CLI uses deterministic metadata similarity and diversity. A context-curator agent may refine a prefiltered list before proposal creation, but the root approves one final pack. Its canonical digest is bound to the candidate result. `select-context` is a preview only and does not increment usage counts or authorize `update`.

## Optional project experience index

`select-context` and `plan-round` accept `--lesson-index FILE` for controller-curated compact lessons, including valid candidates that regressed or gave inconclusive performance evidence. The JSON has `schema_version: 1` and a `lessons` array. Each card supplies candidate ID, stable mechanism family, mechanism, outcome, applicability, conclusion, retry condition, and SHA-256/path evidence references; optional bottlenecks, parent ID, sequence and observed score delta support relevance ranking. Correctness remains exclusively evaluator-owned.

The CLI verifies referenced evidence and selects at most `max_negative_lessons` cards (capped at five), with distinct mechanism families. Selected cards and the index digest are bound into the new proposal context. Later index changes never alter an existing proposal. Prefer the project's digest-bound compact rendering over reading all raw case samples; follow only source references already bound in the proposal. Unknown or cross-environment differences must remain qualified; do not label them causal failures.
