"""Technische Indikatoren ohne Look-Ahead.

Die Berechnung läuft intern auf numpy-Arrays (die *_np-Funktionen) – das ist
um ein Vielfaches schneller als einzelne pandas-Operationen und macht
Backtests mit Lernen erst praktikabel. Die Funktionen ohne _np-Suffix nehmen
pandas-Objekte entgegen und geben pandas-Serien zurück.
"""

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from scipy.signal import lfilter


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def _ffill(x: np.ndarray) -> np.ndarray:
    mask = np.isnan(x)
    if not mask.any():
        return x
    idx = np.where(~mask, np.arange(len(x)), 0)
    np.maximum.accumulate(idx, out=idx)
    return x[idx]


# ------------------------------------------------------------- numpy-Kern
def ewm_np(x, alpha: float, min_periods: int = 1) -> np.ndarray:
    """Exponentieller Durchschnitt wie pandas ewm(adjust=False)."""
    x = _arr(x)
    out = np.full(len(x), np.nan)
    valid = ~np.isnan(x)
    if not valid.any():
        return out
    i0 = int(np.argmax(valid))
    seg = _ffill(x[i0:].copy())
    out[i0:] = lfilter([alpha], [1.0, alpha - 1.0], seg, zi=[(1.0 - alpha) * seg[0]])[0]
    out[i0:i0 + max(min_periods - 1, 0)] = np.nan
    return out


def ema_np(x, n: int) -> np.ndarray:
    return ewm_np(x, 2.0 / (n + 1.0), n)


def wilder_np(x, n: int) -> np.ndarray:
    return ewm_np(x, 1.0 / n, n)


def rolling_np(x, n: int, how: str) -> np.ndarray:
    """Rollierendes Fenster der Länge n (NaN, solange das Fenster nicht voll ist)."""
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if n < 1 or len(x) < n:
        return out
    w = sliding_window_view(x, n)
    if how == "mean":
        v = w.mean(axis=1)
    elif how == "std":
        v = w.std(axis=1, ddof=1) if n > 1 else np.zeros(len(w))
    elif how == "max":
        v = w.max(axis=1)
    elif how == "min":
        v = w.min(axis=1)
    else:
        raise ValueError(how)
    out[n - 1:] = v
    return out


def shift_np(x, k: int = 1) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


def diff_np(x, k: int = 1) -> np.ndarray:
    return _arr(x) - shift_np(x, k)


def _safe_div(a, b) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.divide(a, b)
    out[~np.isfinite(out)] = np.nan
    return out


def rsi_np(close, n: int = 14) -> np.ndarray:
    delta = diff_np(close)
    gain = wilder_np(np.where(np.isnan(delta), np.nan, np.maximum(delta, 0.0)), n)
    loss = wilder_np(np.where(np.isnan(delta), np.nan, np.maximum(-delta, 0.0)), n)
    rs = _safe_div(gain, loss)
    out = 100.0 - 100.0 / (1.0 + rs)
    out[(loss == 0) & ~np.isnan(gain)] = 100.0  # kein Verlust im Fenster
    return out


def true_range_np(high, low, close) -> np.ndarray:
    h, l, pc = _arr(high), _arr(low), shift_np(close)
    return np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))


def atr_np(high, low, close, n: int = 14) -> np.ndarray:
    return wilder_np(true_range_np(high, low, close), n)


def adx_np(high, low, close, n: int = 14) -> np.ndarray:
    h, l = _arr(high), _arr(low)
    up, down = diff_np(h), -diff_np(l)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm[0] = minus_dm[0] = np.nan
    atr_ = wilder_np(true_range_np(h, l, close), n)
    atr_[atr_ == 0] = np.nan
    plus_di = 100.0 * _safe_div(wilder_np(plus_dm, n), atr_)
    minus_di = 100.0 * _safe_div(wilder_np(minus_dm, n), atr_)
    dx = 100.0 * _safe_div(np.abs(plus_di - minus_di), plus_di + minus_di)
    return wilder_np(dx, n)


def zscore_np(close, n: int) -> np.ndarray:
    c = _arr(close)
    return _safe_div(c - rolling_np(c, n, "mean"), rolling_np(c, n, "std"))


# ------------------------------------------------------- pandas-Schnittstelle
def _s(values, like) -> pd.Series:
    return pd.Series(values, index=like.index)


def ema(series: pd.Series, n: int) -> pd.Series:
    return _s(ema_np(series, n), series)


def sma(series: pd.Series, n: int) -> pd.Series:
    return _s(rolling_np(series, n, "mean"), series)


def wilder(series: pd.Series, n: int) -> pd.Series:
    return _s(wilder_np(series, n), series)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    return _s(rsi_np(close, n), close)


def true_range(df: pd.DataFrame) -> pd.Series:
    return _s(true_range_np(df["high"], df["low"], df["close"]), df)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _s(atr_np(df["high"], df["low"], df["close"], n), df)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _s(adx_np(df["high"], df["low"], df["close"], n), df)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema_np(close, fast) - ema_np(close, slow)
    sig = ewm_np(line, 2.0 / (signal + 1.0), signal)
    return _s(line, close), _s(sig, close), _s(line - sig, close)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = rolling_np(close, n, "mean")
    std = rolling_np(close, n, "std")
    return _s(mid, close), _s(mid + k * std, close), _s(mid - k * std, close)


def zscore(close: pd.Series, n: int) -> pd.Series:
    return _s(zscore_np(close, n), close)


def donchian(df: pd.DataFrame, n: int):
    """Höchstes Hoch / tiefstes Tief der letzten n Bars (inklusive aktueller Bar)."""
    return _s(rolling_np(df["high"], n, "max"), df), _s(rolling_np(df["low"], n, "min"), df)
