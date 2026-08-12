# Workflow — Paper Trading

## When to use

Use for simulated account initialization/status, recovery after interruption, canonical 5-minute replay, simulated order preparation/confirmation, settlement and NAV marking.

Primary skill: `$paper-trading-recovery`.

## Flow

1. **Initialize only when needed**
   - Run `uv run astock init` only if the runtime has not been initialized.
   - Otherwise preserve the existing account/journal.

2. **Check integrity before any mutation**
   - Run `paper-status`.
   - Stop on unbalanced journal, corrupt frozen authorization, broken object lineage or inconsistent account state.
   - Full SQLite `state-integrity-audit` is a separate diagnostic, not a prerequisite for every paper action.

3. **Acquire only verified market data**
   - Synchronize the missing 5m interval through existing provider/fallback logic.
   - Inspect quality/canonical manifest; one bad provider must not replace a previously valid canonical dataset.
   - Never invent bars or advance beyond verified data.

4. **Replay deterministically**
   - Run `paper-replay` only against an existing canonical manifest.
   - Preserve stable order sequencing, capacity constraints, T+1 lots and replay checkpoint monotonicity.
   - On provider/quality failure, keep the old canonical release/checkpoint.

5. **Prepare a simulated operation only from a valid formal protocol**
   - Use the paper execution/operation prepare path only when the exact Committee/ClassifiedTradeProtocol gates allow it.
   - Preparation does not write an order by itself.

6. **Require independent explicit confirmation**
   - Simulated order creation/cancel requires the configured user confirmation/signature/idempotency boundary.
   - Expired/invalid/replayed confirmation cannot create a second order.

7. **Settle and mark with verified references**
   - Settlement uses the verified trading calendar and exact lots.
   - NAV mark uses valid unadjusted reference releases.

8. **Keep experimental research isolated**
   - Shadow/Phase 7/Phase 8/Adaptive results are read-only analytical state for the main paper ledger.
   - They cannot initialize, repair, replay or mutate the account.

## Output

Show paper cash, frozen cash, positions/lots, open orders, NAV, replay checkpoint and integrity state in plain language. For recovery, say exactly which deterministic step can resume.

## Stop conditions

- No direct SQLite edits.
- No synthetic data to bridge a market gap.
- No shadow/adaptive output may mutate the main ledger.
- No real brokerage connection or real order submission exists.
