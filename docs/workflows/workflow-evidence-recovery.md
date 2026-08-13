# Workflow — Evidence Recovery

## When to use

Use when one bounded decision-relevant fact is unsupported, conflicting, stale, provider-blocked, or returned as a Committee/Research `NEEDS_INFO` gap.

Primary skill: `$evidence-investigation`.

## Flow

1. **Name the exact gap and decision impact**
   - Do not begin with unrestricted browsing.
   - If this is a committee task, inspect `committee-task-status` and keep the investigation scoped to that reason.

2. **Reuse existing frozen evidence**
   - Check registered snapshots/artifacts and their audit status before fetching again.
   - A valid existing source is preferred to duplicate collection.

3. **Diagnose, repair and fallback deterministically**
   - Current-investment recovery shares the 1800-second automatic budget from the orchestrator.
   - Diagnose whether the failure is transport, rate-limit/access, provider schema drift, request-binding drift, or data-quality failure; preserve that detail internally.
   - Retry only retryable transient errors with bounded attempts/circuit breaking. Prefer a more reliable endpoint for the same exact capability before falling back to a broad/bulk endpoint.
   - Move to configured backup providers when the preferred source still cannot safely answer the question. Do not interpret a provider failure as evidence against the underlying fact.

4. **Authoritative Web fallback**
   - If local/provider paths remain insufficient, automatically search the Web.
   - Priority: exchange → CNINFO/legal disclosure → issuer IR → regulator → reputable professional sources for corroboration.
   - Prefer pages/files that require no login/API key/user intervention.
   - For material contested facts, cross-check independent authoritative sources where practical.

5. **Freeze evidence when an importer exists**
   - When the repository supports deterministic capture/import, store raw source material immutably before advancing the chain.
   - Preserve exact page/section/DOM/snapshot locator and source availability.
   - Web material that cannot yet enter a formal artifact may support a clearly labelled provisional explanation, but cannot silently satisfy a frozen formal gate.

6. **Manual intervention is last**
   - Only after provider and authoritative Web paths are exhausted may manual user help be requested.
   - Aggregate all remaining requests into one checklist: exact document/action, why it matters, and what will resume after it is supplied.

7. **Close the named gap only**
   - If a new frozen artifact resolves a Committee task, link it with `committee-task-resolve`; the Committee still needs a new formal plan/decision.
   - Do not broaden one resolved fact into a full research rerun unless the change invalidates downstream artifacts.

## Output

Normal investor-facing output should say the resolved fact or plain-language remaining uncertainty and its decision impact. Provider IDs, exception classes, artifact IDs/hashes and command logs stay in diagnostics unless debugging is explicitly requested.

## Stop conditions

- Never bypass access controls, captchas, signatures or rate limits.
- Never call “no search result” proof of nonexistence.
- Never upgrade community content into statutory/official authority.
- Never ask the user to fetch a public authoritative source the Agent can obtain automatically.
