---
name: candidate-scan
description: Build an evidence-bounded A-share research candidate or observation registry from one immutable CandidateInputRelease. Use when the user asks what companies deserve research, what may be worth observing, for a candidate list, or to refresh an existing watchlist.
---

# 候选扫描

1. For normal discovery, start with `uv run astock research-seeds --live`; use `uv run astock probe` only for explicit capability diagnostics rather than as a per-request prerequisite.
2. For a broad current-market discovery request, first run `uv run astock research-seeds --live`. The command fetches XSHG/XSHE/BJSE market snapshots on demand with the hardware-aware worker budget. It merges existing `RESEARCH_READY` candidates, a **blind market tranche** from liquidity/scale data, and bounded Expert overlays from audited Skills plus public industry-board constituents. Expert-domain admission uses only the absolute audited-Skill count (`minimum_domain_skill_count`, default 3); the retired author-relative Skill-share threshold must not be restored. `skill_share` is diagnostic only. Expert overlay may add at most the active policy bonus and cannot displace the reserved blind tranche. Research Seeds are not CandidateRecords and never imply BUY.
3. Promote the bounded Seed set with `uv run astock research-seeds-promote <ResearchSeedReport-artifact-id> --live`. Promotion automatically reuses existing `RESEARCH_READY` candidates, binds each Seed to a verified current instrument identity, checks/syncs calendar and unadjusted daily data, evaluates research-sufficient data quality, enumerates official corporate actions and canonical announcements, reuses or runs FinancialIntegrity, builds the current `CandidateInputRelease`, and immediately runs Candidate Scan. The CURRENT path validates semantic identity/content/source lineage rather than historical PIT-head or wrapper-field equality. Corporate actions and material announcement-title hits are research signals to continue investigating, not reasons to stop promotion. Do not perform full deep-research evidence collection for the entire A-share market when a smaller Seed set exists.
4. CURRENT discovery uses the latest trustworthy information obtained during the request and freezes only after bounded acquisition/recovery. Artifacts must remain registered/readable and traceable to their source snapshots, but data arriving after the user's request timestamp is normal current acquisition rather than "future data". Historical scans, only when explicitly requested, may retain separate `CERTIFIED` / `DOCUMENT_RECONSTRUCTED` anti-lookahead rules. Expert Skill/domain matches are research-scope hints only; company facts still come from attributable sources.
5. `candidate-input-schema / candidate-input-stage / candidate-input-run` remain manual/diagnostic fallbacks for an already assembled release. In normal MCP/web-agent discovery, do not ask the model to hand-build the large release JSON when `research-seeds-promote` can derive it from registered Seed/reference/evidence artifacts.
6. Otherwise put only the release id and object hash in `CandidateScanRequest`, then run `uv run astock candidate-scan REQUEST.json`.
7. Inspect with `uv run astock candidate-status --scan-id SCAN_ID` or `--company-id COMPANY_ID`, and verify with `uv run astock candidate-audit SCAN_ID`.
8. `NEEDS_INFO` is an **internal recovery state**, not an investor-facing terminal state. Classify every gap before stopping: unreadable/corrupt canonical material → refetch/rebuild it; incomplete structured provider coverage → exhaust allowlisted fallback; missing/conflicting public facts → invoke `$evidence-investigation` and authoritative Web cross-check/capture; only genuinely private input or a public gap that remains unresolved after the bounded automatic-resolution budget may become `NEEDS_USER_INPUT`. Rerun promotion/scan after recovered artifacts are registered. For a user asking which stocks to buy、如何配组合或对候选做投资决策，Candidate Scan 是**同一 `FULL_RESEARCH_RECOMMENDATION` 请求中的内部中间态**：立即把 bounded research-ready shortlist 继续送入 `$company-deep-research`，完成后再回到 `$astock-research-orchestrator` 执行跨标的 Red Team、Committee、`$portfolio-manager`、Mandatory Research DAG、`RecommendationResearchReceipt` 与 Publication Gate；不得把 candidate ranking 单独输出为荐股答案，也不得用“下一步可以深研”把自动工作交回用户。Only names published by the final receipt may enter recommendation tracking as `RECOMMENDED`; raw Seed/Candidate membership must never enroll itself as `RECOMMENDED` or otherwise self-promote into a recommendation.
9. current full-market Seed acquisition 必须把“Universe 身份”与“当前行情 numerator”分离：XSHG/XSHE/BJSE 优先使用各交易所官方 Instrument Master 作为 denominator；EastMoney/Sina/Tencent 等公开行情源只能补价格、成交、换手和流通市值。行情源或 secondary master 即使自报 100% 也不能制造 `OFFICIAL_DENOMINATOR_RECONCILED`；正式全市场权限仍要求三市场官方 denominator 与 numerator 的 ObjectStore lineage 对账达到 >=99.5%。

## Deterministic policy

- `candidate-scan-v1` uses 20 valid trading days, median turnover of at least CNY 20 million, and a nonzero-turnover ratio of at least 0.90.
- A 20-day absolute price change of at least 15% plus current volume at least 1.5 times the prior-20 median is a weak clue only.
- Quality `FAIL` disables technical and liquidity support. `PARTIAL` is allowed only when the available current sample still satisfies the deterministic minimum needed by the scan and the degradation is explicit (for example, missing daily amount with a transparent `close×volume` proxy); otherwise automatic recovery continues.
- Official announcement/corporate-action metadata may create a bounded research signal at Candidate discovery. Full document/fact evidence is closed during company deep research before any BUY authority.
- Watchlist membership is user intent only. Holding review contributes only new or invalidating evidence.
- `RESEARCH_READY` means “worth deeper research,” not “ready to buy”: it requires at least MODERATE verified research signals, non-failed quality, liquidity pass, and `TRADABLE` status. Historical PIT safety is not a CURRENT admission criterion.

## Workflows

- [`docs/workflows/workflow-candidate-discovery.md`](../../../docs/workflows/workflow-candidate-discovery.md)

## Output boundary

The only durable outputs are `CandidateSignal`, `CandidateRecord`, `CandidateUniverseSnapshot`, `CandidateScanReport`, and `CandidateAuditReport`. `CandidateRecord` means “worth further research” or “observation only”; it is never paper eligibility or a trading instruction.

## Prohibitions

- Do not create BUY/SELL direction, target price, quantity, weight, order, position, `TradeProtocol`, committee decision, or paper-ledger mutation.
- If current market Universe/market Seeds are unavailable, do not hand-pick replacement names from Web/news/community material and present them as a market-wide shortlist. Automatically repair/refetch the Universe and use authoritative Web only to restore/cross-check the canonical coverage proof; stop only after the public recovery budget is exhausted.
- Do not turn price momentum, liquidity, or community popularity alone into `RESEARCH_READY`.
- In CURRENT mode, do not reject data merely because it arrived after request creation or carries a legacy `NOT_PIT_SAFE` marker. Do reject duplicate/corrupt/internally inconsistent data; unresolved missing/partial public data must trigger explicit degradation plus automatic recovery rather than silent use.
- Do not use an incomplete release to increment lifecycle misses or close a candidate.
- Do not bypass `astock` commands by directly editing SQLite.
