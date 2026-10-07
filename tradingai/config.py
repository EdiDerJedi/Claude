"""Konfiguration: YAML-Datei -> typisierte Dataclasses mit sinnvollen Defaults."""

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .timeframes import validate_timeframe

log = logging.getLogger(__name__)


@dataclass
class MT5Config:
    login: int = 0
    password: str = ""
    server: str = ""
    path: str = ""  # optional: Pfad zu terminal64.exe
    allow_real_account: bool = False  # Schutz: Echtgeld nur nach bewusster Freigabe
    magic: int = 26100501  # kennzeichnet Orders dieses Bots
    deviation: int = 20  # max. Slippage in Points


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01  # 1 % des Kapitals pro Trade
    max_open_positions: int = 3
    max_daily_loss: float = 0.03  # 3 % Tagesverlust -> keine neuen Trades bis morgen
    max_drawdown: float = 0.15  # 15 % vom Höchststand -> Not-Aus (alles schließen)
    atr_period: int = 14
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.0
    trailing_atr_mult: float = 0.0  # 0 = kein Trailing-Stop
    max_spread_points: float = 0.0  # 0 = kein Spread-Filter
    allow_min_lot_override: bool = False  # Mindestlot handeln, auch wenn es > Risiko ist


@dataclass
class LearningConfig:
    enabled: bool = True
    decay: float = 0.99  # Gedächtnis des Strategie-Selektors (0.99 ~ letzte 100 Bars)
    temperature: float = 1.0  # höher = Gewichte gleichmäßiger verteilt
    entry_threshold: float = 0.35  # Mindest-Konsens für einen neuen Trade
    exit_threshold: float = 0.05  # unter diesem Konsens wird geschlossen
    optimize_every_hours: float = 168.0
    optimize_trials: int = 40
    min_improvement: float = 0.1  # nötige Verbesserung der Out-of-Sample-Sharpe
    retrain_every_hours: float = 168.0
    ml_enabled: bool = True
    ml_horizon: int = 5  # Prognosehorizont in Bars
    ml_threshold: float = 0.55  # Mindestwahrscheinlichkeit für ein ML-Signal
    train_bars: int = 5000
    min_train_bars: int = 1000


@dataclass
class InternetConfig:
    enabled: bool = True
    yahoo_symbols: dict = field(default_factory=dict)  # z.B. {EURUSD: "EURUSD=X"}
    context_symbols: dict = field(
        default_factory=lambda: {"VIX": "^VIX", "SPX": "^GSPC", "DXY": "DX-Y.NYB", "US10Y": "^TNX"}
    )
    news_enabled: bool = True
    news_feeds: list = field(
        default_factory=lambda: [
            "https://www.fxstreet.com/rss/news",
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            "https://www.federalreserve.gov/feeds/press_all.xml",
            "https://www.ecb.europa.eu/rss/press.html",
        ]
    )
    news_analyzer: str = "lexicon"  # lexicon | claude
    claude_model: str = "claude-opus-5-5"
    news_refresh_minutes: float = 15.0
    news_max_age_hours: float = 12.0
    news_block_threshold: float = 0.5  # Trade blockieren, wenn Sentiment klar dagegen spricht
    calendar_enabled: bool = True
    calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    calendar_impacts: list = field(default_factory=lambda: ["High"])
    calendar_block_before_minutes: float = 30.0
    calendar_block_after_minutes: float = 30.0
    symbol_currencies: dict = field(default_factory=dict)  # Overrides für den Kalender


@dataclass
class PaperConfig:
    initial_balance: float = 10000.0
    data_source: str = "yahoo"  # yahoo | csv | synthetic
    csv_dir: str = "data"
    symbol_specs: dict = field(default_factory=dict)  # Overrides, z.B. {EURUSD: {spread_points: 12}}


@dataclass
class DashboardConfig:
    host: str = "127.0.0.1"  # nur dieser PC; aus Sicherheitsgründen nicht ändern
    port: int = 8765
    open_browser: bool = True


SECTIONS = {
    "mt5": MT5Config,
    "risk": RiskConfig,
    "learning": LearningConfig,
    "internet": InternetConfig,
    "paper": PaperConfig,
    "dashboard": DashboardConfig,
}


@dataclass
class Config:
    mode: str = "paper"  # paper | live
    symbols: list = field(default_factory=lambda: ["EURUSD"])
    timeframe: str = "H1"
    history_bars: int = 600  # Bars für die Signalberechnung
    loop_seconds: float = 30.0
    state_dir: str = "state"
    log_level: str = "INFO"
    mt5: MT5Config = field(default_factory=MT5Config)
    risk: RiskConfig = field(default_factory=RiskConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    internet: InternetConfig = field(default_factory=InternetConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


def _build(cls, data: dict, prefix: str = ""):
    known = {f.name for f in fields(cls)}
    kwargs = {}
    for key, value in (data or {}).items():
        if key not in known:
            log.warning("Unbekannter Konfigurationsschlüssel ignoriert: %s%s", prefix, key)
            continue
        if prefix == "" and key in SECTIONS:
            value = _build(SECTIONS[key], value or {}, prefix=f"{key}.")
        kwargs[key] = value
    return cls(**kwargs)


def validate(cfg: Config) -> Config:
    if cfg.mode not in ("paper", "live"):
        raise ValueError("mode muss 'paper' oder 'live' sein")
    cfg.timeframe = validate_timeframe(cfg.timeframe)
    if not cfg.symbols:
        raise ValueError("Mindestens ein Symbol angeben")
    cfg.symbols = [str(s) for s in cfg.symbols]
    r = cfg.risk
    if not 0 < r.risk_per_trade <= 0.05:
        raise ValueError("risk.risk_per_trade muss zwischen 0 und 0.05 (5 %) liegen")
    if r.risk_per_trade > 0.02:
        log.warning("Risiko pro Trade %.1f %% ist hoch – empfohlen sind 0.5–1 %%", r.risk_per_trade * 100)
    if not 0 < r.max_drawdown < 1 or not 0 < r.max_daily_loss < 1:
        raise ValueError("risk.max_drawdown und risk.max_daily_loss müssen zwischen 0 und 1 liegen")
    if r.sl_atr_mult <= 0:
        raise ValueError("risk.sl_atr_mult muss > 0 sein (jeder Trade braucht einen Stop-Loss)")
    lr = cfg.learning
    if not 0.5 < lr.decay < 1:
        raise ValueError("learning.decay muss zwischen 0.5 und 1 liegen")
    if not 0.5 <= lr.ml_threshold < 1:
        raise ValueError("learning.ml_threshold muss zwischen 0.5 und 1 liegen")
    if cfg.internet.news_analyzer not in ("lexicon", "claude"):
        raise ValueError("internet.news_analyzer muss 'lexicon' oder 'claude' sein")
    if cfg.paper.data_source not in ("yahoo", "csv", "synthetic"):
        raise ValueError("paper.data_source muss 'yahoo', 'csv' oder 'synthetic' sein")
    return cfg


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return validate(Config())
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Konfigurationsdatei {path} nicht gefunden – kopiere config.example.yaml nach {path}"
        )
    with path.open("r", encoding="utf-8-sig") as fh:
        data = yaml.safe_load(fh) or {}
    return validate(_build(Config, data))
