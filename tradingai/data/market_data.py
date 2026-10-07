"""Kursdaten aus dem Internet (Yahoo Finance), aus CSV-Dateien oder synthetisch."""

import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..timeframes import YAHOO_INTERVALS, minutes, validate_timeframe

log = logging.getLogger(__name__)

OHLC = ["open", "high", "low", "close"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Einheitliches Format: tz-naiver UTC-Index, Spalten open/high/low/close/volume."""
    df = df.copy()
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df.index = idx
    df.index.name = "time"
    if "volume" not in df.columns:
        df["volume"] = df["tick_volume"] if "tick_volume" in df.columns else 0.0
    df = df[OHLC + ["volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=OHLC)


def _drop_unfinished(df: pd.DataFrame, timeframe: str, now: datetime | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    bar_end = df.index[-1] + pd.Timedelta(minutes=minutes(timeframe))
    return df.iloc[:-1] if bar_end > now else df


def fetch_yahoo(ticker: str, timeframe: str) -> pd.DataFrame:
    """Historische Kurse von Yahoo Finance (kostenlos, leicht verzögert)."""
    import yfinance as yf

    timeframe = validate_timeframe(timeframe)
    interval, period = YAHOO_INTERVALS[timeframe]
    raw = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"Yahoo lieferte keine Daten für {ticker} ({interval})")
    df = _normalize(raw)
    if timeframe == "H4":
        df = (
            df.resample("4h", label="left", closed="left")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna(subset=OHLC)
        )
    return _drop_unfinished(df, timeframe)


def fetch_yahoo_daily(ticker: str, period: str = "5y") -> pd.Series:
    """Tägliche Schlusskurse (für Kontext-Merkmale wie VIX oder S&P 500)."""
    import yfinance as yf

    raw = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"Yahoo lieferte keine Tagesdaten für {ticker}")
    return _normalize(raw)["close"]


def load_context(tickers: dict) -> dict:
    """Lädt alle Kontextmärkte; fehlende werden mit Warnung übersprungen."""
    out = {}
    for name, ticker in (tickers or {}).items():
        try:
            out[name] = fetch_yahoo_daily(ticker)
        except Exception as exc:  # Netzwerkfehler sollen den Bot nicht stoppen
            log.warning("Kontextdaten %s (%s) nicht verfügbar: %s", name, ticker, exc)
    return out


def load_csv(path: str | Path) -> pd.DataFrame:
    """CSV laden – auch den MT5-Export ("<DATE>\\t<TIME>\\t<OPEN>...") und deutsche Excel-Dateien
    (Semikolon, Dezimalkomma, TT.MM.JJJJ) sowie UTF-16-Dateien."""
    path = Path(path)
    raw = path.read_bytes()[:4]
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    with path.open("r", encoding=encoding) as fh:
        first = fh.readline()
    sep = "\t" if "\t" in first else (";" if first.count(";") > first.count(",") else ",")
    german = sep == ";"
    df = pd.read_csv(path, sep=sep, encoding=encoding, decimal="," if german else ".")
    df.columns = [str(c).strip().strip("<>").lower() for c in df.columns]
    if "date" in df.columns and "time" in df.columns:
        ts = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), dayfirst=german)
    else:
        col = next((c for c in ("time", "datetime", "date", "timestamp") if c in df.columns), None)
        if col is None:
            raise ValueError(f"{path}: keine Zeitspalte gefunden")
        ts = pd.to_datetime(df[col], dayfirst=german)
    df.index = ts
    if "tickvol" in df.columns:
        df["volume"] = df["tickvol"]
    elif "vol" in df.columns and "volume" not in df.columns:
        df["volume"] = df["vol"]
    return _normalize(df)


def synthetic_ohlc(n: int = 5000, timeframe: str = "H1", seed: int = 0, start_price: float = 1.10,
                   start: str = "2022-01-03") -> pd.DataFrame:
    """Synthetische Kurse mit wechselnden Marktphasen (Trend, Seitwärts, Zufall).

    Nützlich zum Testen ohne Internet und um zu prüfen, ob sich das System an
    Regimewechsel anpasst.
    """
    rng = np.random.default_rng(seed)
    freq = pd.Timedelta(minutes=minutes(timeframe))
    index = pd.date_range(start=start, periods=n * 2, freq=freq)
    index = index[index.dayofweek < 5][:n]  # Wochenenden auslassen
    base_vol = 0.0012 * np.sqrt(minutes(timeframe) / 60)

    logp = np.empty(n)
    logp[0] = np.log(start_price)
    regime, left, drift, anchor = 0, 0, 0.0, logp[0]
    for i in range(1, n):
        if left <= 0:
            regime = rng.integers(0, 3)  # 0 Trend, 1 Mean Reversion, 2 Zufall
            left = int(rng.integers(150, 600))
            drift = rng.choice([-1, 1]) * base_vol * rng.uniform(0.08, 0.2)
            anchor = logp[i - 1]
        left -= 1
        hour = index[i].hour
        vol = base_vol * (1.3 if 7 <= hour <= 16 else 0.8)
        shock = rng.standard_t(5) * vol / np.sqrt(5 / 3)
        if regime == 0:
            step = drift + shock
        elif regime == 1:
            step = -0.05 * (logp[i - 1] - anchor) + shock
        else:
            step = shock
        logp[i] = logp[i - 1] + step

    close = np.exp(logp)
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, base_vol * 0.6, size=(2, n))) * close
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    volume = rng.integers(100, 5000, size=n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.DatetimeIndex(index, name="time"),
    )


class ContextProvider:
    """Hält Tagesdaten anderer Märkte aktuell. `data` wird an Ort und Stelle
    aktualisiert, damit alle Strategien automatisch die neuesten Werte sehen."""

    def __init__(self, tickers: dict, refresh_hours: float = 6.0, loader=load_context):
        self.tickers = dict(tickers or {})
        self.refresh_seconds = refresh_hours * 3600
        self.loader = loader
        self.data: dict = {}
        self._last = None

    def update(self, force: bool = False) -> None:
        now = datetime.now(timezone.utc)
        if not self.tickers:
            return
        if not force and self._last and (now - self._last).total_seconds() < self.refresh_seconds:
            return
        self._last = now
        fresh = self.loader(self.tickers)
        self.data.update(fresh)  # bei Fehlern bleiben die alten Werte erhalten
        if fresh:
            log.info("Kontextmärkte aktualisiert: %s", ", ".join(sorted(fresh)))
