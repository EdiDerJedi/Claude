"""Machine-Learning-Strategie: Gradient-Boosting-Modell sagt die Richtung voraus."""

import numpy as np
import pandas as pd

from ..features import WARMUP_FEATURE, build_features
from .base import Strategy


class MLStrategy(Strategy):
    name = "ml"

    def __init__(self, bundle=None, context: dict | None = None):
        super().__init__({})
        self.bundle = bundle
        self.context = context

    def with_params(self, params: dict) -> "MLStrategy":
        return MLStrategy(self.bundle, self.context)

    def generate(self, df: pd.DataFrame) -> pd.Series:
        if self.bundle is None or len(df) == 0:
            return pd.Series(0.0, index=df.index)
        # Signale nur außerhalb der Aufwärmphase und nur auf Daten, die das Modell
        # NICHT im Training gesehen hat – sonst wäre seine Bewertung geschönt.
        unseen = np.asarray(df.index > self.bundle.trained_until)
        pos = np.zeros(len(df))
        if not unseen.any():
            return self._series(pos, df.index)
        X = build_features(df, self.context)
        rows = unseen & X[WARMUP_FEATURE].notna().to_numpy()
        if rows.any():
            proba = self.bundle.predict_proba(X[rows])
            thr = self.bundle.threshold
            pos[rows] = np.where(proba > thr, 1.0, np.where(proba < 1.0 - thr, -1.0, 0.0))
        return self._series(pos, df.index)
