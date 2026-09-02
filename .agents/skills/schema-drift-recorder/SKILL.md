---
name: schema-drift-recorder
description: Record provider schema drift from immutable raw snapshots, validate repair proposals, and roll back candidates without mutating production parsers.
---

# Schema Drift Recorder

1. Start from the existing SourceSnapshot/ObjectStore material. Never reconstruct a drift sample from normalized fields when the original raw response is available.
2. Inspect `uv run astock adaptive-edge-schema` and the active provider dialect before proposing a repair. A drift proposal must name the provider, base dialect version, sample snapshot ids, official cross-check artifacts and repository contract tests.
3. Use `uv run astock adaptive-schema-repair-validate <proposal.json>` to validate the candidate against the existing `AdaptiveEdgeService`. Missing raw snapshots, insufficient diverse samples, missing official evidence, unknown canonical fields or absent contract tests must fail closed.
4. `uv run astock adaptive-schema-repair-admit <validation_id> --approve` may create only a reviewed candidate dialect release. It must not mutate the active production parser or write formal market/financial facts.
5. Audit frozen artifacts with `uv run astock adaptive-artifact-audit <artifact_id>` and use `uv run astock adaptive-dialect-rollback <release_id>` for the exit drill. The active dialect remains the last separately validated production configuration.
6. Preserve all raw responses, validation findings and candidate releases in the existing ObjectStore/State lineage. Do not create a parallel response archive or mutable shadow parser registry.
7. Record Skill selection/completion through `uv run astock agent-observation-register <request.json>` and inspect `uv run astock agent-observability-report --lookback-days 30` for routing telemetry.

## Output

Return the raw SourceSnapshot ids, immutable object hashes, structural drift summary, validation id/status, rejection codes, candidate release id when explicitly approved, contract-test evidence, and rollback result. State clearly that a candidate release is not an active production parser mutation.

## Prohibitions

- Do not overwrite or delete raw SourceSnapshot/ObjectStore evidence.
- Do not infer schema fields, units, scope, currency, PIT time or source authority when the upstream response omits them.
- Do not auto-admit a schema repair or modify the active provider dialect from a single sample.
- Do not create a second Provider route, parser registry, facts store or Evidence system.
- Do not enable broker execution; `broker_execution_allowed=false` remains unchanged.

## Workflows

- [Adaptive Edge](../../../docs/workflows/workflow-adaptive-edge.md) — use when immutable raw snapshots show provider schema drift and a candidate dialect repair must be validated without mutating production parsing.
