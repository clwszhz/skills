# Search modes

The mode changes parent and context sampling; it does not change correctness rules.

## Repair

Use only when the archive contains measured candidates but none is valid and the project declares `evaluation.repair_metric` plus direction. Select the build- and scope-passing invalid candidate with the best repair metric, preferring the most recent on ties. Repair candidates remain invalid evidence until every normal hard constraint passes; they never become cell elites or the development best merely because the repair metric improves.

## Exploit

Use when the best feasible development score is improving beyond noise. Select the development best or a cell elite with recent progress. Prefer nearby inspirations relevant to the current bottleneck. Keep edits focused unless a Campaign is active.

## Explore

Use during bootstrap, when archive coverage is low, or when new mechanisms are needed. Select a cell elite from an underused or structurally distinct cell. Use diverse inspirations plus the development best as a quality anchor. A new cell may be retained without beating the development best if it passes the project's quality floor.

## Campaign

Use whenever `mode_state.active_campaign` is set. Continue the Campaign head until a milestone, failure condition, or budget boundary. Do not abandon a coherent multi-step change because an explicitly allowed intermediate version is slower.

## Research

Use after a measured plateau with a repeated bottleneck or exhausted known mechanisms. Compare representative elites, failure clusters, profiler evidence, and targeted primary sources. Produce architecture hypotheses or a Campaign specification before large edits.

## Override discipline

`select-mode` is a deterministic preview, not an oracle. `plan-round` records the default mode and may choose a different mode only when supplied a nonempty structured reason plus evidence references. Active Campaign mode and stop decisions cannot be overridden. Repair mode ends automatically once a valid candidate exists. Workers cannot select or override modes in candidate results.
