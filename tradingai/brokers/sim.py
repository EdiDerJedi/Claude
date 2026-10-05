"""Simulierter Broker für Backtests und Paper-Trading (ohne echtes Geld)."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..models import AccountInfo, ClosedTrade, OrderResult, Position, SymbolInfo
from .base import Broker

log = logging.getLogger(__name__)


class SimulatedAccount:
    """Kontoführung mit Spread, Stop-Loss/Take-Profit und Kurslücken."""

    def __init__(self, balance: float, infos: dict):
        self.balance = float(balance)
        self.infos: dict[str, SymbolInfo] = infos
        self.positions: list[Position] = []
        self.closed: list[ClosedTrade] = []
        self.unreported: list[ClosedTrade] = []
        self.marks: dict[str, float] = {}
        self.next_ticket = 1

    def _half_spread(self, symbol: str) -> float:
        return self.infos[symbol].spread_price / 2.0

    def pnl(self, pos: Position, exit_price: float) -> float:
        info = self.infos[pos.symbol]
        return (exit_price - pos.open_price) * pos.direction / info.tick_size * info.tick_value * pos.volume

    def open(self, symbol: str, direction: int, volume: float, mid: float, when: datetime, sl: float, tp: float,
             comment: str = "") -> Position:
        price = mid + direction * self._half_spread(symbol)  # Kauf zum Ask, Verkauf zum Bid
        pos = Position(self.next_ticket, symbol, direction, volume, price, when, sl, tp, 0.0, comment)
        self.next_ticket += 1
        self.positions.append(pos)
        self.marks[symbol] = mid
        return pos

    def close(self, pos: Position, mid: float | None, when: datetime, reason: str, price: float | None = None) -> ClosedTrade:
        if price is None:
            price = mid - pos.direction * self._half_spread(pos.symbol)
        profit = self.pnl(pos, price)
        self.balance += profit
        self.positions = [p for p in self.positions if p.ticket != pos.ticket]
        trade = ClosedTrade(pos.ticket, pos.symbol, pos.direction, pos.volume, pos.open_price, price,
                            pos.open_time, when, profit, reason, pos.comment)
        self.closed.append(trade)
        self.unreported.append(trade)
        return trade

    def on_bar(self, symbol: str, when: datetime, o: float, h: float, l: float, c: float) -> None:
        """Prüft SL/TP mit der neuen Bar. Bei Treffer von beidem in einer Bar zählt
        konservativ der Stop-Loss (außer der Kurs eröffnet schon jenseits des TP)."""
        hs = self._half_spread(symbol)
        for pos in [p for p in self.positions if p.symbol == symbol]:
            d = pos.direction
            # Ausstiegspreise: Long schließt zum Bid (mid - hs), Short zum Ask (mid + hs)
            ex_open, ex_high, ex_low = o - d * hs, h - d * hs, l - d * hs
            fav, adv = (ex_high, ex_low) if d == 1 else (ex_low, ex_high)
            hit_sl = pos.sl > 0 and (adv - pos.sl) * d <= 0
            hit_tp = pos.tp > 0 and (fav - pos.tp) * d >= 0
            if pos.tp > 0 and (ex_open - pos.tp) * d >= 0:
                self.close(pos, None, when, "tp", price=ex_open)
            elif pos.sl > 0 and (ex_open - pos.sl) * d <= 0:
                self.close(pos, None, when, "sl", price=ex_open)
            elif hit_sl:
                self.close(pos, None, when, "sl", price=pos.sl)
            elif hit_tp:
                self.close(pos, None, when, "tp", price=pos.tp)
        self.marks[symbol] = c

    def equity(self) -> float:
        eq = self.balance
        for pos in self.positions:
            mid = self.marks.get(pos.symbol, pos.open_price)
            eq += self.pnl(pos, mid - pos.direction * self._half_spread(pos.symbol))
        return eq

    def mark_positions(self) -> list[Position]:
        for pos in self.positions:
            mid = self.marks.get(pos.symbol, pos.open_price)
            pos.profit = self.pnl(pos, mid - pos.direction * self._half_spread(pos.symbol))
        return list(self.positions)

    # ---------------------------------------------------------- Persistenz
    def to_dict(self) -> dict:
        def pos_d(p: Position):
            d = dict(p.__dict__)
            d["open_time"] = p.open_time.isoformat()
            return d

        def trade_d(t: ClosedTrade):
            d = dict(t.__dict__)
            d["open_time"], d["close_time"] = t.open_time.isoformat(), t.close_time.isoformat()
            return d

        return {
            "balance": self.balance,
            "next_ticket": self.next_ticket,
            "marks": self.marks,
            "positions": [pos_d(p) for p in self.positions],
            "closed": [trade_d(t) for t in self.closed[-500:]],
        }

    def load(self, data: dict) -> "SimulatedAccount":
        self.balance = float(data["balance"])
        self.next_ticket = int(data["next_ticket"])
        self.marks = {k: float(v) for k, v in data.get("marks", {}).items()}
        self.positions = [
            Position(**{**p, "open_time": datetime.fromisoformat(p["open_time"])}) for p in data.get("positions", [])
        ]
        self.closed = [
            ClosedTrade(**{**t, "open_time": datetime.fromisoformat(t["open_time"]),
                           "close_time": datetime.fromisoformat(t["close_time"])})
            for t in data.get("closed", [])
        ]
        return self


class _SimBrokerMixin:
    """Gemeinsame Order-Logik von Backtest- und Paper-Broker."""

    sim: SimulatedAccount

    def account(self) -> AccountInfo:
        eq = self.sim.equity()
        return AccountInfo(balance=self.sim.balance, equity=eq, is_demo=True, free_margin=eq)

    def symbol_info(self, symbol: str) -> SymbolInfo:
        return self.sim.infos[symbol]

    def _mid(self, symbol: str) -> float:
        if symbol not in self.sim.marks:
            raise RuntimeError(f"Noch kein Kurs für {symbol} bekannt")
        return self.sim.marks[symbol]

    def quote(self, symbol: str) -> tuple[float, float]:
        mid, hs = self._mid(symbol), self.sim.infos[symbol].spread_price / 2
        return mid - hs, mid + hs

    def positions(self, symbol: str | None = None) -> list[Position]:
        return [p for p in self.sim.mark_positions() if symbol is None or p.symbol == symbol]

    def open_position(self, symbol, direction, volume, sl, tp, comment="") -> OrderResult:
        if volume <= 0 or direction not in (1, -1):
            return OrderResult(False, message="ungültige Order")
        pos = self.sim.open(symbol, direction, volume, self._mid(symbol), self.now(), sl, tp, comment)
        self._after_change()
        return OrderResult(True, pos.ticket, pos.open_price)

    def close_position(self, position, comment="") -> OrderResult:
        if position.ticket not in {p.ticket for p in self.sim.positions}:
            return OrderResult(False, message="Position nicht gefunden")
        trade = self.sim.close(position, self._mid(position.symbol), self.now(), comment or "signal")
        self._after_change()
        return OrderResult(True, position.ticket, trade.close_price)

    def modify_position(self, position, sl, tp) -> OrderResult:
        for p in self.sim.positions:
            if p.ticket == position.ticket:
                p.sl, p.tp = sl, tp
                self._after_change()
                return OrderResult(True, p.ticket)
        return OrderResult(False, message="Position nicht gefunden")

    def pop_closed_trades(self) -> list[ClosedTrade]:
        out, self.sim.unreported = self.sim.unreported, []
        return out

    def _after_change(self) -> None:
        pass


class BacktestBroker(_SimBrokerMixin, Broker):
    """Spielt historische Daten Bar für Bar ab – ohne Blick in die Zukunft."""

    def __init__(self, data: dict, infos: dict, initial_balance: float, start: int = 0):
        self.data = data
        timeline = sorted(set().union(*[set(df.index) for df in data.values()]))
        self.timeline = pd.DatetimeIndex(timeline)
        self.sim = SimulatedAccount(initial_balance, infos)
        # Für jeden Zeitpunkt: Anzahl Bars je Symbol, die bis dahin abgeschlossen sind
        self._count = {
            s: np.searchsorted(df.index.values, self.timeline.values, side="right") for s, df in data.items()
        }
        self.i = min(max(start, 0), len(self.timeline) - 1)
        for s in data:
            n = self._count[s][self.i]
            if n:
                self.sim.marks[s] = float(data[s]["close"].iloc[n - 1])

    def now(self) -> datetime:
        return self.timeline[self.i].to_pydatetime()

    def has_next(self) -> bool:
        return self.i < len(self.timeline) - 1

    def advance(self) -> None:
        self.i += 1
        t = self.timeline[self.i]
        for s, df in self.data.items():
            n = self._count[s][self.i]
            if n and df.index[n - 1] == t:
                row = df.iloc[n - 1]
                self.sim.on_bar(s, t.to_pydatetime(), row["open"], row["high"], row["low"], row["close"])

    def get_rates(self, symbol, timeframe, count) -> pd.DataFrame:
        n = self._count[symbol][self.i]
        return self.data[symbol].iloc[max(0, n - count):n]


class PaperBroker(_SimBrokerMixin, Broker):
    """Paper-Trading mit Live-Daten aus dem Internet. Kontostand wird gespeichert."""

    def __init__(self, infos: dict, initial_balance: float, state_path: str | Path, fetcher,
                 refresh_seconds: float = 60.0):
        self.state_path = Path(state_path)
        self.fetcher = fetcher  # fetcher(symbol) -> DataFrame mit abgeschlossenen Bars
        self.refresh_seconds = refresh_seconds
        self.sim = SimulatedAccount(initial_balance, infos)
        self._cache: dict = {}
        self.last_checked: dict = {}
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.sim.load(data["account"])
            self.last_checked = {k: pd.Timestamp(v) for k, v in data.get("last_checked", {}).items()}
            log.info("Paper-Konto geladen: Balance %.2f, %d offene Positionen", self.sim.balance,
                     len(self.sim.positions))

    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def get_rates(self, symbol, timeframe, count) -> pd.DataFrame:
        cached = self._cache.get(symbol)
        if cached is None or time.monotonic() - cached[0] > self.refresh_seconds:
            df = self.fetcher(symbol)
            self._cache[symbol] = (time.monotonic(), df)
        else:
            df = cached[1]
        if df.empty:
            return df
        self._process_new_bars(symbol, df)
        return df.iloc[-count:]

    def _process_new_bars(self, symbol: str, df: pd.DataFrame) -> None:
        last = self.last_checked.get(symbol)
        new = df if last is None else df[df.index > last]
        if last is None:
            new = df.iloc[-1:]
        for t, row in new.iterrows():
            self.sim.on_bar(symbol, t.to_pydatetime(), row["open"], row["high"], row["low"], row["close"])
        self.last_checked[symbol] = df.index[-1]
        if len(new):
            self._after_change()

    def _after_change(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        payload = {"account": self.sim.to_dict(),
                   "last_checked": {k: v.isoformat() for k, v in self.last_checked.items()}}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)
