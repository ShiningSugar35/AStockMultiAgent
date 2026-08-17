# Workflow — Research Technology Scout

## When to use

Use when the project needs an external capability scan, an architecture comparison, a new quant/research idea, or evidence that an existing subsystem should be simplified rather than expanded.

Primary skill: `$research-tech-scout`.

## Flow

1. **Baseline the local system first**
   - Read current capability, Agent observability, Skill efficiency, and relevant architecture boundaries.
   - Identify the exact local gap or measurable bottleneck before searching outside.

2. **Search in descending evidence strength**
   - Official repository/release/paper/platform documentation.
   - Maintainer issues, pull requests and discussions for active failure modes and roadmap direction.
   - Quant practitioner communities/social media only for discovery, operational pain points, and counterexamples.

3. **Deduplicate before adoption**
   - Map every external feature to an existing local provider, Skill, Workflow, deterministic gate, or prospective study.
   - Prefer `REJECT` when an outside multi-agent stack merely renames roles already present in this repository.
   - Prefer `ADAPT_PATTERN` when a small mechanism such as tracing, walk-forward scheduling, signed audit receipts, or incremental storage can be implemented without importing the whole framework.

4. **Apply the correct evidence gate**
   - Infrastructure/correctness/performance patterns may proceed through normal code review and tests.
   - Factors, models, portfolio rules, execution heuristics and RL ideas are `SHADOW_EXPERIMENT` until the existing PIT/prospective gates admit them.
   - Social/community claims never become production evidence by themselves.

5. **Minimize the integration surface**
   - Check license and dependency cost.
   - Reuse current config/registry/provider/plugin contracts.
   - Do not create a parallel state store, research ledger, Skill registry, or portfolio engine.

6. **Verify and record**
   - Put the dated scan in `docs/scouting/`.
   - State source, novelty, duplication, decision, smallest implementation, and required test/evidence.
   - If code changes, run targeted tests plus final Ruff/Pyright/full pytest before release.

## Stop conditions

- Stop adoption when the idea weakens PIT, source authority, deterministic accounting/math, paper confirmation, or broker prohibition.
- Stop when the same capability already exists locally with equal or stronger guarantees.
- Stop when licensing/data rights or reproducibility cannot be established.
