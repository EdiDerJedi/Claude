"""Walk-Forward-Optimierung der Strategieparameter.

Ablauf pro Strategie:
1. Zufallssuche im Parameterraum auf dem älteren Teil der Daten (In-Sample).
2. Die besten Kandidaten werden auf den neuesten Daten getestet (Out-of-Sample),
   die sie vorher nie gesehen haben.
3. Neue Parameter werden nur übernommen, wenn sie Out-of-Sample klar besser
   sind als die aktuellen. Das schützt vor Überanpassung (Overfitting).
"""

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..metrics import count_trades, sharpe, strategy_returns
from ..strategies.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class Evaluation:
    params: dict
    is_sharpe: float
    oos_sharpe: float
    is_trades: int
    oos_trades: int


@dataclass
class OptimizationResult:
    strategy: str
    current: Evaluation
    best: Evaluation | None
    adopted: bool
    tested: int = 0
    notes: list = field(default_factory=list)

    @property
    def params(self) -> dict:
        return self.best.params if self.adopted and self.best else self.current.params


def evaluate(strategy: Strategy, params: dict, df: pd.DataFrame, cost_rate: float, split: int, bpy: float) -> Evaluation:
    pos = strategy.with_params(params).generate(df)
    rets = strategy_returns(df["close"], pos, cost_rate)
    return Evaluation(
        params=params,
        is_sharpe=sharpe(rets.iloc[:split], bpy),
        oos_sharpe=sharpe(rets.iloc[split:], bpy),
        is_trades=count_trades(pos.iloc[:split]),
        oos_trades=count_trades(pos.iloc[split:]),
    )


def optimize_strategy(
    strategy: Strategy,
    df: pd.DataFrame,
    cost_rate: float,
    bars_per_year: float,
    n_trials: int = 40,
    min_improvement: float = 0.1,
    rng: np.random.Generator | None = None,
    oos_fraction: float = 0.3,
    top_k: int = 5,
    min_trades: int = 5,
) -> OptimizationResult:
    rng = rng or np.random.default_rng()
    split = int(len(df) * (1.0 - oos_fraction))
    current = evaluate(strategy, strategy.params, df, cost_rate, split, bars_per_year)

    candidates = []
    for _ in range(n_trials):
        params = strategy.sample_params(rng)
        if not strategy.is_valid(params):
            continue
        ev = evaluate(strategy, params, df, cost_rate, split, bars_per_year)
        if ev.is_trades >= min_trades and ev.is_sharpe > 0:
            candidates.append(ev)

    result = OptimizationResult(strategy.name, current, None, False, tested=len(candidates))
    if not candidates:
        result.notes.append("kein Kandidat mit positiver In-Sample-Sharpe")
        return result

    top = sorted(candidates, key=lambda e: e.is_sharpe, reverse=True)[:top_k]
    best = max(top, key=lambda e: e.oos_sharpe)
    result.best = best
    if best.oos_trades < 1:
        result.notes.append("bester Kandidat handelt Out-of-Sample nicht")
    elif best.oos_sharpe <= 0:
        result.notes.append("bester Kandidat ist Out-of-Sample nicht profitabel")
    elif best.oos_sharpe < current.oos_sharpe + min_improvement:
        result.notes.append("Verbesserung zu gering")
    else:
        result.adopted = True
    return result
