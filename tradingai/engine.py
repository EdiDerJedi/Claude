"""Die Trading-Engine: Daten -> Signale -> Lernen -> Entscheidung -> Risiko -> Order.

Dieselbe Engine läuft im Backtest, im Paper-Trading und live mit MetaTrader 5.
"""

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .brokers.base import Broker
from .config import Config
from .data.symbols import calendar_currencies
from .indicators import atr
from .learning import StrategySelector, optimize_strategy, train_model
from .risk import RiskManager
from .state import StateStore
from .strategies import RULE_STRATEGIES, MLStrategy
from .timeframes import bars_per_year

log = logging.getLogger(__name__)

MIN_BARS = 250


class TradingEngine:
    def __init__(self, cfg: Config, broker: Broker, store: StateStore, news=None, calendar=None,
                 context_provider=None, learn: bool | None = None, raise_errors: bool = False,
                 rng_seed: int | None = None, on_phase=None):
        self.cfg = cfg
        self.on_phase = on_phase  # Rückmeldung für das Dashboard, was der Bot gerade tut
        self.broker = broker
        self.store = store
        self.news = news
        self.calendar = calendar
        self.context_provider = context_provider
        self.context = context_provider.data if context_provider is not None else {}
        self.learn = cfg.learning.enabled if learn is None else learn
        self.raise_errors = raise_errors
        self.bpy = bars_per_year(cfg.timeframe)
        # Woher die Kurse kommen. Wechselt die Quelle (z.B. Paper/Yahoo in UTC -> MT5 in
        # Serverzeit), wird sofort neu gelernt statt mit fremden Daten weiterzumachen.
        self.source = "mt5" if cfg.mode == "live" else f"paper:{cfg.paper.data_source}"
        store.data.setdefault("learn_source", {})
        self.rng = np.random.default_rng(rng_seed)
        lc = cfg.learning
        self.selector = StrategySelector(decay=lc.decay, temperature=lc.temperature).load(store.data.get("selector"))
        self.risk = RiskManager(cfg.risk, store.data["risk"])
        self.strategies = {s: self._build_strategies(s) for s in cfg.symbols}

    def _build_strategies(self, symbol: str) -> dict:
        out = {name: cls(self.store.params(symbol, name)) for name, cls in RULE_STRATEGIES.items()}
        if self.cfg.learning.ml_enabled:
            out["ml"] = MLStrategy(self.store.load_model(symbol), self.context)
        return out

    # ================================================================ Hauptschleife
    def step(self) -> int:
        """Ein Durchlauf. Gibt die Anzahl Symbole zurück, für die eine neue Kerze verarbeitet wurde."""
        now = self.broker.now()
        self.risk.update_equity(self.broker.account().equity, now)
        self.store.append_trades(self.broker.pop_closed_trades())
        if self.risk.killed:
            self._flatten_all("Not-Aus")
        self._update_internet()

        processed = 0
        for symbol in self.cfg.symbols:
            try:
                processed += bool(self._process_symbol(symbol))
            except Exception:
                if self.raise_errors:
                    raise
                log.exception("%s: Fehler bei der Verarbeitung", symbol)

        self.store.append_trades(self.broker.pop_closed_trades())
        self.store.data["selector"] = self.selector.to_dict()
        self.store.save()
        return processed

    def _phase(self, text: str) -> None:
        if self.on_phase is not None:
            self.on_phase(text)

    def _update_internet(self) -> None:
        for name, source in (("Kontext", self.context_provider), ("News", self.news), ("Kalender", self.calendar)):
            if source is None:
                continue
            try:
                source.update()
            except Exception as exc:
                log.warning("%s-Aktualisierung fehlgeschlagen: %s", name, exc)

    def _process_symbol(self, symbol: str) -> bool:
        self._phase(f"prüft {symbol}")
        df = self.broker.get_rates(symbol, self.cfg.timeframe, self.cfg.history_bars)
        if len(df) < MIN_BARS:
            log.warning("%s: nur %d Bars verfügbar (mind. %d nötig)", symbol, len(df), MIN_BARS)
            return False
        bar_time = df.index[-1]
        self._check_source(symbol)
        last = self.store.get_time("last_bar", symbol)
        if last is not None and bar_time <= last:
            return False  # keine neue Bar -> nichts zu tun

        info = self.broker.symbol_info(symbol)
        cost = self._cost_rate(info, df)
        signals = {n: s.generate(df) for n, s in self.strategies[symbol].items()}

        # 1) Lernen aus dem Ergebnis der letzten Bar(s)
        self.selector.update(symbol, df, signals, cost)

        # 2) Periodisch: Parameter optimieren und ML-Modell neu trainieren
        self._phase(f"lernt / entscheidet {symbol}")
        if self.learn and self._maybe_learn(symbol, bar_time, cost):
            signals = {n: s.generate(df) for n, s in self.strategies[symbol].items()}

        # 3) Entscheidung per gewichtetem Konsens
        current = {n: float(sig.iloc[-1]) for n, sig in signals.items()}
        score, weights = self.selector.decide(symbol, current)
        action = self._execute(symbol, score, df, info)
        self._trail(symbol, df)

        self.store.set_time("last_bar", symbol, bar_time)
        self.store.data["decisions"][symbol] = {
            "time": str(bar_time),
            "score": round(score, 4),
            "signals": current,
            "weights": {k: round(v, 4) for k, v in weights.items()},
            "action": action,
        }
        top = ", ".join(f"{k} {v:.0%}" for k, v in sorted(weights.items(), key=lambda x: -x[1])[:3])
        log.info("%s %s | Konsens %+.2f | Gewichte: %s | %s", symbol, bar_time, score, top, action)
        return True

    @staticmethod
    def _cost_rate(info, df: pd.DataFrame) -> float:
        price = float(df["close"].iloc[-1])
        return max(info.spread_price / (2.0 * price), 1e-5) if price > 0 else 1e-4

    # ===================================================================== Lernen
    def _due(self, key: str, symbol: str, bar_time, hours: float) -> bool:
        last = self.store.get_time(key, symbol)
        return last is None or bar_time - last >= timedelta(hours=hours)

    def _check_source(self, symbol: str) -> None:
        sources = self.store.data["learn_source"]
        known = sources.get(symbol)
        if known is not None and known != self.source:
            log.info("%s: Datenquelle gewechselt (%s -> %s) – lerne sofort neu mit den neuen Daten",
                     symbol, known, self.source)
            for key in ("last_bar", "last_optimize", "last_retrain"):
                self.store.data[key].pop(symbol, None)
        sources[symbol] = self.source

    def _maybe_learn(self, symbol: str, bar_time, cost: float, force: bool = False) -> bool:
        lc = self.cfg.learning
        due_opt = force or self._due("last_optimize", symbol, bar_time, lc.optimize_every_hours)
        due_ml = lc.ml_enabled and (force or self._due("last_retrain", symbol, bar_time, lc.retrain_every_hours))
        if not (due_opt or due_ml):
            return False

        df = self.broker.get_rates(symbol, self.cfg.timeframe, lc.train_bars)
        if len(df) < lc.min_train_bars:
            # Kein Zeitstempel setzen: Sobald mehr Historie da ist (MT5 lädt sie oft erst
            # nach), wird bei der nächsten Kerze erneut gelernt statt erst in einer Woche.
            log.warning("%s: zu wenig Historie zum Lernen (%d < %d Bars) – neuer Versuch bei der nächsten "
                        "Kerze. Tipp: Chart öffnen und mit Pos1 zurückscrollen oder learning.min_train_bars "
                        "senken.", symbol, len(df), lc.min_train_bars)
            return False
        if due_opt:
            self.store.set_time("last_optimize", symbol, bar_time)
        if due_ml:
            self.store.set_time("last_retrain", symbol, bar_time)

        strategies = self.strategies[symbol]
        changed = False
        if due_opt:
            for name in list(strategies):
                if name not in RULE_STRATEGIES:
                    continue
                res = optimize_strategy(strategies[name], df, cost, self.bpy, lc.optimize_trials,
                                        lc.min_improvement, self.rng)
                if res.adopted:
                    strategies[name] = strategies[name].with_params(res.best.params)
                    self.store.set_params(symbol, name, res.best.params)
                    changed = True
                    log.info("%s: %s lernt neue Parameter %s (OOS-Sharpe %.2f -> %.2f)", symbol, name,
                             res.best.params, res.current.oos_sharpe, res.best.oos_sharpe)
                else:
                    log.info("%s: %s behält Parameter (%s)", symbol, name, "; ".join(res.notes) or "-")

        if due_ml:
            res = train_model(df, self.context, lc.ml_horizon, lc.ml_threshold, cost, self.bpy)
            self.store.data["model_metrics"][symbol] = {
                **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.metrics.items()},
                "accepted": res.accepted, "reason": res.reason, "time": str(bar_time),
            }
            if res.accepted:
                self.store.save_model(symbol, res.bundle)
                strategies["ml"] = MLStrategy(res.bundle, self.context)
                changed = True
                log.info("%s: neues ML-Modell aktiv (Validierung: Trefferquote %.1f%%, Sharpe %.2f)", symbol,
                         res.metrics["val_accuracy"] * 100, res.metrics["val_sharpe"])
            else:
                log.info("%s: ML-Modell nicht übernommen – %s", symbol, res.reason)
        return changed

    def learn_all(self) -> None:
        """Sofort für alle Symbole optimieren und trainieren (CLI-Befehl `train`)."""
        for symbol in self.cfg.symbols:
            df = self.broker.get_rates(symbol, self.cfg.timeframe, self.cfg.learning.train_bars)
            if len(df) < MIN_BARS:
                log.warning("%s: keine ausreichenden Daten", symbol)
                continue
            self._check_source(symbol)
            info = self.broker.symbol_info(symbol)
            self._maybe_learn(symbol, df.index[-1], self._cost_rate(info, df), force=True)
        self.store.save()

    # ================================================================== Ausführung
    def _execute(self, symbol: str, score: float, df: pd.DataFrame, info) -> str:
        lc = self.cfg.learning
        positions = self.broker.positions(symbol)
        current = positions[0].direction if positions else 0
        target = 1 if score >= lc.entry_threshold else (-1 if score <= -lc.entry_threshold else 0)
        actions = []

        if current != 0 and (current * score < lc.exit_threshold or self.risk.killed):
            for p in positions:
                res = self.broker.close_position(p, "tai exit")
                log.info("%s: Position %s geschlossen (%s)", symbol, p.ticket, "ok" if res.ok else res.message)
            current = 0
            actions.append("schließen")

        if target != 0 and current == 0:
            allowed, reason = self._entry_allowed(symbol, target, info)
            if not allowed:
                actions.append(f"blockiert: {reason}")
            else:
                actions.append(self._open(symbol, target, score, df, info))

        return " + ".join(actions) if actions else "halten"

    def _entry_allowed(self, symbol: str, direction: int, info) -> tuple[bool, str]:
        ok, reason = self.risk.can_open(len(self.broker.positions()))
        if not ok:
            return False, reason
        if not self.risk.spread_ok(info.spread_points):
            return False, f"Spread {info.spread_points:.0f} Points zu hoch"
        utc_now = datetime.now(timezone.utc)
        if self.calendar is not None:
            ev = self.calendar.blocking_event(
                calendar_currencies(symbol, self.cfg.internet.symbol_currencies), utc_now)
            if ev is not None:
                return False, f"Wirtschaftstermin {ev.currency} {ev.title} um {ev.time:%H:%M} UTC"
        if self.news is not None:
            sentiment = self.news.pair_sentiment(symbol)
            if sentiment is not None and direction * sentiment <= -self.cfg.internet.news_block_threshold:
                return False, f"Nachrichtenlage spricht dagegen (Sentiment {sentiment:+.2f})"
        return True, ""

    def _open(self, symbol: str, direction: int, score: float, df: pd.DataFrame, info) -> str:
        a = float(atr(df, self.cfg.risk.atr_period).iloc[-1])
        if not np.isfinite(a) or a <= 0:
            return "kein ATR"
        sl_dist, tp_dist = self.risk.stop_distances(a)
        volume = self.risk.position_size(self.broker.account().equity, sl_dist, info)
        if volume <= 0:
            return "Positionsgröße 0"
        bid, ask = self.broker.quote(symbol)
        price = ask if direction == 1 else bid
        sl = price - direction * sl_dist
        tp = price + direction * tp_dist if tp_dist > 0 else 0.0
        res = self.broker.open_position(symbol, direction, volume, sl, tp, comment=f"tai {score:+.2f}")
        side = "KAUF" if direction == 1 else "VERKAUF"
        if not res.ok:
            log.error("%s: %s fehlgeschlagen: %s", symbol, side, res.message)
            return f"{side} fehlgeschlagen"
        log.info("%s: %s %.2f Lot @ %.5f | SL %.5f | TP %.5f", symbol, side, volume, res.price or price, sl, tp)
        return f"{side} {volume:.2f}"

    def _trail(self, symbol: str, df: pd.DataFrame) -> None:
        mult = self.cfg.risk.trailing_atr_mult
        if mult <= 0:
            return
        a = float(atr(df, self.cfg.risk.atr_period).iloc[-1])
        if not np.isfinite(a) or a <= 0:
            return
        for p in self.broker.positions(symbol):
            bid, ask = self.broker.quote(symbol)
            price = bid if p.direction == 1 else ask
            new_sl = price - p.direction * mult * a
            if p.sl == 0 or (new_sl - p.sl) * p.direction > 0.2 * a:
                self.broker.modify_position(p, new_sl, p.tp)

    def _flatten_all(self, reason: str) -> None:
        for p in self.broker.positions():
            res = self.broker.close_position(p, reason)
            log.warning("%s: Position %s wegen %s geschlossen (%s)", p.symbol, p.ticket, reason,
                        "ok" if res.ok else res.message)

    # ================================================================== Dashboard
    def snapshot(self) -> dict:
        """Momentaufnahme für das Dashboard (wird nach jedem Durchlauf gespeichert)."""
        from datetime import datetime, timezone

        from .status import iso

        acc = self.broker.account()
        risk = self.risk.state
        peak = risk.get("peak_equity") or acc.equity
        day_start = risk.get("day_start_equity") or acc.equity
        rc = self.cfg.risk
        out = {
            "updated_at": iso(datetime.now(timezone.utc)),
            "mode": self.cfg.mode,
            "timeframe": self.cfg.timeframe,
            "symbols": list(self.cfg.symbols),
            "account": {
                "balance": acc.balance, "equity": acc.equity, "currency": acc.currency,
                "leverage": acc.leverage, "is_demo": acc.is_demo, "free_margin": acc.free_margin,
                "login": acc.login, "server": acc.server,
            },
            "positions": [
                {"ticket": p.ticket, "symbol": p.symbol, "direction": p.direction, "volume": p.volume,
                 "open_price": p.open_price, "sl": p.sl, "tp": p.tp, "profit": p.profit,
                 "open_time": str(p.open_time)}
                for p in self.broker.positions()
            ],
            "risk": {
                "day_start_equity": day_start,
                "daily_pl": acc.equity - day_start,
                "daily_loss_pct": self.risk.daily_loss(),
                "peak_equity": peak,
                "drawdown_pct": max(0.0, 1.0 - acc.equity / peak) if peak else 0.0,
                "killed": bool(risk.get("killed")),
                "kill_reason": risk.get("kill_reason", ""),
                "limits": {
                    "risk_per_trade": rc.risk_per_trade, "max_daily_loss": rc.max_daily_loss,
                    "max_drawdown": rc.max_drawdown, "max_open_positions": rc.max_open_positions,
                },
            },
            "learning": {
                "entry_threshold": self.cfg.learning.entry_threshold,
                "exit_threshold": self.cfg.learning.exit_threshold,
            },
            "news": {},
            "headlines": [],
            "calendar": [],
        }
        if hasattr(self.broker, "terminal_flags"):
            try:
                out["terminal"] = self.broker.terminal_flags()
            except Exception:
                out["terminal"] = {}
        if self.news is not None:
            out["news"] = {s: self.news.pair_sentiment(s) for s in self.cfg.symbols}
            items = sorted(self.news.items, key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc),
                           reverse=True)
            out["headlines"] = [
                {"title": i.title, "published": iso(i.published) if i.published else None, "source": i.source}
                for i in items[:8]
            ]
        if self.calendar is not None:
            currencies = sorted({c for s in self.cfg.symbols
                                 for c in calendar_currencies(s, self.cfg.internet.symbol_currencies)})
            events = [e for e in self.calendar.upcoming(currencies, hours=48)
                      if e.impact.lower() in self.calendar.impacts]
            out["calendar"] = [
                {"time": iso(e.time), "currency": e.currency, "title": e.title, "impact": e.impact}
                for e in events[:12]
            ]
        return out
