"""Ausbruch: Donchian-Kanal (Turtle-Prinzip)."""

import numpy as np
import pandas as pd

from ..indicators import rolling_np, shift_np
from .base import Strategy


class BreakoutStrategy(Strategy):
    name = "breakout"
    default_params = {"entry_n": 55, "exit_n": 20}
    param_space = {"entry_n": (10, 120), "exit_n": (5, 60)}

    def is_valid(self, params: dict) -> bool:
        return params["exit_n"] < params["entry_n"]

    def generate(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        entry_n, exit_n = int(p["entry_n"]), int(p["exit_n"])
        # Kanal der VORHERIGEN Bars, damit der Ausbruch der aktuellen Bar erkannt wird
        high, low = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
        hi = shift_np(rolling_np(high, entry_n, "max"))
        lo = shift_np(rolling_np(low, entry_n, "min"))
        xhi = shift_np(rolling_np(high, exit_n, "max"))
        xlo = shift_np(rolling_np(low, exit_n, "min"))
        close = df["close"].to_numpy(dtype=float)
        out = np.zeros(len(close))
        pos = 0.0
        for i in range(len(close)):
            c = close[i]
            if np.isnan(hi[i]) or np.isnan(xlo[i]):
                pos = 0.0
            elif pos <= 0 and c > hi[i]:
                pos = 1.0
            elif pos >= 0 and c < lo[i]:
                pos = -1.0
            elif pos == 1.0 and c < xlo[i]:
                pos = 0.0
            elif pos == -1.0 and c > xhi[i]:
                pos = 0.0
            out[i] = pos
        return self._series(out, df.index)
