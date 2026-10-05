"""Gemeinsame Datenklassen für Broker, Engine und Simulation."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SymbolInfo:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float  # Gewinn/Verlust pro Tick und 1.0 Lot in Kontowährung
    contract_size: float
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    spread_points: float = 0.0
    stops_level_points: float = 0.0

    @property
    def spread_price(self) -> float:
        return self.spread_points * self.point


@dataclass
class AccountInfo:
    balance: float
    equity: float
    currency: str = "USD"
    leverage: int = 100
    is_demo: bool = True
    free_margin: float = 0.0


@dataclass
class Position:
    ticket: int
    symbol: str
    direction: int  # +1 = Long, -1 = Short
    volume: float
    open_price: float
    open_time: datetime
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0
    comment: str = ""


@dataclass
class ClosedTrade:
    ticket: int
    symbol: str
    direction: int
    volume: float
    open_price: float
    close_price: float
    open_time: datetime
    close_time: datetime
    profit: float
    reason: str = ""
    comment: str = ""


@dataclass
class OrderResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    message: str = ""
