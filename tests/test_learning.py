import numpy as np
import pandas as pd

from tradingai.learning import CASH, ModelBundle, StrategySelector, optimize_strategy, train_model
from tradingai.metrics import count_trades, sharpe, strategy_returns
from tradingai.strategies import MLStrategy, TrendStrategy


def _trending_df(n=600, step=0.001):
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    noise = np.random.default_rng(1).normal(0, 0.0003, n)
    close = 1.0 * np.exp(np.cumsum(step + noise))
    return pd.DataFrame({"open": close, "high": close * 1.0005, "low": close * 0.9995, "close": close,
                         "volume": 1.0}, index=idx)


def test_strategy_returns_and_costs():
    idx = pd.date_range("2024-01-01", periods=4, freq="h")
    close = pd.Series([100.0, 101.0, 102.0, 101.0], index=idx)
    pos = pd.Series([1.0, 1.0, 0.0, 0.0], index=idx)
    r = strategy_returns(close, pos, cost_rate=0.001)
    assert r.iloc[0] == -0.001  # Einstieg kostet
    assert abs(r.iloc[1] - 0.01) < 1e-12  # Position aus Bar 0 verdient Bar 1
    assert abs(r.iloc[2] - ((102 / 101 - 1) - 0.001)) < 1e-12  # Ausstieg kostet ebenfalls
    assert r.iloc[3] == 0.0
    assert count_trades(pos) == 1


def test_selector_prefers_winning_strategy():
    df = _trending_df()
    sel = StrategySelector(decay=0.99)
    signals = {"long": pd.Series(1.0, index=df.index), "short": pd.Series(-1.0, index=df.index)}
    sel.update("X", df, signals, cost_rate=0.0)
    w = sel.weights("X", ["long", "short"])
    assert w["long"] > 0.8
    assert w["short"] < 0.01
    score, _ = sel.decide("X", {"long": 1.0, "short": -1.0})
    assert score > 0.5


def test_selector_goes_to_cash_when_everything_loses():
    df = _trending_df()
    sel = StrategySelector()
    sel.update("X", df, {"a": pd.Series(-1.0, index=df.index), "b": pd.Series(-1.0, index=df.index)}, 0.0)
    w = sel.weights("X", ["a", "b"])
    assert w[CASH] > 0.9


def test_selector_is_incremental_and_persistent():
    df = _trending_df()
    sig = {"long": pd.Series(1.0, index=df.index)}
    a = StrategySelector()
    a.update("X", df.iloc[:400], {"long": sig["long"].iloc[:400]}, 0.0)
    assert a.update("X", df.iloc[:400], {"long": sig["long"].iloc[:400]}, 0.0) == 0  # nichts Neues
    assert a.update("X", df, sig, 0.0) == 200
    b = StrategySelector().load(a.to_dict())
    assert abs(b.scores("X", ["long"])["long"] - a.scores("X", ["long"])["long"]) < 1e-12


def test_selector_adapts_to_regime_change():
    up = _trending_df(600, 0.001)
    down_close = up["close"].iloc[-1] * np.exp(np.cumsum(np.full(600, -0.001)))
    idx = pd.date_range(up.index[-1] + pd.Timedelta(hours=1), periods=600, freq="h")
    down = pd.DataFrame({"open": down_close, "high": down_close, "low": down_close, "close": down_close,
                         "volume": 1.0}, index=idx)
    df = pd.concat([up, down])
    sig = {"long": pd.Series(1.0, index=df.index), "short": pd.Series(-1.0, index=df.index)}
    sel = StrategySelector(decay=0.99)
    sel.update("X", df.iloc[:600], {k: v.iloc[:600] for k, v in sig.items()}, 0.0)
    assert sel.weights("X", ["long", "short"])["long"] > 0.8
    sel.update("X", df, sig, 0.0)
    assert sel.weights("X", ["long", "short"])["short"] > 0.8


def test_optimizer_only_adopts_better_params(ohlc):
    res = optimize_strategy(TrendStrategy(), ohlc, 5e-5, 6240, n_trials=15, rng=np.random.default_rng(0))
    if res.adopted:
        assert res.best.oos_sharpe >= res.current.oos_sharpe + 0.1
        assert res.best.oos_sharpe > 0
    else:
        assert res.notes


def test_train_model_reports_validation_metrics():
    from tradingai.data.market_data import synthetic_ohlc

    df = synthetic_ohlc(2500, "H1", seed=11)
    res = train_model(df, None, horizon=5, threshold=0.52, cost_rate=0.0, bars_per_year=6240, min_samples=300)
    for key in ("samples", "val_accuracy", "val_sharpe", "val_trades"):
        assert key in res.metrics
    assert res.accepted == (res.bundle is not None)
    if res.accepted:
        assert res.metrics["val_sharpe"] > 0 and res.metrics["val_accuracy"] > 0.5
        assert res.bundle.trained_until == df.index[-1]


def test_train_model_rejects_too_little_data(ohlc):
    res = train_model(ohlc.iloc[:300], None, 5, 0.55, 0.0, 6240)
    assert not res.accepted and "zu wenig" in res.reason


class _AlwaysUp:
    def predict_proba(self, X):
        return np.column_stack([np.full(len(X), 0.2), np.full(len(X), 0.8)])


def test_ml_strategy_masks_training_period(ohlc):
    bundle = ModelBundle(_AlwaysUp(), ["ret_1"], trained_until=ohlc.index[999], horizon=5, threshold=0.55)
    sig = MLStrategy(bundle).generate(ohlc)
    assert (sig.iloc[:1000] == 0).all()  # keine Signale auf Daten, die das Modell schon kennt
    assert (sig.iloc[1000:] == 1.0).all()
    assert (MLStrategy(None).generate(ohlc) == 0).all()
