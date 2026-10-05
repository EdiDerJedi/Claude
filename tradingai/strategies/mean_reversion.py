"""Mean Reversion: Einstieg bei starker Abweichung vom Mittelwert, nur in ruhigen Märkten."""

import numpy as np
import pandas as pd

from ..indicators import adx_np, zscore_np
from .base import Strategy


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    default_params = {"window": 20, "entry_z": 2.0, "exit_z": 0.5, "adx_max": 25.0}
    param_space = {"window": (10, 60), "entry_z": (1.2, 3.0), "exit_z": (0.0, 1.0), "adx_max": (15.0, 50.0)}

    def is_valid(self, params: dict) -> bool:
        return params["exit_z"] < params["entry_z"]

    def generate(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        z = zscore_np(df["close"], int(p["window"]))
        trend = adx_np(df["high"], df["low"], df["close"], 14)
        entry, exit_, adx_max = float(p["entry_z"]), float(p["exit_z"]), float(p["adx_max"])
        out = np.zeros(len(z))
        pos = 0.0
        for i in range(len(z)):
            zi = z[i]
            if np.isnan(zi):
                pos = 0.0
            elif pos == 0.0:
                if not np.isnan(trend[i]) and trend[i] < adx_max:
                    if zi < -entry:
                        pos = 1.0
                    elif zi > entry:
                        pos = -1.0
            elif pos == 1.0 and zi > -exit_:
                pos = 0.0
            elif pos == -1.0 and zi < exit_:
                pos = 0.0
            out[i] = pos
        return self._series(out, df.index)
