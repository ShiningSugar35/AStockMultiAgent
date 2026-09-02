# RD-Agent Qualification Report

## Scope

E-03 does **not** install or execute Microsoft RD-Agent. It independently models a bounded hypothesis → evaluation → feedback loop on local deterministic fixtures to ask whether that workflow pattern adds value beyond the project's existing prospective/shadow governance. Synthetic loop metrics are not measurements of RD-Agent itself.

## Identity

- Repository: https://github.com/microsoft/RD-Agent
- Upstream reference observed: 2026-09-02, current public repository state; no package version is installed or admitted here.
- License evidence: https://github.com/microsoft/RD-Agent/blob/main/LICENSE
- The repository License is MIT and the repository publishes current 2026 project/news activity.
- Runtime dependency added to AStockMultiAgent: none.

## Maintenance

The public repository remains active and documents quant/data-science scenarios. Because E-03 executes no RD-Agent release, this report deliberately does not assert a package release number. Any future runtime trial must freeze an exact revision and dependency/SBOM separately.

## License / Data Rights

MIT applies to the RD-Agent software subject to its notice terms. No RD-Agent source code is copied here. Market-data, papers, model APIs, generated factors and any third-party services keep their own rights and terms; the software license does not upgrade them.

## PIT / Provenance

An automated hypothesis loop can improve research throughput, but it cannot replace point-in-time evidence, pre-registration, independent validation or the formal recommendation gate. AStockMultiAgent keeps prospective/shadow experiments and model-risk admission as the controlling contract.

## Secrets / Supply Chain / Cost

A real RD-Agent run can require model/embedding backends and therefore credentials, cost and a substantially larger dependency/runtime surface. E-03 uses none of those: network requests=0, model calls=0, secrets=0, external dependencies=0.

## Exit / Deletion

Delete `experiments/external_quant_patterns/` and the associated test. No production model loop, provider, scheduler, database, weight or dependency points to RD-Agent.

## Decision

**SHADOW_EXPERIMENT pattern only.** A bounded automated hypothesis iteration pattern is worth future research only behind existing prospective/model-risk gates. It is not admitted to production research, recommendation, allocation or paper execution, and RD-Agent is not added as a runtime dependency.
