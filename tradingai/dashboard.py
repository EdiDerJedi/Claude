"""Dashboard im Browser: Kontostand, Trades, Bot-Status und Start/Stopp.

Starten:  python -m tradingai dashboard   (Windows: Doppelklick auf dashboard_starten.bat)
Adresse:  http://127.0.0.1:8765

Sicherheit: Der Server lauscht nur auf diesem PC (127.0.0.1). Aktionen (Bot starten/
stoppen usw.) brauchen ein geheimes Token, das nur in der ausgelieferten Seite steht –
fremde Webseiten können den Bot daher nicht fernsteuern. Zusätzlich wird der
Host-Header geprüft (Schutz gegen DNS-Rebinding).
"""

import csv
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import Config
from .status import EQUITY, STATUS, read_bot_state, read_json, request_stop

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
APP_ID = "tradingai-dashboard"

REASONS = {"sl": "Stop-Loss", "tp": "Take-Profit", "tai exit": "Signal", "signal": "Signal",
           "Not-Aus": "Not-Aus", "Backtest-Ende": "Ende"}


# ------------------------------------------------------------------ Dateien lesen
def tail_lines(path: Path, n: int = 80) -> list:
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in deque(fh, maxlen=n)]
    except OSError:
        return []


def _float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_trades(path: Path, limit: int = 50) -> dict:
    """Letzte Trades (neueste zuerst) und Kennzahlen über alle Trades."""
    rows = []
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        pass
    profits = [_float(r.get("profit")) for r in rows]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    gross_loss = -sum(losses)
    stats = {
        "count": len(profits),
        "wins": len(wins),
        "win_rate": len(wins) / len(profits) if profits else None,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "total": sum(profits),
        "best": max(profits) if profits else None,
        "worst": min(profits) if profits else None,
    }
    recent = []
    for r in reversed(rows[-limit:]):
        reason = r.get("reason") or ""
        recent.append({
            "ticket": r.get("ticket"), "symbol": r.get("symbol"),
            "direction": int(_float(r.get("direction"))), "volume": _float(r.get("volume")),
            "open_price": _float(r.get("open_price")), "close_price": _float(r.get("close_price")),
            "open_time": r.get("open_time"), "close_time": r.get("close_time"),
            "profit": _float(r.get("profit")), "reason": REASONS.get(reason, reason),
        })
    return {"recent": recent, "stats": stats}


def read_equity(path: Path, max_points: int = 400) -> list:
    points = []
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("time"):
                    points.append({"t": r["time"], "balance": _float(r.get("balance")),
                                   "equity": _float(r.get("equity"))})
    except OSError:
        return []
    if len(points) > max_points:  # gleichmäßig ausdünnen, letzten Punkt immer behalten
        step = len(points) / max_points
        points = [points[int(i * step)] for i in range(max_points - 1)] + [points[-1]]
    return points


# ------------------------------------------------------------------ Dashboard
class Dashboard:
    def __init__(self, cfg: Config, config_path: str | None, cwd: Path | None = None):
        self.cfg = cfg
        self.config_path = config_path
        self.cwd = Path(cwd or Path.cwd())
        self.state_dir = (self.cwd / cfg.state_dir) if not Path(cfg.state_dir).is_absolute() else Path(cfg.state_dir)
        self.token = secrets.token_urlsafe(24)
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ Status
    def bot_state(self) -> dict:
        st = read_bot_state(self.state_dir, stale_after=max(45.0, self.cfg.loop_seconds * 1.5))
        proc = self.process
        if proc is not None and self.started_at is not None and time.time() - self.started_at < 180:
            code = proc.poll()
            ours = str(st.get("pid")) == str(proc.pid)
            if code is None and not ours:
                # Prozess lebt, hat aber noch keinen Herzschlag geschrieben (Programmteile laden)
                st.update(state="starting", label="Startet …")
            elif code not in (None, 0) and not (ours and st["state"] == "running"):
                st.update(state="failed", label="Start fehlgeschlagen", exit_code=code,
                          details=tail_lines(self.state_dir / "bot_stderr.log", 25))
        return st

    def status(self) -> dict:
        state = read_json(self.state_dir / "state.json") or {}
        snapshot = read_json(self.state_dir / STATUS) or {}
        learning = {}
        for sym in self.cfg.symbols:
            learning[sym] = {
                "params": state.get("params", {}).get(sym, {}),
                "model": state.get("model_metrics", {}).get(sym),
                "decision": state.get("decisions", {}).get(sym),
                "last_optimize": state.get("last_optimize", {}).get(sym),
                "last_retrain": state.get("last_retrain", {}).get(sym),
            }
        return {
            "app": APP_ID,
            "version": __version__,
            "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "bot": self.bot_state(),
            "snapshot": snapshot,
            "trades": read_trades(self.state_dir / "trades.csv"),
            "equity": read_equity(self.state_dir / EQUITY),
            "learning": learning,
            "log": tail_lines(self.state_dir / "tradingai.log", 80),
            "config": {
                "found": self.config_path is not None,
                "mode": self.cfg.mode,
                "symbols": list(self.cfg.symbols),
                "timeframe": self.cfg.timeframe,
                "loop_seconds": self.cfg.loop_seconds,
                "state_dir": str(self.cfg.state_dir),
                "risk_per_trade": self.cfg.risk.risk_per_trade,
                "max_daily_loss": self.cfg.risk.max_daily_loss,
                "max_drawdown": self.cfg.risk.max_drawdown,
                "max_open_positions": self.cfg.risk.max_open_positions,
                "allow_real_account": self.cfg.mt5.allow_real_account,
            },
        }

    # ------------------------------------------------------------ Aktionen
    def _python(self) -> str:
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe":
            console = exe.with_name("python.exe")
            if console.exists():
                return str(console)
        return str(exe)

    def start_bot(self) -> tuple[int, dict]:
        st = self.bot_state()
        if st["state"] in ("running", "unresponsive", "starting"):
            return 409, {"ok": False, "message": "Der Bot läuft bereits."}
        if self.config_path is None:
            return 400, {"ok": False, "message": "config.yaml fehlt – bitte zuerst install.bat ausführen."}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cmd = [self._python(), "-m", "tradingai", "-c", str(self.config_path), "run"]
        kwargs = {}
        if os.name == "nt":
            # unsichtbar und unabhängig vom Dashboard: Schließen des Dashboards stoppt den Bot nicht
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        stderr = (self.state_dir / "bot_stderr.log").open("ab")
        try:
            self.process = subprocess.Popen(cmd, cwd=str(self.cwd), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=stderr, close_fds=True, **kwargs)
        finally:
            stderr.close()
        self.started_at = time.time()
        log.info("Bot gestartet (PID %s): %s", self.process.pid, " ".join(cmd))
        return 202, {"ok": True, "message": "Bot wird gestartet …"}

    def stop_bot(self) -> tuple[int, dict]:
        st = self.bot_state()
        if st["state"] not in ("running", "unresponsive", "starting"):
            return 409, {"ok": False, "message": "Der Bot läuft nicht."}
        request_stop(self.state_dir)
        log.info("Stopp-Anfrage an den Bot gesendet")
        return 202, {"ok": True, "message": "Bot wird nach dem aktuellen Durchlauf beendet …"}

    def reset_kill_switch(self) -> tuple[int, dict]:
        from .cli import InstanceLock
        from .risk import RiskManager
        from .state import StateStore

        lock = InstanceLock(str(self.state_dir))
        if not lock.acquire():
            return 409, {"ok": False, "message": "Erst den Bot stoppen, dann den Not-Aus zurücksetzen."}
        try:
            store = StateStore(self.state_dir)
            RiskManager(self.cfg.risk, store.data["risk"]).reset_kill_switch()
            store.save()
        finally:
            lock.release()
        log.info("Not-Aus über das Dashboard zurückgesetzt")
        return 200, {"ok": True, "message": "Not-Aus zurückgesetzt."}

    # ------------------------------------------------------------ Server
    def page(self, nonce: str) -> bytes:
        html = (WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
        return html.replace("__TOKEN__", self.token).replace("__NONCE__", nonce).encode("utf-8")

    def allowed_hosts(self) -> set:
        port = self.server.server_address[1] if self.server else self.cfg.dashboard.port
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def serve(self, open_browser: bool = True) -> None:
        handler = _make_handler(self)
        host, port = self.cfg.dashboard.host, self.cfg.dashboard.port
        for candidate in range(port, port + 10):
            if _is_our_dashboard(host, candidate):
                url = f"http://127.0.0.1:{candidate}/"
                log.info("Dashboard läuft bereits: %s", url)
                if open_browser:
                    webbrowser.open(url)
                return
            try:
                self.server = _Server((host, candidate), handler)
                break
            except OSError:
                continue
        if self.server is None:
            raise OSError(f"Kein freier Port zwischen {port} und {port + 9}")
        url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        log.info("Dashboard läuft auf %s", url)
        print(f"Dashboard läuft auf {url}  (Beenden: Strg+C oder Knopf im Dashboard)")
        if open_browser:
            threading.Timer(0.8, webbrowser.open, args=(url,)).start()
        try:
            self.server.serve_forever(poll_interval=0.5)
        finally:
            self.server.server_close()

    def shutdown_later(self) -> None:
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Unter Windows erlaubt SO_REUSEADDR, einen schon belegten Port doppelt zu öffnen
    allow_reuse_address = os.name != "nt"


# Lokale Anfragen nie über einen (Firmen-)Proxy schicken
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_our_dashboard(host: str, port: int) -> bool:
    try:
        with _LOCAL.open(f"http://127.0.0.1:{port}/api/ping", timeout=1) as resp:
            return json.loads(resp.read().decode("utf-8")).get("app") == APP_ID
    except Exception:
        return False


def _make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingAI"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("HTTP %s", fmt % args)

        # -------------------------------------------------------- Hilfen
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data) -> None:
            self._send(code, json.dumps(data, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in dash.allowed_hosts()

        # -------------------------------------------------------- GET
        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "message": "Unzulässiger Host"})
            path = self.path.split("?", 1)[0]
            if path == "/":
                nonce = secrets.token_urlsafe(16)
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                       "frame-ancestors 'none'")
                return self._send(200, dash.page(nonce), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": csp})
            if path == "/api/ping":
                return self._json(200, {"app": APP_ID})
            if path == "/api/status":
                try:
                    return self._json(200, dash.status())
                except Exception as exc:  # nie mit kaputter Seite enden
                    log.exception("Statusabfrage fehlgeschlagen")
                    return self._json(500, {"ok": False, "message": str(exc)})
            return self._json(404, {"ok": False, "message": "Nicht gefunden"})

        # -------------------------------------------------------- POST
        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "message": "Unzulässiger Host"})
            origin = self.headers.get("Origin")
            if origin and origin.lower().removeprefix("http://") not in dash.allowed_hosts():
                return self._json(403, {"ok": False, "message": "Unzulässige Herkunft"})
            if not secrets.compare_digest(self.headers.get("X-TradingAI-Token", ""), dash.token):
                return self._json(403, {"ok": False, "message": "Ungültiges Token – Seite neu laden"})
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 10_000))
            actions = {
                "/api/bot/start": dash.start_bot,
                "/api/bot/stop": dash.stop_bot,
                "/api/killswitch/reset": dash.reset_kill_switch,
            }
            path = self.path.split("?", 1)[0]
            if path in actions:
                try:
                    code, data = actions[path]()
                except Exception as exc:
                    log.exception("Aktion %s fehlgeschlagen", path)
                    code, data = 500, {"ok": False, "message": str(exc)}
                return self._json(code, data)
            if path == "/api/dashboard/quit":
                self._json(200, {"ok": True, "message": "Dashboard wird beendet. Der Bot läuft weiter."})
                dash.shutdown_later()
                return None
            return self._json(404, {"ok": False, "message": "Nicht gefunden"})

        def do_OPTIONS(self):  # keine CORS-Freigaben
            self._json(405, {"ok": False, "message": "Nicht erlaubt"})

    return Handler
