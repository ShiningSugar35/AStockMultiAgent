---
name: paper-trading-recovery
description: Initialize, inspect, recover, or advance the deterministic paper-trading account and its 5-minute replay checkpoint. Use after startup or interruption, for paper account status, missing replay periods, open orders, journal integrity, frozen cash, positions, NAV, or stop/target recovery checks.
---

# 模拟盘恢复

1. Run `uv run astock init` only when state has not been initialized.
2. Run `uv run astock paper-status` and stop if integrity is not `ok` or any event is unbalanced.
3. Run `uv run astock probe`, synchronize the missing 5m interval, and inspect its quality report.
4. Use `uv run astock paper-replay` only with an existing canonical manifest.
5. Preserve the old canonical and checkpoint when a provider or quality gate fails.
6. Treat `shadow-status`, `shadow-audit`, `phase8-admission`, and `adaptive-research-status` as read-only analytical state. Shadow studies and their observations must never initialize, replay, repair, or mutate this account. Adaptive research has the same hard boundary.

## Output

Produce deterministic `PortfolioNAV` and `ReplayCheckpoint` values plus an integrity report. Use validated paper-account commands for any state change.

## Prohibitions

- Do not edit SQLite or advance a cursor past verified data.
- Do not replace missing 5m bars with invented data.
- Do not write shadow-study results, arm returns, or adaptive weights into the main paper ledger.
- Do not use adaptive-research eligibility or approval as a paper-account recovery or ledger-write command.
- Do not connect to or submit orders to a real broker.
