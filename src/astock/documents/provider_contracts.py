"""Structural capability contracts for official disclosure acquisition."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from astock.schemas import (
    DisclosureAnnouncement,
    DisclosureSearchBatch,
    DisclosureSearchRequest,
    DownloadedDocument,
)


@runtime_checkable
class DisclosureEnumerationProvider(Protocol):
    """Enumerate a bounded disclosure window and download exact official documents."""

    def search_all(
        self,
        request: DisclosureSearchRequest,
        *,
        max_pages: int = 200,
    ) -> list[DisclosureSearchBatch]: ...

    def download(self, announcement: DisclosureAnnouncement) -> DownloadedDocument: ...


__all__ = ["DisclosureEnumerationProvider"]
