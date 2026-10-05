"""Merkmale (Features) für das Machine-Learning-Modell.

Alle Merkmale an Bar t verwenden ausschließlich Daten bis einschließlich Bar t,
damit Backtests und Training keinen Blick in die Zukunft enthalten.
"""

import numpy as np
import pandas as pd

from .indicators import (
    _safe_div,
    adx_np,
    atr_np,
    diff_np,
    ema_np,
    ewm_np,
    rolling_np,
    rsi_np,
    shift_np,
)

# Merkmal, das erst nach der längsten Aufwärmphase gültig ist
WARMUP_FEATURE = "dist_ema200"


def build_features(df: pd.DataFrame, context: dict | None = None) -> pd.DataFrame:
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    f = {}

    with np.errstate(divide="ignore", invalid="ignore"):
        logc = np.log(c)
    logret = diff_np(logc)
    vol20 = rolling_np(logret, 20, "std")
    vol100 = rolling_np(logret, 100, "std")

    for n in (1, 3, 5, 10, 20):
        f[f"ret_{n}"] = _safe_div(logc - shift_np(logc, n), vol20 * np.sqrt(n))
    f["vol_20"] = vol20
    f["vol_ratio"] = _safe_div(vol20, vol100)

    a = atr_np(h, l, c, 14)
    a[a == 0] = np.nan
    f["atr_pct"] = _safe_div(a, c)
    f["rsi_14"] = rsi_np(c, 14) / 100.0 - 0.5
    macd_line = ema_np(c, 12) - ema_np(c, 26)
    f["macd_hist"] = _safe_div(macd_line - ewm_np(macd_line, 2.0 / 10.0, 9), a)
    mid, std = rolling_np(c, 20, "mean"), rolling_np(c, 20, "std")
    f["bb_pos"] = _safe_div(c - (mid - 2 * std), 4 * std) - 0.5
    f["dist_ema20"] = _safe_div(c - ema_np(c, 20), a)
    f["dist_ema50"] = _safe_div(c - ema_np(c, 50), a)
    f["dist_ema200"] = _safe_div(c - ema_np(c, 200), a)
    f["adx_14"] = adx_np(h, l, c, 14) / 100.0
    hh, ll = rolling_np(h, 20, "max"), rolling_np(l, 20, "min")
    f["donchian_pos"] = _safe_div(c - ll, hh - ll) - 0.5
    f["range_atr"] = _safe_div(h - l, a)
    f["body_atr"] = _safe_div(c - o, a)

    if isinstance(df.index, pd.DatetimeIndex):
        hour = (df.index.hour + df.index.minute / 60.0).to_numpy(dtype=float)
        f["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
        f["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
        f["weekday"] = df.index.dayofweek.to_numpy(dtype=float) / 4.0

    out = pd.DataFrame(f, index=df.index)
    if context:
        out = out.join(context_features(context, df.index))
    return out


_DAILY_CACHE: dict = {}


def _daily_context(name: str, series: pd.Series) -> pd.DataFrame:
    key = (name, len(series), series.index[-1], float(series.iloc[-1]))
    cached = _DAILY_CACHE.get(key)
    if cached is not None:
        return cached
    s = series.dropna().astype(float)
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    s.index = idx.normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    daily = pd.DataFrame(index=s.index)
    logs = np.log(s.where(s > 0))
    daily[f"ctx_{name}_ret1"] = logs.diff()
    daily[f"ctx_{name}_ret5"] = logs.diff(5)
    mean = s.rolling(60, min_periods=20).mean()
    std = s.rolling(60, min_periods=20).std()
    daily[f"ctx_{name}_z60"] = (s - mean) / std.replace(0.0, np.nan)
    # Verschiebung um einen Tag: an Tag D nur Werte bis zum Schluss von D-1
    daily = daily.shift(1).replace([np.inf, -np.inf], np.nan)
    if len(_DAILY_CACHE) > 64:
        _DAILY_CACHE.clear()
    _DAILY_CACHE[key] = daily
    return daily


def context_features(context: dict, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Tagesdaten anderer Märkte (z.B. VIX, S&P 500) als Merkmale.

    An Tag D sieht das Modell nur den Schlusskurs von D-1. So entsteht kein
    Look-Ahead, auch wenn die Märkte unterschiedliche Handelszeiten haben.
    """
    cols = {}
    for name, series in context.items():
        if series is None or len(series) < 10:
            continue
        daily = _daily_context(name, series)
        pos = np.searchsorted(daily.index.values, index.values, side="right") - 1
        values = daily.to_numpy(dtype=float)
        for j, col in enumerate(daily.columns):
            v = np.full(len(index), np.nan)
            ok = pos >= 0
            v[ok] = values[pos[ok], j]
            cols[col] = v
    return pd.DataFrame(cols, index=index)
