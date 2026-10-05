"""Hilfsfunktionen rund um Zeiteinheiten (M1 ... D1)."""

TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}

# Yahoo-Intervall und maximal verfügbarer Zeitraum je Timeframe.
# H4 gibt es bei Yahoo nicht – es wird aus 1h-Daten zusammengesetzt.
YAHOO_INTERVALS = {
    "M1": ("1m", "7d"),
    "M5": ("5m", "60d"),
    "M15": ("15m", "60d"),
    "M30": ("30m", "60d"),
    "H1": ("1h", "730d"),
    "H4": ("1h", "730d"),
    "D1": ("1d", "10y"),
}

TRADING_DAYS_PER_YEAR = 260


def validate_timeframe(tf: str) -> str:
    tf = tf.upper()
    if tf not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unbekannter Timeframe '{tf}'. Erlaubt: {', '.join(TIMEFRAME_MINUTES)}")
    return tf


def minutes(tf: str) -> int:
    return TIMEFRAME_MINUTES[validate_timeframe(tf)]


def bars_per_year(tf: str) -> float:
    """Ungefähre Anzahl Bars pro Jahr (für annualisierte Kennzahlen)."""
    m = minutes(tf)
    if m >= 1440:
        return float(TRADING_DAYS_PER_YEAR)
    return TRADING_DAYS_PER_YEAR * 24 * 60 / m
