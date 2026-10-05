"""Gemeinsame Schnittstelle für MetaTrader 5, Paper-Trading und Backtest."""

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

from ..models import AccountInfo, ClosedTrade, OrderResult, Position, SymbolInfo


class Broker(ABC):
    def connect(self) -> None:
        """Verbindung herstellen (optional)."""

    def shutdown(self) -> None:
        """Verbindung trennen (optional)."""

    @abstractmethod
    def now(self) -> datetime:
        """Aktuelle Zeit (naiv, UTC bzw. Serverzeit)."""

    @abstractmethod
    def account(self) -> AccountInfo: ...

    @abstractmethod
    def symbol_info(self, symbol: str) -> SymbolInfo: ...

    @abstractmethod
    def quote(self, symbol: str) -> tuple[float, float]:
        """(Bid, Ask)"""

    @abstractmethod
    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Nur abgeschlossene Bars, Spalten open/high/low/close/volume."""

    @abstractmethod
    def positions(self, symbol: str | None = None) -> list[Position]:
        """Nur Positionen dieses Bots."""

    @abstractmethod
    def open_position(self, symbol: str, direction: int, volume: float, sl: float, tp: float,
                      comment: str = "") -> OrderResult: ...

    @abstractmethod
    def close_position(self, position: Position, comment: str = "") -> OrderResult: ...

    @abstractmethod
    def modify_position(self, position: Position, sl: float, tp: float) -> OrderResult: ...

    def pop_closed_trades(self) -> list[ClosedTrade]:
        """Seit dem letzten Aufruf geschlossene Trades (für das Trade-Journal)."""
        return []
