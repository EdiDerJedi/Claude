"""Dashboard im Browser: Kontostand, Trades, Bot-Status und Start/Stopp.

Starten:  python -m tradingai dashboard   (Windows: Doppelklick auf dashboard_starten.bat)
Adresse:  http://127.0.0.1:8765

Sicherheit: Der Server lauscht nur auf diesem PC (127.0.0.1). Aktionen (Bot starten/
stoppen usw.) brauchen ein geheimes Token, das nur in der ausgelieferten Seite steht –
fremde Webseiten können den Bot daher nicht fernsteuern. Zusätzlich werden Host- und
Origin-Header geprüft (Schutz gegen DNS-Rebinding und CSRF).
"""

import csv
import json
import locale
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .status import (
    EQUITY, STATUS, STOP_REQUEST, InstanceLock, parse_iso, read_bot_state, read_json, request_stop,
    write_json_atomic,
)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
APP_ID = "tradingai-dashboard"
RUNNING_STATES = ("running", "waiting", "stalled", "unresponsive", "starting")

REASONS = {"sl": "Stop-Loss", "tp": "Take-Profit", "tai exit": "Signal", "signal": "Signal",
           "stopout": "Stop-Out (Margin)", "manual": "Manuell geschlossen", "Not-Aus": "Not-Aus",
           "Backtest-Ende": "Ende"}
RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}


# ------------------------------------------------------------------ Dateien lesen
def tail_lines(path: Path, n: int = 80, encodings=("utf-8",)) -> list:
    try:
        data = Path(path).read_bytes()[-200_000:]
    except OSError:
        return []
    text = None
    for enc in encodings:
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")
    return [line.rstrip("\r") for line in deque(text.split("\n"), maxlen=n + 1) if line.strip()][-n:]


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


class EquityReader:
    """Liest equity_history.csv (mit Cache) und dünnt für das Diagramm aus. Je Abschnitt
    bleiben Tiefst- und Höchstwert erhalten, damit kurze Einbrüche nicht verschwinden."""

    def __init__(self, path: Path):
        self.path = path
        self._key = None
        self._points: list = []

    def _load(self) -> list:
        try:
            st = self.path.stat()
        except OSError:
            return []
        key = (st.st_mtime_ns, st.st_size)
        if key != self._key:
            points = []
            try:
                with self.path.open("r", encoding="utf-8", newline="") as fh:
                    for r in csv.DictReader(fh):
                        t = parse_iso(r.get("time"))
                        if t is not None:
                            points.append((t, _float(r.get("balance")), _float(r.get("equity"))))
            except OSError:
                return self._points
            self._points, self._key = points, key
        return self._points

    def series(self, rng: str = "7d", max_points: int = 500) -> dict:
        points = self._load()
        total = len(points)
        span = RANGES.get(rng, RANGES["7d"])
        if span is not None and points:
            cutoff = points[-1][0] - span
            points = [p for p in points if p[0] >= cutoff]
        if len(points) > max_points:
            buckets = max_points // 2
            size = len(points) / buckets
            out = []
            for b in range(buckets):
                chunk = points[int(b * size):int((b + 1) * size)] or [points[min(len(points) - 1, int(b * size))]]
                lo = min(chunk, key=lambda p: p[2])
                hi = max(chunk, key=lambda p: p[2])
                out.extend(sorted({lo, hi}, key=lambda p: p[0]))
            if out[-1] != points[-1]:
                out.append(points[-1])
            points = out
        return {
            "range": rng if rng in RANGES else "7d",
            "total_points": total,
            "first": points[0][0].isoformat().replace("+00:00", "Z") if points else None,
            "points": [{"t": t.isoformat().replace("+00:00", "Z"), "balance": b, "equity": e} for t, b, e in points],
        }


# ------------------------------------------------------------------ Autostart (Windows)
def _startup_shortcut() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if os.name != "nt" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "TradingAI Dashboard.lnk"


def _set_windows_autostart(enabled: bool, cwd: Path) -> None:
    lnk = _startup_shortcut()
    if lnk is None:
        raise RuntimeError("Autostart gibt es nur unter Windows.")
    if not enabled:
        lnk.unlink(missing_ok=True)
        return
    pythonw = cwd / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable).with_name("pythonw.exe")
    # Pfade über Umgebungsvariablen übergeben – keine Probleme mit Leerzeichen oder Anführungszeichen
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TAI_LNK); "
              "$s.TargetPath=$env:TAI_TARGET; $s.Arguments='-m tradingai dashboard --no-browser'; "
              "$s.WorkingDirectory=$env:TAI_DIR; $s.IconLocation=$env:TAI_TARGET + ',0'; $s.Save()")
    env = dict(os.environ, TAI_LNK=str(lnk), TAI_TARGET=str(pythonw), TAI_DIR=str(cwd))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                   env=env, check=True, timeout=30, capture_output=True, creationflags=0x08000000)


# ------------------------------------------------------------------ Dashboard
class Dashboard:
    def __init__(self, cfg: Config, config_path: str | None, cwd: Path | None = None, config_error: str = ""):
        self.cwd = Path(cwd or Path.cwd())
        self.config_path = config_path
        self.config_error = config_error
        self._config_mtime = self._mtime()
        self._apply_config(cfg)
        self.token = secrets.token_urlsafe(24)
        self.process: subprocess.Popen | None = None
        self.launch_id = ""
        self.started_at: float | None = None
        self.server: ThreadingHTTPServer | None = None
        self.actions_lock = threading.Lock()

    # ------------------------------------------------------------ Konfiguration
    def _mtime(self) -> float | None:
        try:
            return Path(self.config_path).stat().st_mtime if self.config_path else None
        except OSError:
            return None

    def _apply_config(self, cfg: Config) -> None:
        self.cfg = cfg
        sd = Path(cfg.state_dir)
        self.state_dir = sd if sd.is_absolute() else self.cwd / sd
        self.equity = EquityReader(self.state_dir / EQUITY)

    def reload_config(self) -> None:
        """config.yaml neu einlesen, wenn sie geändert (oder neu angelegt) wurde."""
        if self.config_path is None:
            candidate = self.cwd / "config.yaml"
            if candidate.exists():
                self.config_path = str(candidate)
        mtime = self._mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            cfg = load_config(self.config_path)
        except Exception as exc:
            self.config_error = str(exc)
            log.error("config.yaml enthält einen Fehler: %s", exc)
            return
        port = self.cfg.dashboard.port
        self.config_error = ""
        self._apply_config(cfg)
        self.cfg.dashboard.port = port
        log.info("config.yaml neu eingelesen")

    @property
    def prefs_path(self) -> Path:
        return self.state_dir / "dashboard_prefs.json"

    def prefs(self) -> dict:
        p = read_json(self.prefs_path) or {}
        lnk = _startup_shortcut()
        return {
            "autostart_bot": bool(p.get("autostart_bot", False)),
            "autostart_windows": bool(lnk and lnk.exists()),
            "windows": os.name == "nt",
        }

    # ------------------------------------------------------------ Status
    def bot_state(self) -> dict:
        st = read_bot_state(self.state_dir, stale_after=max(45.0, self.cfg.loop_seconds * 1.5),
                            stall_after=max(900.0, self.cfg.loop_seconds * 10))
        proc = self.process
        if proc is None:
            return st
        code = proc.poll()
        ours = bool(self.launch_id) and st.get("launch_id") == self.launch_id
        if ours:
            if code is not None and st["state"] in RUNNING_STATES:
                st.update(state="crashed", label="Unerwartet beendet", exit_code=code,
                          details=self.stderr_tail())
            return st
        if st["state"] in ("running", "waiting", "stalled"):
            return st  # ein anderer Bot läuft (z.B. über bot_starten.bat gestartet)
        if code is None:
            st.update(state="starting", label="Startet …")
        elif code != 0:
            st.update(state="failed", label="Start fehlgeschlagen", exit_code=code, details=self.stderr_tail())
        return st

    def stderr_tail(self) -> list:
        enc = locale.getpreferredencoding(False) or "cp1252"
        return tail_lines(self.state_dir / "bot_stderr.log", 25, encodings=("utf-8", enc))

    def last_errors(self) -> list:
        lines = tail_lines(self.state_dir / "tradingai.log", 400)
        return [line for line in lines if " ERROR " in line or " CRITICAL " in line][-6:]

    def status(self, rng: str = "7d") -> dict:
        self.reload_config()
        state = read_json(self.state_dir / "state.json") or {}
        snapshot = read_json(self.state_dir / STATUS) or {}
        bot = self.bot_state()
        if bot["state"] not in RUNNING_STATES:
            # Not-Aus-Status direkt aus state.json: nach einem Zurücksetzen sofort korrekt
            risk = state.get("risk", {})
            if snapshot.get("risk"):
                snapshot["risk"]["killed"] = bool(risk.get("killed"))
                snapshot["risk"]["kill_reason"] = risk.get("kill_reason", "")
        symbols = snapshot.get("symbols") or list(self.cfg.symbols)
        learning = {}
        for sym in symbols:
            learning[sym] = {
                "params": state.get("params", {}).get(sym, {}),
                "model": state.get("model_metrics", {}).get(sym),
                "decision": state.get("decisions", {}).get(sym),
                "last_optimize": state.get("last_optimize", {}).get(sym),
                "last_retrain": state.get("last_retrain", {}).get(sym),
            }
        started = parse_iso(bot.get("started_at"))
        config_changed = bool(self._config_mtime and started and bot["state"] in RUNNING_STATES
                              and self._config_mtime > started.timestamp() + 2)
        return {
            "app": APP_ID,
            "version": __version__,
            "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "bot": bot,
            "can_kill": self._can_kill(bot),
            "last_errors": self.last_errors() if bot["state"] in ("crashed", "stopped_error", "stalled") else [],
            "snapshot": snapshot,
            "trades": read_trades(self.state_dir / "trades.csv"),
            "equity": self.equity.series(rng),
            "learning": learning,
            "log": tail_lines(self.state_dir / "tradingai.log", 80),
            "prefs": self.prefs(),
            "config": {
                "found": self.config_path is not None,
                "error": self.config_error,
                "changed_since_start": config_changed,
                "mode": self.cfg.mode,
                "symbols": symbols,
                "timeframe": self.cfg.timeframe,
                "loop_seconds": self.cfg.loop_seconds,
                "state_dir": str(self.cfg.state_dir),
                "risk_per_trade": self.cfg.risk.risk_per_trade,
                "max_daily_loss": self.cfg.risk.max_daily_loss,
                "max_drawdown": self.cfg.risk.max_drawdown,
                "max_open_positions": self.cfg.risk.max_open_positions,
                "allow_real_account": self.cfg.mt5.allow_real_account,
                "paper_balance": self.cfg.paper.initial_balance,
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

    def _can_kill(self, bot: dict) -> bool:
        return (bot["state"] in ("unresponsive", "stalled") and self.process is not None
                and self.process.poll() is None and bot.get("launch_id") == self.launch_id)

    def start_bot(self) -> tuple[int, dict]:
        self.reload_config()
        st = self.bot_state()
        if st["state"] in RUNNING_STATES:
            return 409, {"ok": False, "message": "Der Bot läuft bereits."}
        if self.config_path is None:
            return 400, {"ok": False, "message": "config.yaml fehlt – bitte zuerst install.bat ausführen."}
        if self.config_error:
            return 400, {"ok": False, "message": "config.yaml enthält einen Fehler: " + self.config_error}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / STOP_REQUEST).unlink(missing_ok=True)  # alte Stopp-Anfrage verwerfen
        cmd = [self._python(), "-m", "tradingai", "-c", str(self.config_path), "run"]
        self.launch_id = uuid.uuid4().hex
        env = dict(os.environ, TRADINGAI_LAUNCH_ID=self.launch_id, TRADINGAI_SPAWNED_AT=repr(time.time()),
                   PYTHONIOENCODING="utf-8")
        kwargs = {}
        if os.name == "nt":
            # unsichtbar und unabhängig vom Dashboard: Schließen des Dashboards stoppt den Bot nicht
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        # Für jeden Start neu: so zeigt ein Fehler nur die Meldung DIESES Starts
        stderr = (self.state_dir / "bot_stderr.log").open("wb")
        try:
            self.process = subprocess.Popen(cmd, cwd=str(self.cwd), env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=stderr, close_fds=True, **kwargs)
        finally:
            stderr.close()
        self.started_at = time.time()
        log.info("Bot gestartet (PID %s): %s", self.process.pid, " ".join(cmd))
        return 202, {"ok": True, "message": "Bot wird gestartet …"}

    def stop_bot(self) -> tuple[int, dict]:
        st = self.bot_state()
        if st["state"] not in RUNNING_STATES:
            return 409, {"ok": False, "message": "Der Bot läuft nicht."}
        request_stop(self.state_dir)
        log.info("Stopp-Anfrage an den Bot gesendet")
        return 202, {"ok": True, "message": "Bot wird beendet … (offene Positionen behalten Stop-Loss und Take-Profit)"}

    def kill_bot(self) -> tuple[int, dict]:
        st = self.bot_state()
        if not self._can_kill(st):
            return 409, {"ok": False, "message": "Zwangsweises Beenden ist nur für einen hängenden, vom Dashboard "
                                                 "gestarteten Bot möglich."}
        self.process.kill()  # unter Windows beendet das auch den eigentlichen Python-Prozess (Job-Objekt)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        hb = read_json(self.state_dir / "heartbeat.json") or {}
        hb.update(stopped=True, error=True, phase="zwangsweise beendet",
                  time=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        write_json_atomic(self.state_dir / "heartbeat.json", hb)
        log.warning("Bot zwangsweise beendet")
        return 200, {"ok": True, "message": "Bot wurde zwangsweise beendet."}

    def reset_kill_switch(self) -> tuple[int, dict]:
        from .risk import RiskManager
        from .state import StateStore

        lock = InstanceLock(self.state_dir)
        if not lock.acquire():
            return 409, {"ok": False, "message": "Erst den Bot stoppen, dann den Not-Aus zurücksetzen."}
        try:
            store = StateStore(self.state_dir)
            RiskManager(self.cfg.risk, store.data["risk"]).reset_kill_switch()
            store.save()
            snap = read_json(self.state_dir / STATUS)
            if snap and snap.get("risk"):
                snap["risk"].update(killed=False, kill_reason="")
                write_json_atomic(self.state_dir / STATUS, snap)
        finally:
            lock.release()
        log.info("Not-Aus über das Dashboard zurückgesetzt")
        return 200, {"ok": True, "message": "Not-Aus zurückgesetzt."}

    def save_settings(self, body: dict) -> tuple[int, dict]:
        prefs = read_json(self.prefs_path) or {}
        messages = []
        if "autostart_bot" in body:
            prefs["autostart_bot"] = bool(body["autostart_bot"])
            write_json_atomic(self.prefs_path, prefs)
            messages.append("Bot startet automatisch mit dem Dashboard." if prefs["autostart_bot"]
                            else "Bot startet nicht mehr automatisch.")
        if "autostart_windows" in body:
            if os.name != "nt":
                return 400, {"ok": False, "message": "Autostart mit Windows gibt es nur unter Windows."}
            try:
                _set_windows_autostart(bool(body["autostart_windows"]), self.cwd)
            except Exception as exc:
                return 500, {"ok": False, "message": f"Autostart konnte nicht geändert werden: {exc}"}
            messages.append("Dashboard startet mit Windows." if body["autostart_windows"]
                            else "Autostart mit Windows entfernt.")
        return 200, {"ok": True, "message": " ".join(messages) or "Keine Änderung."}

    def open_config(self) -> tuple[int, dict]:
        path = Path(self.config_path) if self.config_path else self.cwd / "config.yaml"
        if not path.exists():
            return 400, {"ok": False, "message": "config.yaml fehlt – bitte zuerst install.bat ausführen."}
        if os.name != "nt":
            return 400, {"ok": False, "message": f"Bitte die Datei selbst öffnen: {path}"}
        subprocess.Popen(["notepad.exe", str(path)], close_fds=True)
        return 200, {"ok": True, "message": "config.yaml wird im Editor geöffnet. Nach dem Speichern den Bot neu starten."}

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
        if host not in ("127.0.0.1", "localhost"):
            log.warning("dashboard.host %r ignoriert – das Dashboard ist aus Sicherheitsgründen nur lokal erreichbar",
                        host)
            host = "127.0.0.1"
        for candidate in range(port, port + 10):
            if _is_our_dashboard(candidate):
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
        if self.prefs()["autostart_bot"] and self.bot_state()["state"] in ("never", "stopped", "stopped_error",
                                                                           "crashed"):
            log.info("Automatischer Bot-Start (Einstellung im Dashboard)")
            threading.Timer(2.0, self._autostart).start()
        try:
            self.server.serve_forever(poll_interval=0.5)
        finally:
            self.server.server_close()

    def _autostart(self) -> None:
        with self.actions_lock:
            try:
                self.start_bot()
            except Exception:
                log.exception("Automatischer Bot-Start fehlgeschlagen")

    def shutdown_later(self) -> None:
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Unter Windows erlaubt SO_REUSEADDR, einen schon belegten Port doppelt zu öffnen
    allow_reuse_address = os.name != "nt"


# Lokale Anfragen nie über einen (Firmen-)Proxy schicken
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_our_dashboard(port: int) -> bool:
    try:
        with _LOCAL.open(f"http://127.0.0.1:{port}/api/ping", timeout=1) as resp:
            return json.loads(resp.read().decode("utf-8")).get("app") == APP_ID
    except Exception:
        return False


def _make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingAI"
        sys_version = ""
        timeout = 30  # hängende Verbindungen nicht ewig offen halten

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

        def _query(self) -> dict:
            if "?" not in self.path:
                return {}
            out = {}
            for part in self.path.split("?", 1)[1].split("&"):
                k, _, v = part.partition("=")
                out[k] = v
            return out

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
                    return self._json(200, dash.status(self._query().get("range", "7d")))
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
            token = self.headers.get("X-TradingAI-Token", "")
            if not token.isascii() or not secrets.compare_digest(token, dash.token):
                return self._json(403, {"ok": False, "message": "Ungültiges Token – Seite neu laden"})
            try:
                length = max(0, min(int(self.headers.get("Content-Length") or 0), 10_000))
            except ValueError:
                return self._json(400, {"ok": False, "message": "Ungültige Anfrage"})
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
                if not isinstance(body, dict):
                    body = {}
            except (ValueError, UnicodeDecodeError):
                body = {}
            actions = {
                "/api/bot/start": dash.start_bot,
                "/api/bot/stop": dash.stop_bot,
                "/api/bot/kill": dash.kill_bot,
                "/api/killswitch/reset": dash.reset_kill_switch,
                "/api/config/open": dash.open_config,
                "/api/settings": lambda: dash.save_settings(body),
            }
            path = self.path.split("?", 1)[0]
            if path in actions:
                with dash.actions_lock:  # Aktionen nie gleichzeitig (z.B. zwei Tabs)
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
