"""Anbindung an MetaTrader 5 über das offizielle Python-Paket `MetaTrader5`.

Voraussetzungen:
- Windows (das Paket MetaTrader5 gibt es nur für Windows)
- installiertes und eingeloggtes MetaTrader-5-Terminal
- im Terminal: Extras > Optionen > Expert Advisors > "Algorithmischen Handel erlauben"
"""

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..config import MT5Config
from ..models import AccountInfo, ClosedTrade, OrderResult, Position, SymbolInfo
from .base import Broker

log = logging.getLogger(__name__)

# Bits von symbol_info().filling_mode
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


def _import_mt5():
    try:
        import MetaTrader5 as mt5  # noqa: N813
    except ImportError as exc:
        raise ImportError(
            "Das Paket 'MetaTrader5' fehlt. Es läuft nur unter Windows: pip install MetaTrader5"
        ) from exc
    return mt5


class MT5Broker(Broker):
    def __init__(self, cfg: MT5Config, mt5_module=None, journal_state: dict | None = None):
        self.cfg = cfg
        self.mt5 = mt5_module or _import_mt5()
        # Bereits ins Trade-Journal übernommene Deals; wird von der Engine mitgespeichert,
        # damit nach einem Neustart nichts doppelt eingetragen wird.
        self.journal = journal_state if journal_state is not None else {}

    # ------------------------------------------------------------ Verbindung
    def connect(self) -> None:
        mt5 = self.mt5
        kwargs = {}
        if self.cfg.login:
            kwargs["login"] = int(self.cfg.login)
            # Leere Werte nicht übergeben: dann nimmt MT5 die im Terminal gespeicherten Zugangsdaten
            if self.cfg.password:
                kwargs["password"] = str(self.cfg.password)
            if self.cfg.server:
                kwargs["server"] = str(self.cfg.server)
        ok = mt5.initialize(self.cfg.path, **kwargs) if self.cfg.path else mt5.initialize(**kwargs)
        if not ok:
            raise ConnectionError(f"MT5-Initialisierung fehlgeschlagen: {mt5.last_error()}")
        acc = mt5.account_info()
        if acc is None:
            raise ConnectionError(f"Kein MT5-Konto verfügbar: {mt5.last_error()}")
        term = mt5.terminal_info()
        if term is not None and not getattr(term, "trade_allowed", True):
            log.warning("Im MT5-Terminal ist 'Algo Trading' ausgeschaltet – Orders werden abgelehnt!")
        log.info("Verbunden mit MT5: Konto %s (%s), Server %s, Balance %.2f %s",
                 acc.login, "DEMO" if self.is_demo() else "ECHTGELD", acc.server, acc.balance, acc.currency)

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def reconnect(self) -> None:
        """Verbindung neu aufbauen, z.B. nachdem MT5 neu gestartet wurde."""
        try:
            self.mt5.shutdown()
        except Exception:
            pass
        self.connect()

    def is_demo(self) -> bool:
        acc = self.mt5.account_info()
        return acc is not None and acc.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO

    # ------------------------------------------------------------- Abfragen
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def account(self) -> AccountInfo:
        acc = self.mt5.account_info()
        if acc is None:
            raise ConnectionError(f"account_info fehlgeschlagen: {self.mt5.last_error()}")
        return AccountInfo(
            balance=float(acc.balance),
            equity=float(acc.equity),
            currency=acc.currency,
            leverage=int(acc.leverage),
            is_demo=acc.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO,
            free_margin=float(acc.margin_free),
            login=int(getattr(acc, "login", 0) or 0),
            server=str(getattr(acc, "server", "") or ""),
        )

    def _raw_info(self, symbol: str):
        if not self.mt5.symbol_select(symbol, True):
            raise ValueError(f"Symbol {symbol} nicht verfügbar: {self.mt5.last_error()}")
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise ValueError(f"symbol_info({symbol}) fehlgeschlagen: {self.mt5.last_error()}")
        return info

    def symbol_info(self, symbol: str) -> SymbolInfo:
        i = self._raw_info(symbol)
        return SymbolInfo(
            name=symbol,
            digits=int(i.digits),
            point=float(i.point),
            tick_size=float(i.trade_tick_size or i.point),
            tick_value=float(i.trade_tick_value),
            contract_size=float(i.trade_contract_size),
            volume_min=float(i.volume_min),
            volume_max=float(i.volume_max),
            volume_step=float(i.volume_step),
            spread_points=float(i.spread),
            stops_level_points=float(getattr(i, "trade_stops_level", 0)),
        )

    def quote(self, symbol: str) -> tuple[float, float]:
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"Kein Kurs für {symbol}: {self.mt5.last_error()}")
        return float(tick.bid), float(tick.ask)

    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        tf = getattr(self.mt5, f"TIMEFRAME_{timeframe}")
        self.mt5.symbol_select(symbol, True)
        rates = self.mt5.copy_rates_from_pos(symbol, tf, 0, count + 1)
        if rates is None or len(rates) == 0:
            log.warning("Keine Kursdaten für %s: %s", symbol, self.mt5.last_error())
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rates)
        df.index = pd.DatetimeIndex(pd.to_datetime(df["time"], unit="s"), name="time")
        df["volume"] = df["tick_volume"].astype(float)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df.iloc[:-1]  # letzte Bar ist noch nicht abgeschlossen

    def positions(self, symbol: str | None = None) -> list[Position]:
        raw = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        out = []
        for p in raw or []:
            if p.magic != self.cfg.magic:
                continue  # manuelle Trades oder andere EAs nicht anfassen
            out.append(Position(
                ticket=int(p.ticket),
                symbol=p.symbol,
                direction=1 if p.type == self.mt5.POSITION_TYPE_BUY else -1,
                volume=float(p.volume),
                open_price=float(p.price_open),
                open_time=datetime.fromtimestamp(p.time, tz=timezone.utc).replace(tzinfo=None),
                sl=float(p.sl),
                tp=float(p.tp),
                profit=float(p.profit),
                comment=p.comment,
            ))
        return out

    # --------------------------------------------------------------- Orders
    def _filling(self, info) -> int:
        mode = int(getattr(info, "filling_mode", 0))
        if mode & SYMBOL_FILLING_FOK:
            return self.mt5.ORDER_FILLING_FOK
        if mode & SYMBOL_FILLING_IOC:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def _send(self, request: dict) -> OrderResult:
        result = self.mt5.order_send(request)
        if result is None:
            return OrderResult(False, message=f"order_send fehlgeschlagen: {self.mt5.last_error()}")
        ok = result.retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL)
        ticket = int(getattr(result, "order", 0) or 0)
        return OrderResult(ok, ticket, float(getattr(result, "price", 0.0) or 0.0),
                           f"retcode={result.retcode} {getattr(result, 'comment', '')}")

    @staticmethod
    def _norm(price: float, info) -> float:
        """Preis auf die Tick-Größe des Symbols runden (sonst lehnt der Server ab)."""
        if not price:
            return 0.0
        tick = float(getattr(info, "trade_tick_size", 0) or info.point)
        return round(round(price / tick) * tick, int(info.digits))

    def _fit_stops(self, info, direction: int, sl: float, tp: float, bid: float, ask: float):
        """Mindestabstand des Brokers einhalten. MT5 prüft SL/TP einer Long-Position gegen den
        Bid und einer Short-Position gegen den Ask."""
        ref = bid if direction == 1 else ask
        min_dist = (float(getattr(info, "trade_stops_level", 0) or 0) + 1) * info.point
        if sl and (ref - sl) * direction < min_dist:
            sl = ref - direction * min_dist
        if tp and (tp - ref) * direction < min_dist:
            tp = ref + direction * min_dist
        return self._norm(sl, info), self._norm(tp, info)

    def open_position(self, symbol, direction, volume, sl, tp, comment="") -> OrderResult:
        info = self._raw_info(symbol)
        bid, ask = self.quote(symbol)
        price = ask if direction == 1 else bid
        sl, tp = self._fit_stops(info, direction, sl, tp, bid, ask)
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": self.mt5.ORDER_TYPE_BUY if direction == 1 else self.mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": int(self.cfg.deviation),
            "magic": int(self.cfg.magic),
            "comment": comment[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(info),
        }
        return self._send(request)

    def close_position(self, position, comment="") -> OrderResult:
        info = self._raw_info(position.symbol)
        bid, ask = self.quote(position.symbol)
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": float(position.volume),
            "type": self.mt5.ORDER_TYPE_SELL if position.direction == 1 else self.mt5.ORDER_TYPE_BUY,
            "position": int(position.ticket),
            "price": bid if position.direction == 1 else ask,
            "deviation": int(self.cfg.deviation),
            "magic": int(self.cfg.magic),
            "comment": (comment or "close")[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(info),
        }
        return self._send(request)

    def modify_position(self, position, sl, tp) -> OrderResult:
        info = self._raw_info(position.symbol)
        bid, ask = self.quote(position.symbol)
        ref = bid if position.direction == 1 else ask
        min_dist = (float(getattr(info, "trade_stops_level", 0) or 0) + 1) * info.point
        if sl and (ref - sl) * position.direction < min_dist:
            return OrderResult(False, message="neuer Stop-Loss läge zu nah am Kurs – übersprungen")
        request = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": int(position.ticket),
            "sl": self._norm(sl, info),
            "tp": self._norm(tp, info),
            "magic": int(self.cfg.magic),
        }
        return self._send(request)

    def _entry_of(self, position_id: int, fallback: datetime) -> tuple[float, datetime]:
        """Einstiegspreis und -zeit einer Position aus ihrem Eröffnungs-Deal."""
        try:
            deals = self.mt5.history_deals_get(position=int(position_id)) or []
        except Exception:
            deals = []
        for d in deals:
            if d.entry == self.mt5.DEAL_ENTRY_IN:
                return float(d.price), datetime.fromtimestamp(d.time, tz=timezone.utc).replace(tzinfo=None)
        return 0.0, fallback

    def pop_closed_trades(self) -> list[ClosedTrade]:
        now = datetime.now(timezone.utc)
        # Deal-Zeiten sind Serverzeit (oft UTC+2/+3) – daher großzügiges Fenster; doppelte
        # Einträge verhindert die gespeicherte Ticket-Liste.
        deals = self.mt5.history_deals_get(now - timedelta(days=3), now + timedelta(days=2))
        own = sorted((d for d in deals or [] if d.magic == self.cfg.magic and d.entry == self.mt5.DEAL_ENTRY_OUT),
                     key=lambda d: (d.time, d.ticket))
        seen = set(self.journal.get("seen_deals", []))
        first_run = "seen_deals" not in self.journal
        out = []
        for d in own:
            if d.ticket in seen:
                continue
            seen.add(d.ticket)
            if first_run:
                continue  # beim allerersten Start nur den Bestand merken, nichts nachtragen
            when = datetime.fromtimestamp(d.time, tz=timezone.utc).replace(tzinfo=None)
            # Ein schließender Kauf-Deal beendet eine Short-Position und umgekehrt
            direction = -1 if d.type == self.mt5.DEAL_TYPE_BUY else 1
            open_price, open_time = self._entry_of(d.position_id, when)
            out.append(ClosedTrade(
                ticket=int(d.position_id), symbol=d.symbol, direction=direction, volume=float(d.volume),
                open_price=open_price, close_price=float(d.price), open_time=open_time, close_time=when,
                profit=float(d.profit + d.commission + d.swap), reason=d.comment or "", comment=d.comment or "",
            ))
        self.journal["seen_deals"] = sorted(seen)[-2000:]
        return out
