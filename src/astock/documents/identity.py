"""Cached exact official-provider identity resolution with auditable discovery lineage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from astock.core.state import StateStore
from astock.schemas import DisclosureExchange, SourceSnapshot


@dataclass(frozen=True, slots=True)
class OfficialIdentityResolution:
    provider_id: str
    symbol: str
    exchange: DisclosureExchange
    external_id: str | None
    discovery_snapshot_id: str | None
    available_to_system_at: datetime | None
    cache_hit: bool


class OfficialIdentityResolver:
    """Resolve provider-native company ids without treating code heuristics as facts."""

    def __init__(self, state: StateStore, provider_id: str) -> None:
        self.state = state
        self.provider_id = provider_id

    def resolve(
        self,
        symbol: str,
        exchange: DisclosureExchange,
        discover: Callable[[], tuple[str | None, SourceSnapshot, int]],
    ) -> tuple[OfficialIdentityResolution, int]:
        scope_key = self._scope_key(symbol, exchange)
        cached = self.state.get_checkpoint("official-identity", scope_key)
        if cached is not None:
            cursor = cached.get("cursor", {})
            external_id = cursor.get("external_id")
            snapshot_id = cursor.get("discovery_snapshot_id")
            if isinstance(external_id, str) and external_id and isinstance(snapshot_id, str):
                snapshot = self.state.get_snapshot(snapshot_id)
                if snapshot is not None:
                    return (
                        OfficialIdentityResolution(
                            provider_id=self.provider_id,
                            symbol=symbol,
                            exchange=exchange,
                            external_id=external_id,
                            discovery_snapshot_id=snapshot_id,
                            available_to_system_at=snapshot.available_to_system_at,
                            cache_hit=True,
                        ),
                        0,
                    )
        external_id, snapshot, latency_ms = discover()
        if external_id:
            self.state.set_checkpoint(
                scope_type="official-identity",
                scope_key=scope_key,
                cursor={
                    "external_id": external_id,
                    "discovery_snapshot_id": snapshot.snapshot_id,
                    "available_to_system_at": snapshot.available_to_system_at.isoformat(),
                },
                status="SUCCEEDED",
                object_hash=snapshot.object_sha256,
            )
        return (
            OfficialIdentityResolution(
                provider_id=self.provider_id,
                symbol=symbol,
                exchange=exchange,
                external_id=external_id,
                discovery_snapshot_id=snapshot.snapshot_id,
                available_to_system_at=snapshot.available_to_system_at,
                cache_hit=False,
            ),
            latency_ms,
        )

    def _scope_key(self, symbol: str, exchange: DisclosureExchange) -> str:
        return f"{self.provider_id}:{exchange.value}:{symbol}"


__all__ = ["OfficialIdentityResolution", "OfficialIdentityResolver"]
