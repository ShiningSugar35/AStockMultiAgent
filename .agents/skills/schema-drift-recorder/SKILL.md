---
name: schema-drift-recorder
description: Raw-first response saving, difference generation, minimal regression fixture, Provider dialect/Schema Repair proposal, validation/rollback; does not bypass existing access flow to directly modify active production parser
---

# Schema Drift Recorder

## Responsibility
Raw-first response saving, difference generation, minimal regression fixture, Provider dialect or Schema Repair proposal, validation and rollback. This Skill does not bypass existing access flow to directly modify active production parser.

## Triggers
- **Negative**: When raw response saving fails (disk full, permission denied, invalid path), when difference generation produces empty or corrupt fixtures, or when dialect proposal contains invalid syntax, the skill returns `REJECTED` with specific error codes.
- **Conflict routing**: When multiple drift recordings compete for the same response contract, the skill resolves through the existing Provider dialect management without establishing a second access path.
- **Fact/permission protection**: Ensures recorded raw responses are immutable append-only; no modification of existing Provider responses or dialect configurations.
- **Recovery**: When a drift recording or fixture is lost, the skill re-generates from the original SourceSnapshot; existing production parser remains unchanged.
- **Exit/Uninstall**: On skill unload, all recorded raw responses, diff fixtures, and dialect proposals are preserved in ObjectStore; Provider dialect registry reverts to last validated state.

## Workflow
1. Capture a raw provider response and associate it with a SourceSnapshot.
2. Generate structural diff against last validated response for the same capability.
3. Produce minimal regression fixture (smallest input that reproduces any drift observed).
4. Propose a Provider dialect or Schema Repair patch if drift exceeds tolerance threshold.
5. Submit proposal for validation: must pass active policy's multi-sample SourceSnapshot test and repository true contract test.
6. If validated, generate ADMITTED candidate dialect; does not automatically modify active dialect/config or write formal facts.
7. If not validated, return `REJECTED`; proposal can be resubmitted after correction.
8. On rollback, revert to last ADMITTED dialect; all prior recordings remain as audit evidence.

## Observability
- Records drift detection events, fixture hashes, proposal outcomes per `agent-observation-register`.
- Emits `agent-observability-report` compatible metrics via ResearchRun checkpoint.

## Hard Contracts
- Does not bypass existing access flow to directly modify active production parser.
- All passing items enter existing `M-06 ExternalCapabilityRegistry/QualificationRelease` Router fallback, not a second bus.
- Execution-type broker MCP permanently rejected: `broker_execution_allowed=false` remains.
- Large dependency/lazy loading does not enter core minimal installation.
- Public live only read-only low-frequency: prohibited account/Secret/write operations.
- Raw-first: must satisfy active policy's multi-sample SourceSnapshot, official artifact type, and repository true contract test before PROPOSED → VALIDATED entry.