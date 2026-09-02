# Qlib Qualification Report

## Scope

E-03 does **not** install, import, execute, or benchmark Microsoft Qlib itself. The experiment independently models one public architectural idea—experiment/run/metric/artifact recording—against AStockMultiAgent's current observation/checkpoint contracts on fixed local fixtures. Therefore the result is a **pattern comparison**, not a Qlib performance claim.

## Identity

- Repository: https://github.com/microsoft/qlib
- Upstream reference observed: 2026-09-02, current public repository state; no package version is installed or admitted by this experiment.
- License evidence: https://github.com/microsoft/qlib/blob/main/LICENSE
- Repository identifies the project as MIT licensed.
- Runtime dependency added to AStockMultiAgent: none.

## Maintenance

The public repository is still maintained and documents current integration with RD-Agent. E-03 intentionally does not rely on a claimed "latest release" number because no Qlib release is executed or pinned here; a later runtime-admission task would have to freeze an exact package/repository revision separately.

## License / Data Rights

MIT permits reuse subject to its notice requirements. This experiment copies no Qlib source code. Qlib's software license does not grant rights to any market dataset a user may connect, so data rights remain outside this pattern-only experiment.

## PIT / Provenance

The modeled Recorder hierarchy is useful for grouping experiments, but by itself does not prove A-share point-in-time availability, officiality, or provenance. AStockMultiAgent's existing SourceSnapshot/ObjectStore/Evidence/PIT contracts remain authoritative.

## Supply Chain / Cost

No Qlib package, model, data bundle, service, secret, network call, or transitive dependency is used. The benchmark cost is fixed local Python execution only.

## Exit / Deletion

Delete `experiments/external_quant_patterns/` and its test file. No production provider, schema, database migration, scheduler, ledger, or default dependency refers to Qlib.

## Decision

**ADAPT_PATTERN only.** Experiment-level grouping can inform future research-run observability, but wholesale Qlib adoption would duplicate existing runtime, ObjectStore, Evidence, model-risk and paper-trading architecture. This decision does not qualify Qlib as a production runtime dependency.
