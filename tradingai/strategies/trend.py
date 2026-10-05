"""Trendfolge: EMA-Kreuzung mit ADX-Filter."""

import numpy as np
import pandas as pd

from ..indicators import adx_np, ema_np
from .base import Strategy


class TrendStrategy(Strategy):
    name = "trend"
    default_params = {"fast": 20, "slow": 60, "adx_min": 20.0}
    param_space = {"fast": (5, 50), "slow": (20, 200), "adx_min": (0.0, 35.0)}

    def is_valid(self, params: dict) -> bool:
        return params["fast"] * 1.5 <= params["slow"]

    def generate(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        c = df["close"].to_numpy(dtype=float)
        diff = ema_np(c, int(p["fast"])) - ema_np(c, int(p["slow"]))
        strength = adx_np(df["high"], df["low"], c, 14)
        with np.errstate(invalid="ignore"):
            pos = np.where(strength >= float(p["adx_min"]), np.sign(diff), 0.0)
        return self._series(pos, df.index)
