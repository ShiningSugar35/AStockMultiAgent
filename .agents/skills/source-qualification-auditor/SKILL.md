---
name: source-qualification-auditor
description: Unified external capability qualification and decision execution; fixed version/hash, License/ToS, data rights, true upstream, PIT/provenance, credential handling, SBOM, security record, performance, cost, fault, validity, revocation, exit verification
---

# Source Qualification Auditor

## Responsibility
Unified external capability qualification and decision execution: fixed version/hash, License/ToS, data rights, true upstream, PIT/provenance, credential handling, SBOM, security record, performance, cost, fault, validity, revocation, exit verification. This Skill only organizes existing deterministic capability commands and artifacts, does not independently grant source authority.

## Triggers
- **Negative**: When qualification checks fail (license, ToS, data rights, PIT, provenance, SBOM, security review, maintenance, cost, latency, cache behavior, offline behavior, failure behavior, exit uninstall), the skill returns `REJECTED` with specific reason codes.
- **Conflict routing**: When multiple capabilities conflict for the same resource, the skill resolves routing through the existing `SourceAccessRouter` without establishing a second bus.
- **Fact/permission protection**: Ensures qualification artifacts and evidence objects are read-only append-only; no modification of existing qualification records.
- **Recovery**: When a capability is revoked or expires, the skill ensures existing Provider Registry routes and stored snapshots remain authoritative.
- **Exit/Uninstall**: On skill unload, all qualification records and evidence objects are preserved; adapter deregistration follows the exit contract of the associated capability definition.

## Workflow
1. Accept `ExternalCapabilityQualificationRequest` with candidate version, source metadata, and qualification checks.
2. Validate fixed version, License, ToS, data rights, PIT provenance, SBOM, security review, maintenance, cost, latency, cache behavior, offline behavior, failure behavior, and exit uninstall.
3. If all checks pass, produce `CapabilityQualificationReport` and advance stage per capability maximum.
4. If any check fails, produce `REJECTED` verdict with reason codes; candidate remains in `SHADOW` or `DISCOVERY_ONLY`.
5. On revocation, produce `CapabilityRevocation` artifact; existing routes remain available.
6. On uninstall, deregister adapter per exit contract; qualification history preserved in append-only storage.

## Observability
- Records selection reasons, hits, failures, fallback, latency, cost, qualification validity and revocation status per `agent-observation-register`.
- Emits `agent-observability-report` compatible metrics via ResearchRun checkpoint.

## Hard Contracts
- Does not establish second facts source, routing, evidence, user state, agent scheduling or trading execution system.
- All passing items enter existing `M-06 ExternalCapabilityRegistry/QualificationRelease` Router fallback, not a second bus.
- Execution-type broker MCP permanently rejected: `broker_execution_allowed=false` remains.
- Large dependency/lazy loading does not enter core minimal installation.
- Public live only read-only low-frequency: prohibited account/Secret/write operations.