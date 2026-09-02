# LEAN Qualification Report

## Scope

E-03 does **not** install or run QuantConnect LEAN. It independently models a small event-ordering abstraction on fixed local fixtures and compares that abstraction with AStockMultiAgent's current classified protocol / execution / ledger ordering. The result is not a benchmark of the LEAN engine and must not be read as a claim that the full LEAN engine is weaker or stronger.

## Identity

- Repository: https://github.com/QuantConnect/Lean
- Upstream reference observed: 2026-09-02, current public repository state; no LEAN build/tag is installed or admitted here.
- License evidence: https://github.com/QuantConnect/Lean/blob/master/LICENSE
- Repository License: Apache License 2.0.
- Runtime dependency added to AStockMultiAgent: none.

## Maintenance

The public repository remains actively developed. E-03 does not depend on a claimed release number because the engine is not executed. A future engine trial would require a fixed Git revision/build, data-license review and independent SBOM.

## License / Data Rights

Apache-2.0 applies to the referenced LEAN repository subject to its terms. No LEAN code is copied into this experiment. QuantConnect-hosted services/data have separate terms and are outside this pattern-only test; this experiment uses only project-owned synthetic fixtures.

## PIT / Provenance

The modeled ordering abstraction cannot establish point-in-time correctness or source provenance. AStockMultiAgent's classification, evidence, official corporate-action baseline, confirmation and ledger contracts remain unchanged.

## Supply Chain / Cost

No .NET runtime, LEAN binaries, Python package, brokerage connector, hosted data, secret or network service is introduced. E-03 runs only local Python fixture code.

## Exit / Deletion

Delete `experiments/external_quant_patterns/` and its test. There are no LEAN artifacts, packages, migrations, providers, broker connectors or persistent state to migrate.

## Decision

**REJECT modeled ordering replacement; WATCH selected robustness ideas.** The specific event-order abstraction represented by the fixture does not justify replacing AStockMultiAgent's current ordering/governance chain. Parameter-perturbation or walk-forward ideas may be studied independently under existing model-risk governance, but the full LEAN engine is not adopted and no statement is made about its overall quality relative to this project.
