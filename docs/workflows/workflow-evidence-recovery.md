# Workflow — Evidence Recovery

## When to use

Use `$evidence-investigation` for one unsupported, conflicting, stale or unavailable decision-relevant fact. Preserve the original request/task identity.

## Flow

1. **Scope and reuse.** Name the claim and decision impact, inspect an existing committee task when applicable, then reuse valid registered snapshots and body content before making another request.
2. **Diagnose within policy.** The active Current Research policy owns the budget. `SourceAccessRouter` ranks registered routes by capability/authority/health/freshness/cost; retry transient faults only. A provider failure is not evidence against a fact. Record sent/returned/handled separately. `ProviderRecoveryProposal` needs deterministic validation; Schema Repair is raw-first and cannot directly modify formal facts or active dialect. For supported basic/market/financial/macro supplements, use the Tushare route below before generic Web recovery.
3. **Authoritative Web fallback.** Use observed host capabilities, with exchange/CNINFO/issuer/regulator sources preferred. A dynamic source passes `source-proposal-check` before admission. Search is discovery; exact-item admission proves only that document, with `official_exhaustive_proof_allowed=false`. Exhaustive absence requires a capable enumeration route, terminal pagination and complete source lineage.
4. **Complete the host operation.** Read only relevant `web-tool-schema` / `src/astock/schemas/web_tools.py` contracts. Record SEARCH and FETCH availability, exact observed tool and billing separately for this invocation. Antigravity `search_web` and `read_url_content` can be `INCLUDED` only when observed. `UNKNOWN`, expiry or a fabricated tool name cannot authorize paid backup.
5. **Freeze evidence and resume.** Preserve exact page/section/DOM/source dates through the existing official capture/ObjectStore path. A search hit or fetched Markdown body does not satisfy a formal PDF requirement. Submit the typed automatic resolution to the same continuation; a committee gap gets `committee-task-resolve` followed by a new committee decision. Only invalidate affected downstream artifacts.
6. **Classify the remaining gap.** Use `NEEDS_USER_INPUT` only for an identified private input or authorization dependency, without wasting public searches for that known private gap; continue independent work. Distinguish proved public-route exhaustion (`PUBLIC_DATA_UNAVAILABLE`) from time/attempt limits (`BUDGET_EXHAUSTED`). Preserve captures and reconcile them locally after the network recovery budget is exhausted. Neither public-source failure nor budget exhaustion creates a private-material demand.

## Host tool dispatch

Use `uv run astock web-tool-run REQUEST.json --host-capabilities HOST.json`. For a host dispatch, invoke the actually observed tool and return `web-tool-complete RESULT.json`; use `web-tool-cancel <operation-id>` for abandoned work. Python owns operation identity, leases, budgets and registration; the Agent owns host invocation. Reuse sufficient content returned by search or a prior fetch.

Only an explicit host capability gap enables configured backup. DeepSeek fallback uses `POST /v1/tools/search` and `POST /v1/tools/fetch`; no chat-completions substitute or undocumented endpoint. Use `configs/web_tools.yaml` for limits and policy rather than copying them into this workflow.

## Structured supplements

For basic/market/financial/macro gaps read `tushare-schema --endpoint ...`, then a bounded `tushare-query`. Official endpoint links and limits live in `configs/tushare_access.yaml`; examples live in `integrations/tushare/README.md`. Do not load all upstream interfaces or claim this replaces formal financial_sources certification. Existing optional `research-supplemental-acquire` hints / Finnhub leads and established BaoStock routes remain applicable.

## Output

Return the verified fact, exact provenance, remaining uncertainty and decision impact. Diagnostics, operation IDs, provider errors and hashes stay internal unless the user asks for development detail.

## Stop conditions

- Do not bypass access controls or expand a bounded gap into an unrestricted collector.
- Do not refetch sufficient bodies, trigger paid fallback from unknown host state or invent completed dispatches.
- Do not call no-search-result proof of absence, relabel Markdown as a formal PDF or promote community authority.
- Do not ask the user to fetch available public sources or rerun the whole graph after a local repair.
