"""Backtest des kompletten Systems inkl. Lernen (Walk-Forward, ohne Blick in die Zukunft)."""

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .brokers.sim import BacktestBroker
from .config import Config
from .data.symbols import default_symbol_info
from .engine import TradingEngine
from .metrics import max_drawdown, sharpe
from .state import StateStore
from .timeframes import bars_per_year

log = logging.getLogger(__name__)


@dataclass
class BacktestReport:
    equity: pd.Series
    trades: list
    stats: dict
    per_symbol: dict = field(default_factory=dict)
    benchmark: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.trades])

    def format(self) -> str:
        s = self.stats
        lines = [
            "=" * 60,
            "BACKTEST-ERGEBNIS",
            "=" * 60,
            f"Zeitraum:          {s['start']}  ->  {s['end']}",
            f"Startkapital:      {s['initial']:,.2f}",
            f"Endkapital:        {s['final']:,.2f}",
            f"Rendite:           {s['return']:+.2%}",
            f"Max. Drawdown:     {s['max_drawdown']:.2%}",
            f"Sharpe (annual.):  {s['sharpe']:.2f}",
            f"Trades:            {s['trades']}",
            f"Trefferquote:      {s['win_rate']:.1%}",
            f"Profit-Faktor:     {s['profit_factor']:.2f}",
            f"Ø Gewinn/Verlust:  {s['avg_win']:,.2f} / {s['avg_loss']:,.2f}",
        ]
        risk = self.state.get("risk", {})
        lines.append("Not-Aus:           " + (f"AUSGELÖST – {risk.get('kill_reason')}" if risk.get("killed")
                                              else "nicht ausgelöst"))
        lines.append("-" * 60)
        for sym, ps in self.per_symbol.items():
            bh = self.benchmark.get(sym, float("nan"))
            lines.append(f"{sym:<10} Trades {ps['trades']:>4} | Gewinn {ps['profit']:>11,.2f} | "
                         f"Trefferquote {ps['win_rate']:.0%} | Buy&Hold {bh:+.1%}")
        lines.append("=" * 60)
        return "\n".join(lines)


def _trade_stats(trades) -> dict:
    profits = np.array([t.profit for t in trades], dtype=float)
    wins, losses = profits[profits > 0], profits[profits <= 0]
    gross_loss = -losses.sum()
    return {
        "trades": int(len(profits)),
        "profit": float(profits.sum()) if len(profits) else 0.0,
        "win_rate": float(len(wins) / len(profits)) if len(profits) else 0.0,
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else (float("inf") if len(wins) else 0.0),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
    }


def run_backtest(cfg: Config, data: dict, context_provider=None, warmup: int | None = None,
                 learn: bool | None = None, seed: int = 0) -> BacktestReport:
    learn = cfg.learning.enabled if learn is None else learn
    if warmup is None:
        warmup = max(cfg.history_bars, cfg.learning.min_train_bars if learn else 0)
    for sym, df in data.items():
        if len(df) <= warmup + 10:
            raise ValueError(f"{sym}: {len(df)} Bars sind zu wenig für {warmup} Bars Aufwärmphase")

    infos = {s: default_symbol_info(s, float(df["close"].iloc[-1]), cfg.paper.symbol_specs) for s, df in data.items()}
    start_time = max(df.index[warmup - 1] for df in data.values())
    timeline = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in data.values()])))
    start = int(timeline.searchsorted(start_time))

    cfg.symbols = list(data)
    broker = BacktestBroker(data, infos, cfg.paper.initial_balance, start=start)
    store = StateStore(None)
    engine = TradingEngine(cfg, broker, store, context_provider=context_provider, learn=learn,
                           raise_errors=True, rng_seed=seed)

    total = len(timeline) - start
    times, values = [], []
    next_report = 0.1
    while True:
        engine.step()
        times.append(broker.now())
        values.append(broker.account().equity)
        done = (broker.i - start + 1) / total
        if done >= next_report:
            log.info("Backtest %3.0f%% | Kapital %.2f", done * 100, values[-1])
            next_report += 0.1
        if not broker.has_next():
            break
        broker.advance()

    for p in broker.positions():
        broker.close_position(p, "Backtest-Ende")
    equity = pd.Series(values, index=pd.DatetimeIndex(times), name="equity")
    equity.iloc[-1] = broker.account().equity
    trades = list(broker.sim.closed)

    rets = equity.pct_change().dropna()
    stats = {
        "start": str(equity.index[0]),
        "end": str(equity.index[-1]),
        "initial": float(cfg.paper.initial_balance),
        "final": float(equity.iloc[-1]),
        "return": float(equity.iloc[-1] / cfg.paper.initial_balance - 1),
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe(rets, bars_per_year(cfg.timeframe)),
        **_trade_stats(trades),
    }
    per_symbol = {s: _trade_stats([t for t in trades if t.symbol == s]) for s in data}
    benchmark = {}
    for s, df in data.items():
        seg = df.loc[df.index >= start_time, "close"]
        benchmark[s] = float(seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 1 else 0.0
    return BacktestReport(equity, trades, stats, per_symbol, benchmark, store.data)
