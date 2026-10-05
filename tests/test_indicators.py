import numpy as np
import pandas as pd

from tradingai.features import build_features
from tradingai.indicators import adx, atr, ema, rsi, zscore


def test_ema_matches_pandas(ohlc):
    c = ohlc["close"]
    expected = c.ewm(span=20, adjust=False, min_periods=20).mean()
    pd.testing.assert_series_equal(ema(c, 20), expected, check_names=False, atol=1e-12)


def test_rsi_bounds(ohlc):
    r = rsi(ohlc["close"]).dropna()
    assert len(r) > 0
    assert r.between(0, 100).all()


def test_rsi_without_losses_is_100():
    s = pd.Series(np.arange(1.0, 40.0))
    assert rsi(s, 14).iloc[-1] == 100.0


def test_atr_and_adx(ohlc):
    a = atr(ohlc).dropna()
    assert (a > 0).all()
    d = adx(ohlc).dropna()
    assert d.between(0, 100).all()


def test_zscore_centered(ohlc):
    z = zscore(ohlc["close"], 20).dropna()
    assert abs(z.mean()) < 1.0


def test_features_have_no_lookahead(ohlc):
    full = build_features(ohlc)
    cut = 900
    partial = build_features(ohlc.iloc[:cut])
    # Merkmale an Bar t dürfen sich nicht ändern, wenn spätere Bars hinzukommen
    pd.testing.assert_frame_equal(full.iloc[:cut], partial, atol=1e-9)


def test_context_features_use_previous_day_only(ohlc):
    days = pd.date_range(ohlc.index[0].normalize() - pd.Timedelta(days=100), ohlc.index[-1].normalize(), freq="D")
    vix = pd.Series(np.linspace(10, 30, len(days)), index=days)
    f = build_features(ohlc, {"VIX": vix})
    t = ohlc.index[700]
    day = t.normalize()
    expected = np.log(vix.loc[day - pd.Timedelta(days=1)]) - np.log(vix.loc[day - pd.Timedelta(days=2)])
    assert abs(f.loc[t, "ctx_VIX_ret1"] - expected) < 1e-12
