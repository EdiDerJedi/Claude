"""Training des Machine-Learning-Modells mit zeitlicher Validierung."""

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from ..features import WARMUP_FEATURE, build_features
from ..metrics import count_trades, sharpe, strategy_returns

log = logging.getLogger(__name__)


@dataclass
class ModelBundle:
    model: object
    feature_cols: list
    trained_until: pd.Timestamp
    horizon: int
    threshold: float
    metrics: dict = field(default_factory=dict)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        Xc = X.reindex(columns=self.feature_cols)
        return self.model.predict_proba(Xc.to_numpy(dtype=float))[:, 1]


@dataclass
class TrainResult:
    bundle: ModelBundle | None
    metrics: dict
    accepted: bool
    reason: str = ""


def make_model(random_state: int = 0) -> HistGradientBoostingClassifier:
    # Bewusst kleines, stark regularisiertes Modell: Finanzdaten sind sehr verrauscht.
    return HistGradientBoostingClassifier(
        max_iter=200,
        learning_rate=0.05,
        max_depth=3,
        min_samples_leaf=50,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=random_state,
    )


def train_model(
    df: pd.DataFrame,
    context: dict | None,
    horizon: int,
    threshold: float,
    cost_rate: float,
    bars_per_year: float,
    val_fraction: float = 0.25,
    min_samples: int = 500,
) -> TrainResult:
    X = build_features(df, context)
    close = df["close"]
    fwd = np.log(close.shift(-horizon) / close)
    core = X[WARMUP_FEATURE].notna()
    labeled = core & fwd.notna()
    Xl, fl = X[labeled], fwd[labeled]
    if len(Xl) < min_samples:
        return TrainResult(None, {"samples": len(Xl)}, False, f"zu wenig Daten ({len(Xl)} < {min_samples})")

    # Winzige Bewegungen sind reines Rauschen -> nicht zum Lernen verwenden
    vol = X.loc[labeled, "vol_20"] * np.sqrt(horizon)
    informative = ((fl.abs() / vol) >= 0.1).to_numpy()
    y = (fl > 0).astype(int)

    split = int(len(Xl) * (1.0 - val_fraction))
    train_end = split - horizon  # Lücke, damit sich Labels nicht überlappen (Purging)
    if train_end < min_samples // 2:
        return TrainResult(None, {"samples": len(Xl)}, False, "zu wenig Trainingsdaten nach Split")

    model = make_model()
    fit_rows = np.zeros(len(Xl), dtype=bool)
    fit_rows[:train_end] = True
    fit_rows &= informative
    model.fit(Xl[fit_rows].to_numpy(dtype=float), y[fit_rows])

    # Validierung auf den neuesten, ungesehenen Daten
    val_start = Xl.index[split]
    Xv = X.loc[val_start:]
    valid_rows = Xv[WARMUP_FEATURE].notna().to_numpy()
    proba = np.full(len(Xv), 0.5)
    proba[valid_rows] = model.predict_proba(Xv[valid_rows].to_numpy(dtype=float))[:, 1]
    pos = pd.Series(np.where(proba > threshold, 1.0, np.where(proba < 1.0 - threshold, -1.0, 0.0)), index=Xv.index)
    rets = strategy_returns(close.loc[val_start:], pos, cost_rate)

    yv = y.iloc[split:]
    pv = pd.Series(proba, index=Xv.index).reindex(yv.index)
    metrics = {
        "samples": int(len(Xl)),
        "val_accuracy": float(((pv > 0.5).astype(int) == yv).mean()),
        "val_sharpe": sharpe(rets, bars_per_year),
        "val_trades": count_trades(pos),
        "val_return": float((1 + rets).prod() - 1),
    }

    if metrics["val_trades"] < 3:
        return TrainResult(None, metrics, False, "Modell handelt in der Validierung kaum")
    if metrics["val_accuracy"] <= 0.5 or metrics["val_sharpe"] <= 0:
        return TrainResult(None, metrics, False, "Modell ist in der Validierung nicht besser als Zufall")

    # Validierung bestanden -> mit allen Daten neu trainieren
    final = make_model()
    final.fit(Xl[informative].to_numpy(dtype=float), y[informative])
    bundle = ModelBundle(final, list(X.columns), df.index[-1], horizon, threshold, metrics)
    return TrainResult(bundle, metrics, True, "")
