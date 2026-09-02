---
name: source-qualification-auditor
description: Audit external capability qualification evidence, M-06 admission gates, revocation, and safe fallback without granting source authority.
---

# External Capability Qualification Auditor

1. Start from the canonical registry with `uv run astock external-capability-list` and inspect the target with `uv run astock external-capability-status <capability_id>`.
2. Read `uv run astock external-capability-schema` before preparing a qualification. The report must use the existing `CapabilityQualificationReport`; do not create a parallel admission model.
3. Verify every M-06 gate from current evidence: fixed candidate identity, License, terms of service, data rights, PIT, provenance, credential handling, SBOM, security review, maintenance, cost, latency, cache behavior, offline behavior, failure behavior, and exit/uninstall.
4. Treat recorded validation and controlled-live validation as separate gates. A fixture, package import, README claim, stars, download count, or registry listing never substitutes for a controlled-live capability check.
5. Store the evidence bundle in the existing ObjectStore before `uv run astock external-capability-qualify <report.json>` so every `evidence_object_hashes` entry is verifiable. A `PRODUCTION_BACKUP` report is valid only when every M-06 check and both validations are `PASS`.
6. For Provider-backed capabilities, keep `SourceAccessRouter`, Provider Registry, SourcePolicyGate, SourceSnapshot and Evidence as the only source path. The backup may be selected only after the standard route is unavailable and the active qualification report remains valid.
7. Revoke with `uv run astock external-capability-revoke <revocation.json>` when evidence expires, drifts, is withdrawn, or an exit drill fails. Verify that stored evidence and primary routes remain usable after revocation.
8. Record the Skill selection/completion through `uv run astock agent-observation-register <request.json>` and inspect aggregate telemetry with `uv run astock agent-observability-report --lookback-days 30`.

## Output

Return the capability id and fixed version, immutable evidence hashes, per-gate PASS/FAIL state, recorded/live results, admitted stage, reason codes, validity window, and revocation/exit result. Explicitly separate package/tool licensing from upstream data rights and source authority.

## Prohibitions

- Do not infer License, terms, data rights, PIT, security, maintenance, latency, or SBOM from popularity or configuration declarations.
- Do not admit `PRODUCTION_BACKUP` when any M-06 check, recorded validation, controlled-live validation, or disable/uninstall regression is missing or failed.
- Do not let a Skill, parser, crawler, MCP, or secondary Provider upgrade the authority of the underlying source material.
- Do not create a second Router, Provider registry, Evidence store, ObjectStore, qualification index, or mutable copy of qualification history.
- Do not enable broker order execution. `broker_execution_allowed=false` and the M-06 permanent rejection of execution-capable broker MCPs remain unchanged.

## Workflows

- [Research Tech Scout](../../../docs/workflows/workflow-research-tech-scout.md) — use after discovery when a candidate needs deterministic qualification, revocation, and exit evidence before it can become a production backup.
