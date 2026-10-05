"""Schnelle vektorisierte Bewertung von Positions-Serien (für Lernen und Optimierung)."""

import numpy as np
import pandas as pd


def strategy_returns(close: pd.Series, positions: pd.Series, cost_rate: float) -> pd.Series:
    """Rendite pro Bar einer Strategie.

    positions[t] ist die Zielposition, die am Schluss von Bar t eingenommen wird.
    Sie verdient daher die Rendite von Bar t+1. Jeder Positionswechsel kostet
    cost_rate (halber Spread relativ zum Preis) pro Einheit Positionsänderung.
    """
    pos = positions.reindex(close.index).fillna(0.0).astype(float)
    ret = close.pct_change().fillna(0.0)
    held = pos.shift(1).fillna(0.0)
    turnover = pos.diff().abs()
    if len(turnover):
        turnover.iloc[0] = abs(pos.iloc[0])
    return held * ret - turnover * cost_rate


def sharpe(returns: pd.Series, bars_per_year: float) -> float:
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    std = r.std(ddof=1)
    if not np.isfinite(std) or std < 1e-12:
        return 0.0
    return float(r.mean() / std * np.sqrt(bars_per_year))


def max_drawdown(equity: pd.Series) -> float:
    e = np.asarray(equity, dtype=float)
    if e.size == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    dd = (peak - e) / np.where(peak == 0, 1, peak)
    return float(dd.max())


def count_trades(positions: pd.Series) -> int:
    """Anzahl neuer Einstiege (Wechsel in eine Position ungleich 0)."""
    p = np.asarray(positions.fillna(0.0), dtype=float)
    if p.size == 0:
        return 0
    prev = np.concatenate([[0.0], p[:-1]])
    return int(((p != 0) & (p != prev)).sum())


def summarize(returns: pd.Series, positions: pd.Series, bars_per_year: float) -> dict:
    equity = (1.0 + returns).cumprod()
    return {
        "sharpe": sharpe(returns, bars_per_year),
        "total_return": float(equity.iloc[-1] - 1.0) if len(equity) else 0.0,
        "max_drawdown": max_drawdown(equity),
        "trades": count_trades(positions),
        "exposure": float((positions.fillna(0.0) != 0).mean()) if len(positions) else 0.0,
    }
