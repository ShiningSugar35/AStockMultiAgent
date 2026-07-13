"""External market-data provider adapters."""

from astock.providers.eastmoney import EastMoney5mProvider
from astock.providers.sina import Sina5mProvider

__all__ = ["EastMoney5mProvider", "Sina5mProvider"]
