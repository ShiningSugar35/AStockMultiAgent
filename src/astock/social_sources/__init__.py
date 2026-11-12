"""Browser-session social search sources for investment research."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astock.social_sources.provider import SocialBrowserSearchProvider

from astock.social_sources.models import (
    SocialCaptureStatus,
    SocialLead,
    SocialPlatform,
    SocialPlatformCoverage,
    SocialSearchReceipt,
    SocialSearchRequest,
    SocialSearchResult,
)

# Do not eagerly import provider adapters: provider self-probe may import our
# schemas while the canonical ProviderBase is still initializing.


def __getattr__(name: str):
    if name == "SocialBrowserSearchProvider":
        from astock.social_sources.provider import SocialBrowserSearchProvider

        return SocialBrowserSearchProvider
    raise AttributeError(name)

__all__ = [
    "SocialBrowserSearchProvider",
    "SocialCaptureStatus",
    "SocialLead",
    "SocialPlatform",
    "SocialPlatformCoverage",
    "SocialSearchReceipt",
    "SocialSearchRequest",
    "SocialSearchResult",
]
