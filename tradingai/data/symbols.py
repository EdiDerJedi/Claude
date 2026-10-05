"""Symbol-Stammdaten für Simulation, Yahoo-Ticker und Währungszuordnung."""

import re

from ..models import SymbolInfo

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNH", "SEK", "NOK", "MXN", "ZAR"}
METALS = {"XAU", "XAG"}

# Näherungswerte für die Simulation (Konto in USD). Bei MT5 kommen echte Werte vom Broker.
DEFAULT_SPECS = {
    "EURUSD": dict(digits=5, contract_size=100_000, tick_value=1.0, spread_points=10),
    "GBPUSD": dict(digits=5, contract_size=100_000, tick_value=1.0, spread_points=14),
    "AUDUSD": dict(digits=5, contract_size=100_000, tick_value=1.0, spread_points=12),
    "NZDUSD": dict(digits=5, contract_size=100_000, tick_value=1.0, spread_points=16),
    "USDJPY": dict(digits=3, contract_size=100_000, tick_value=0.67, spread_points=12),
    "EURJPY": dict(digits=3, contract_size=100_000, tick_value=0.67, spread_points=18),
    "GBPJPY": dict(digits=3, contract_size=100_000, tick_value=0.67, spread_points=25),
    "USDCHF": dict(digits=5, contract_size=100_000, tick_value=1.12, spread_points=14),
    "USDCAD": dict(digits=5, contract_size=100_000, tick_value=0.73, spread_points=15),
    "EURGBP": dict(digits=5, contract_size=100_000, tick_value=1.27, spread_points=14),
    "XAUUSD": dict(digits=2, contract_size=100, tick_value=1.0, spread_points=30),
    "XAGUSD": dict(digits=3, contract_size=5_000, tick_value=5.0, spread_points=30),
    "US500": dict(digits=1, contract_size=1, tick_value=0.1, spread_points=5, volume_step=0.1, volume_min=0.1),
    "US30": dict(digits=1, contract_size=1, tick_value=0.1, spread_points=20, volume_step=0.1, volume_min=0.1),
    "NAS100": dict(digits=1, contract_size=1, tick_value=0.1, spread_points=15, volume_step=0.1, volume_min=0.1),
    "GER40": dict(digits=1, contract_size=1, tick_value=0.11, spread_points=15, volume_step=0.1, volume_min=0.1),
    "BTCUSD": dict(digits=2, contract_size=1, tick_value=0.01, spread_points=3000),
}

YAHOO_TICKERS = {
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "US500": "^GSPC",
    "SPX500": "^GSPC",
    "US30": "^DJI",
    "NAS100": "^NDX",
    "USTEC": "^NDX",
    "GER40": "^GDAXI",
    "DE40": "^GDAXI",
    "UK100": "^FTSE",
    "JP225": "^N225",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "USOIL": "CL=F",
    "XTIUSD": "CL=F",
    "UKOIL": "BZ=F",
    "XBRUSD": "BZ=F",
}

# Nicht-Devisen-Symbole -> (Sentiment-Basis, Sentiment-Quote)
SPECIAL_LEGS = {
    "US500": ("EQ_US", None),
    "SPX500": ("EQ_US", None),
    "US30": ("EQ_US", None),
    "NAS100": ("EQ_US", None),
    "USTEC": ("EQ_US", None),
    "GER40": ("EQ_EU", None),
    "DE40": ("EQ_EU", None),
    "UK100": ("EQ_UK", None),
    "JP225": ("EQ_JP", None),
    "BTCUSD": ("CRYPTO", "USD"),
    "ETHUSD": ("CRYPTO", "USD"),
    "USOIL": ("OIL", None),
    "XTIUSD": ("OIL", None),
    "UKOIL": ("OIL", None),
    "XBRUSD": ("OIL", None),
}

ASSET_TO_CURRENCY = {"EQ_US": "USD", "EQ_EU": "EUR", "EQ_UK": "GBP", "EQ_JP": "JPY", "OIL": "USD", "CRYPTO": "USD"}


def base_symbol(symbol: str) -> str:
    """Broker-Suffixe entfernen: 'EURUSD.m', 'EURUSDm', 'EURUSD-ECN' -> 'EURUSD'."""
    s = symbol.upper()
    known = set(DEFAULT_SPECS) | set(YAHOO_TICKERS) | set(SPECIAL_LEGS)
    for key in sorted(known, key=len, reverse=True):
        if s.startswith(key):
            return key
    letters = re.sub(r"[^A-Z]", "", s)
    if len(letters) >= 6 and letters[:3] in CURRENCIES | METALS and letters[3:6] in CURRENCIES:
        return letters[:6]
    return re.split(r"[^A-Z0-9]", s)[0] or s


def sentiment_legs(symbol: str) -> tuple:
    b = base_symbol(symbol)
    if b in SPECIAL_LEGS:
        return SPECIAL_LEGS[b]
    if len(b) == 6 and b[:3] in CURRENCIES | METALS and b[3:] in CURRENCIES:
        return b[:3], b[3:]
    return None, None


def calendar_currencies(symbol: str, overrides: dict | None = None) -> list:
    if overrides and symbol in overrides:
        return list(overrides[symbol])
    out = []
    for leg in sentiment_legs(symbol):
        if leg is None:
            continue
        cur = leg if leg in CURRENCIES else ASSET_TO_CURRENCY.get(leg)
        if cur and cur not in out:
            out.append(cur)
    return out


def yahoo_ticker(symbol: str, overrides: dict | None = None) -> str:
    if overrides and symbol in overrides:
        return overrides[symbol]
    b = base_symbol(symbol)
    if b in YAHOO_TICKERS:
        return YAHOO_TICKERS[b]
    if len(b) == 6 and b[:3] in CURRENCIES and b[3:] in CURRENCIES:
        return f"{b}=X"
    return b


def default_symbol_info(symbol: str, price: float | None = None, overrides: dict | None = None) -> SymbolInfo:
    """Symbol-Stammdaten für Backtest/Paper-Trading."""
    b = base_symbol(symbol)
    spec = dict(DEFAULT_SPECS.get(b, {}))
    if not spec:
        # Unbekanntes Symbol: Größenordnung aus dem Preis ableiten, 1 Lot = 1 Einheit
        digits = 5 if price is None or price < 10 else (3 if price < 1000 else 2)
        point = 10.0**-digits
        spec = dict(digits=digits, contract_size=1, tick_value=point, spread_points=10)
    spec.update((overrides or {}).get(symbol, {}))
    digits = int(spec["digits"])
    point = 10.0**-digits
    return SymbolInfo(
        name=symbol,
        digits=digits,
        point=point,
        tick_size=float(spec.get("tick_size", point)),
        tick_value=float(spec["tick_value"]),
        contract_size=float(spec["contract_size"]),
        volume_min=float(spec.get("volume_min", 0.01)),
        volume_max=float(spec.get("volume_max", 100.0)),
        volume_step=float(spec.get("volume_step", 0.01)),
        spread_points=float(spec.get("spread_points", 0.0)),
    )
