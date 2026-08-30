"""External market-data provider adapters."""

from astock.providers.baostock import BaoStockReferenceProvider
from astock.providers.bse_official_reference import BseOfficialReferenceProvider
from astock.providers.config import get_provider, load_provider_registry
from astock.providers.eastmoney import EastMoney5mProvider
from astock.providers.eastmoney_financial import EastMoneyFinancialProvider
from astock.providers.eastmoney_reference import EastMoneyReferenceProvider
from astock.providers.probe import ProviderProbeService, RawProbeResponse
from astock.providers.runtime import ProviderFactory, TransportProfile, load_transport_profiles
from astock.providers.sina import Sina5mProvider
from astock.providers.sina_financial import SinaFinancialProvider
from astock.providers.sina_reference import SinaReferenceProvider

__all__ = [
    "EastMoney5mProvider",
    "EastMoneyFinancialProvider",
    "EastMoneyReferenceProvider",
    "BaoStockReferenceProvider",
    "BseOfficialReferenceProvider",
    "ProviderFactory",
    "ProviderProbeService",
    "RawProbeResponse",
    "TransportProfile",
    "Sina5mProvider",
    "SinaFinancialProvider",
    "SinaReferenceProvider",
    "get_provider",
    "load_provider_registry",
    "load_transport_profiles",
]
