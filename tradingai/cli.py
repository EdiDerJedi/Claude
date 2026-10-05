"""Kommandozeile: python -m tradingai <befehl> [optionen]

Befehle:
  backtest          System mit historischen Daten testen (inkl. Lernen)
  train             sofort optimieren und ML-Modell trainieren
  run               Bot starten (paper oder live – siehe mode in config.yaml)
  status            Zustand anzeigen: Gewichte, Parameter, Modell, Risiko
  news              aktuelle Nachrichtenlage und Wirtschaftstermine anzeigen
  reset-killswitch  Not-Aus nach manueller Prüfung zurücksetzen
"""

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import time
import zlib
from pathlib import Path

from .backtest import run_backtest
from .config import Config, load_config
from .data.market_data import ContextProvider, fetch_yahoo, load_csv, synthetic_ohlc
from .data.symbols import calendar_currencies, default_symbol_info, yahoo_ticker
from .engine import TradingEngine
from .risk import RiskManager
from .state import StateStore
from .timeframes import minutes

log = logging.getLogger("tradingai")

DISCLAIMER = (
    "HINWEIS: Trading mit Hebelprodukten ist sehr riskant. Kein Algorithmus garantiert Gewinne – "
    "auch ein selbstlernender nicht. Teste ausgiebig mit Backtest und Demokonto, bevor du echtes Geld einsetzt."
)

START_PRICES = {"EURUSD": 1.10, "GBPUSD": 1.27, "USDJPY": 150.0, "XAUUSD": 2300.0, "AUDUSD": 0.66}


def setup_logging(level: str, state_dir: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if state_dir:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(Path(state_dir) / "tradingai.log", maxBytes=5_000_000,
                                                  backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("yfinance", "urllib3", "httpx", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------- Daten
def load_history(cfg: Config, symbol: str, source: str, bars: int | None = None):
    if source == "synthetic":
        seed = zlib.crc32(symbol.encode()) % 10_000
        df = synthetic_ohlc(bars or 6000, cfg.timeframe, seed=seed, start_price=START_PRICES.get(symbol, 100.0))
    elif source == "csv":
        folder = Path(cfg.paper.csv_dir)
        candidates = [folder / f"{symbol}_{cfg.timeframe}.csv", folder / f"{symbol}.csv"]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            raise FileNotFoundError(f"Keine CSV für {symbol} gefunden (gesucht: {', '.join(map(str, candidates))})")
        df = load_csv(path)
    elif source == "yahoo":
        df = fetch_yahoo(yahoo_ticker(symbol, cfg.internet.yahoo_symbols), cfg.timeframe)
    else:
        raise ValueError(f"Unbekannte Datenquelle {source}")
    return df.iloc[-bars:] if bars else df


def build_internet(cfg: Config, with_news: bool = True):
    ic = cfg.internet
    if not ic.enabled:
        return None, None, None
    context = ContextProvider(ic.context_symbols) if cfg.learning.ml_enabled and ic.context_symbols else None
    news = calendar = None
    if with_news and ic.news_enabled:
        from .data.news import NewsMonitor, make_analyzer

        news = NewsMonitor(ic.news_feeds, make_analyzer(ic.news_analyzer, ic.claude_model), ic.news_refresh_minutes,
                           ic.news_max_age_hours, log_path=Path(cfg.state_dir) / "sentiment_log.csv")
    if with_news and ic.calendar_enabled:
        from .data.calendar import EconomicCalendar

        calendar = EconomicCalendar(ic.calendar_url, ic.calendar_impacts, ic.calendar_block_before_minutes,
                                    ic.calendar_block_after_minutes)
    return context, news, calendar


def make_broker(cfg: Config):
    if cfg.mode == "live":
        from .brokers.mt5 import MT5Broker

        broker = MT5Broker(cfg.mt5)
        broker.connect()
        acc = broker.account()
        if not acc.is_demo:
            if not cfg.mt5.allow_real_account:
                broker.shutdown()
                raise SystemExit(
                    "ABBRUCH: Das MT5-Konto ist ein ECHTGELD-Konto. Zum Schutz handelt der Bot nur auf Demokonten.\n"
                    "Wenn du das Risiko bewusst eingehen willst, setze in config.yaml: mt5.allow_real_account: true"
                )
            log.warning("!!! ECHTGELD-KONTO – der Bot handelt mit echtem Geld !!!")
        return broker

    source = cfg.paper.data_source
    if source == "synthetic":
        raise SystemExit("paper.data_source 'synthetic' ist nur für Backtests gedacht – nutze 'yahoo'.")
    from .brokers.sim import PaperBroker

    def fetch(symbol):
        return load_history(cfg, symbol, source)

    infos = {}
    for s in cfg.symbols:
        df = fetch(s)
        infos[s] = default_symbol_info(s, float(df["close"].iloc[-1]), cfg.paper.symbol_specs)
    refresh = min(60.0, minutes(cfg.timeframe) * 60 / 4)
    return PaperBroker(infos, cfg.paper.initial_balance, Path(cfg.state_dir) / "paper_account.json", fetch, refresh)


# ----------------------------------------------------------------- Befehle
def cmd_backtest(args) -> int:
    cfg = load_config(args.config)
    setup_logging("WARNING" if args.quiet else cfg.log_level)
    print(DISCLAIMER)
    symbols = args.symbols or cfg.symbols
    source = args.source or cfg.paper.data_source
    if args.no_ml:
        cfg.learning.ml_enabled = False
    data = {s: load_history(cfg, s, source, args.bars) for s in symbols}
    for s, df in data.items():
        log.info("%s: %d Bars von %s bis %s", s, len(df), df.index[0], df.index[-1])
    context = None
    if source == "yahoo" and cfg.internet.enabled and cfg.learning.ml_enabled:
        context = ContextProvider(cfg.internet.context_symbols)
        context.update(force=True)
    report = run_backtest(cfg, data, context_provider=context, warmup=args.warmup, learn=not args.no_learn)
    print(report.format())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report.equity.to_csv(out / "equity.csv")
    report.trades_frame().to_csv(out / "trades.csv", index=False)
    (out / "learned_state.json").write_text(json.dumps(report.state, indent=2, default=str), encoding="utf-8")
    print(f"Kapitalkurve, Trades und gelernter Zustand gespeichert in {out}/")
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg.log_level, cfg.state_dir)
    log.warning(DISCLAIMER)
    store = StateStore(cfg.state_dir)
    broker = make_broker(cfg)
    context, news, calendar = build_internet(cfg)
    engine = TradingEngine(cfg, broker, store, news=news, calendar=calendar, context_provider=context)
    log.info("Bot gestartet: Modus %s, Symbole %s, Timeframe %s", cfg.mode.upper(), ", ".join(cfg.symbols),
             cfg.timeframe)

    stop = {"flag": False}

    def _stop(signum, frame):
        stop["flag"] = True
        log.info("Beende nach dem aktuellen Durchlauf ... (offene Positionen behalten ihren SL/TP)")

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    try:
        while not stop["flag"]:
            try:
                engine.step()
            except ConnectionError as exc:
                log.error("Verbindungsproblem: %s – neuer Versuch im nächsten Durchlauf", exc)
            except Exception:
                log.exception("Unerwarteter Fehler im Durchlauf")
            if args.once:
                break
            for _ in range(int(cfg.loop_seconds * 10)):
                if stop["flag"]:
                    break
                time.sleep(0.1)
    finally:
        store.save()
        broker.shutdown()
    return 0


def cmd_train(args) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg.log_level, cfg.state_dir)
    store = StateStore(cfg.state_dir)
    broker = make_broker(cfg)
    context, _, _ = build_internet(cfg, with_news=False)
    if context is not None:
        context.update(force=True)
    engine = TradingEngine(cfg, broker, store, context_provider=context, learn=True)
    engine.learn_all()
    broker.shutdown()
    print_status(cfg, store)
    return 0


def print_status(cfg: Config, store: StateStore) -> None:
    risk = store.data.get("risk", {})
    print("=" * 60)
    print("STATUS")
    print("=" * 60)
    if risk:
        print(f"Kapital: {risk.get('equity', 0):,.2f} | Höchststand: {risk.get('peak_equity', 0):,.2f} | "
              f"Tagesstart: {risk.get('day_start_equity', 0):,.2f}")
        print("Not-Aus:", f"AKTIV – {risk.get('kill_reason')}" if risk.get("killed") else "aus")
    for symbol in cfg.symbols:
        print("-" * 60)
        print(symbol)
        for name, params in store.data["params"].get(symbol, {}).items():
            print(f"  gelernte Parameter {name}: {params}")
        mm = store.data["model_metrics"].get(symbol)
        if mm:
            state = "aktiv" if mm.get("accepted") else f"abgelehnt ({mm.get('reason')})"
            print(f"  ML-Modell ({mm.get('time')}): {state} | Trefferquote {mm.get('val_accuracy', 0):.1%} | "
                  f"Validierungs-Sharpe {mm.get('val_sharpe', 0):.2f}")
        dec = store.data["decisions"].get(symbol)
        if dec:
            weights = ", ".join(f"{k} {v:.0%}" for k, v in sorted(dec["weights"].items(), key=lambda x: -x[1]))
            print(f"  letzte Entscheidung {dec['time']}: Konsens {dec['score']:+.2f} -> {dec['action']}")
            print(f"  Strategie-Gewichte: {weights}")


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    print_status(cfg, StateStore(cfg.state_dir))
    return 0


def cmd_news(args) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg.log_level)
    cfg.internet.enabled = True
    _, news, calendar = build_internet(cfg)
    if news:
        news.update()
        print(f"\n{len(news.items)} aktuelle Meldungen. Sentiment je Symbol (-1 negativ ... +1 positiv):")
        for s in cfg.symbols:
            value = news.pair_sentiment(s)
            print(f"  {s:<10} {'n/a' if value is None else f'{value:+.2f}'}")
        for item in news.items[:10]:
            print(f"  - {item.title}")
    if calendar:
        calendar.update()
        print("\nWichtige Termine der nächsten 48 Stunden:")
        for s in cfg.symbols:
            for ev in calendar.upcoming(calendar_currencies(s, cfg.internet.symbol_currencies), hours=48):
                if ev.impact.lower() in calendar.impacts:
                    print(f"  {ev.time:%a %d.%m. %H:%M} UTC  {ev.currency}  {ev.title}  ({s})")
    return 0


def cmd_reset(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.state_dir)
    RiskManager(cfg.risk, store.data["risk"]).reset_kill_switch()
    store.save()
    print("Not-Aus zurückgesetzt.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tradingai", description="Selbstlernender Trading-Bot für MetaTrader 5")
    parser.add_argument("-c", "--config", default="config.yaml", help="Pfad zur Konfiguration (Default: config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="historischer Test inkl. Lernen")
    bt.add_argument("--source", choices=["yahoo", "csv", "synthetic"], help="Datenquelle (Default: paper.data_source)")
    bt.add_argument("--symbols", nargs="+", help="Symbole (Default: aus der Konfiguration)")
    bt.add_argument("--bars", type=int, help="nur die letzten N Bars verwenden")
    bt.add_argument("--warmup", type=int, help="Bars vor dem Start (Default: automatisch)")
    bt.add_argument("--no-learn", action="store_true", help="ohne Lernen (feste Standardparameter)")
    bt.add_argument("--no-ml", action="store_true", help="ohne Machine-Learning-Modell (schneller)")
    bt.add_argument("--out", default="reports", help="Ausgabeordner")
    bt.add_argument("-q", "--quiet", action="store_true", help="nur das Ergebnis ausgeben")
    bt.set_defaults(func=cmd_backtest)

    rn = sub.add_parser("run", help="Bot starten")
    rn.add_argument("--once", action="store_true", help="nur einen Durchlauf ausführen")
    rn.set_defaults(func=cmd_run)

    sub.add_parser("train", help="sofort lernen").set_defaults(func=cmd_train)
    sub.add_parser("status", help="Zustand anzeigen").set_defaults(func=cmd_status)
    sub.add_parser("news", help="Nachrichten und Termine anzeigen").set_defaults(func=cmd_news)
    sub.add_parser("reset-killswitch", help="Not-Aus zurücksetzen").set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    if args.command != "backtest" and not Path(args.config).exists():
        parser.error(f"{args.config} nicht gefunden – kopiere zuerst config.example.yaml nach config.yaml")
    if args.command == "backtest" and not Path(args.config).exists():
        args.config = None  # Backtest geht auch ohne Konfigurationsdatei
    return args.func(args)
