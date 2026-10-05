"""Online-Lernen: welche Strategie funktioniert im aktuellen Marktumfeld?

Jede Strategie wird für jedes Symbol "im Schatten" mitgehandelt – auch wenn
ihr Signal gerade nicht ausgeführt wird. Aus den (volatilitäts-normierten)
Schatten-Renditen berechnet der Selektor einen exponentiell gewichteten
t-Wert (Rendite / Schwankung * Wurzel(Anzahl)). Ältere Ergebnisse verlieren
mit dem Faktor `decay` pro Bar an Gewicht, so passt sich die Gewichtung an
neue Marktphasen an.

Die Gewichte entstehen per Softmax. Eine zusätzliche "cash"-Option mit
Score 0 sorgt dafür, dass das System flat bleibt, wenn alle Strategien
schlecht laufen.
"""

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from ..indicators import rolling_np, shift_np

CASH = "cash"


@dataclass
class ArmStats:
    w: float = 0.0  # Summe der Gewichte
    w2: float = 0.0  # Summe der quadrierten Gewichte (für effektive Stichprobengröße)
    s1: float = 0.0  # gewichtete Summe der Rewards
    s2: float = 0.0  # gewichtete Summe der quadrierten Rewards

    def update_many(self, rewards: np.ndarray, decay: float) -> None:
        k = len(rewards)
        if k == 0:
            return
        dk = decay**k
        weights = decay ** np.arange(k - 1, -1, -1, dtype=float)
        self.w = dk * self.w + weights.sum()
        self.w2 = dk * dk * self.w2 + (weights**2).sum()
        self.s1 = dk * self.s1 + (weights * rewards).sum()
        self.s2 = dk * self.s2 + (weights * rewards**2).sum()

    def score(self, min_std: float) -> float:
        if self.w < 1e-9 or self.w2 < 1e-12:
            return 0.0
        mean = self.s1 / self.w
        var = max(self.s2 / self.w - mean * mean, 0.0)
        std = max(math.sqrt(var), min_std)
        n_eff = self.w * self.w / self.w2
        return mean / std * math.sqrt(n_eff)


class StrategySelector:
    def __init__(
        self,
        decay: float = 0.99,
        temperature: float = 1.0,
        min_std: float = 0.25,
        reward_clip: float = 5.0,
        warmup_bars: int = 1000,
    ):
        self.decay = decay
        self.temperature = temperature
        self.min_std = min_std
        self.reward_clip = reward_clip
        self.warmup_bars = warmup_bars
        self.stats: dict[str, dict[str, ArmStats]] = {}
        self.last_time: dict[str, pd.Timestamp] = {}

    # ------------------------------------------------------------------ Lernen
    def update(self, symbol: str, df: pd.DataFrame, signals: dict, cost_rate: float) -> int:
        """Verbucht die Schatten-Rendite aller Bars seit dem letzten Update.

        Gibt die Anzahl neu verarbeiteter Bars zurück.
        """
        n = len(df)
        if n < 2:
            return 0
        last = self.last_time.get(symbol)
        if last is None:
            start = max(1, n - self.warmup_bars)
        else:
            start = max(int(np.searchsorted(df.index.values, np.datetime64(last), side="right")), 1)
        if start >= n:
            return 0
        c = df["close"].to_numpy(dtype=float)
        r = np.zeros(n)
        r[1:] = c[1:] / c[:-1] - 1.0
        sigma = shift_np(rolling_np(r, 100, "std"))  # Volatilität, bekannt VOR der Bar
        arms = self.stats.setdefault(symbol, {})
        for name, sig in signals.items():
            pos = np.nan_to_num(np.asarray(sig, dtype=float))
            held = np.concatenate([[0.0], pos[:-1]])
            turnover = np.abs(np.diff(pos, prepend=0.0))
            raw = held * r - turnover * cost_rate
            with np.errstate(divide="ignore", invalid="ignore"):
                rewards = raw[start:] / sigma[start:]
            rewards = np.clip(np.nan_to_num(rewards, nan=0.0, posinf=0.0, neginf=0.0),
                              -self.reward_clip, self.reward_clip)
            arms.setdefault(name, ArmStats()).update_many(rewards, self.decay)
        self.last_time[symbol] = df.index[-1]
        return len(df) - start

    # --------------------------------------------------------------- Entscheiden
    def scores(self, symbol: str, names) -> dict:
        arms = self.stats.get(symbol, {})
        out = {n: arms[n].score(self.min_std) if n in arms else 0.0 for n in names}
        out[CASH] = 0.0
        return out

    def weights(self, symbol: str, names) -> dict:
        sc = self.scores(symbol, names)
        keys = list(sc)
        x = np.array([sc[k] for k in keys]) / max(self.temperature, 1e-6)
        x = np.exp(x - x.max())
        x /= x.sum()
        return dict(zip(keys, x.tolist()))

    def decide(self, symbol: str, current: dict) -> tuple[float, dict]:
        """Gewichteter Konsens der aktuellen Strategiesignale in [-1, 1]."""
        w = self.weights(symbol, list(current))
        score = sum(w[n] * float(current[n]) for n in current)
        return float(score), w

    # ------------------------------------------------------------- Persistenz
    def to_dict(self) -> dict:
        return {
            "stats": {s: {n: asdict(a) for n, a in arms.items()} for s, arms in self.stats.items()},
            "last_time": {s: t.isoformat() for s, t in self.last_time.items()},
        }

    def load(self, data: dict | None) -> "StrategySelector":
        if not data:
            return self
        self.stats = {s: {n: ArmStats(**a) for n, a in arms.items()} for s, arms in data.get("stats", {}).items()}
        self.last_time = {s: pd.Timestamp(t) for s, t in data.get("last_time", {}).items()}
        return self
