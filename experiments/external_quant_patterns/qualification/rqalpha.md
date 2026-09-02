# RQAlpha Qualification Report

## Scope

E-03 does **not** install or execute RQAlpha and copies no RQAlpha source. It independently models a small robustness/event-ordering idea with project-owned fixtures. Synthetic statistics in this experiment are not RQAlpha performance measurements.

## Identity

- Repository: https://github.com/ricequant/rqalpha
- Upstream reference observed: 2026-09-02, current public repository state; no RQAlpha package/release is installed or admitted here.
- License evidence: https://github.com/ricequant/rqalpha/blob/master/LICENSE
- The current repository license permits Apache-2.0 terms for defined non-commercial use, while commercial use or organizational use requires RiceQuant authorization; conflicting terms resolve to the repository's additional license.
- Runtime dependency added to AStockMultiAgent: none.

## Maintenance

The public repository remains available with current issues/commits. E-03 does not assert a latest release number because it does not execute a release. Any future runtime or code-reuse proposal must first obtain a use-rights determination appropriate to the actual user/organization.

## License / Data Rights

The repository's additional commercial-use restriction is material. Therefore E-03 treats RQAlpha as **reference only** and does not copy or adapt its source code. RQData/RiceQuant datasets and hosted services have separate commercial/data rights and are not used here.

## PIT / Provenance

A modeled event or robustness pattern does not establish A-share point-in-time availability or officiality. AStockMultiAgent retains its own SourceSnapshot/Evidence/PIT, official rulebook and classified execution contracts.

## Supply Chain / Cost

No RQAlpha package, RQData API, account, credential, external data, model or network call is used. The local experiment has no added transitive dependency.

## Exit / Deletion

Delete `experiments/external_quant_patterns/` and its test. No production module, migration, ledger, provider, scheduler or dependency points to RQAlpha.

## Decision

**WATCH_PATTERN_ONLY / REJECT code or runtime adoption under current scope.** The synthetic robustness pattern adds no demonstrated value beyond existing block bootstrap / deflated-Sharpe governance, and the repository's commercial-use restriction makes code/runtime adoption inappropriate without separate authorization. E-03 retains only an independently implemented conceptual comparison.
