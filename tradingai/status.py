"""Live-Status des Bots für das Dashboard.

Der Bot schreibt in seinen state-Ordner:
- heartbeat.json      alle paar Sekunden ein Lebenszeichen (auch während er lernt)
- status.json         Momentaufnahme: Konto, offene Positionen, Risiko, Nachrichten
- equity_history.csv  Verlauf von Kontostand und Equity

Das Dashboard liest nur diese Dateien und kann über stop.request einen sauberen
Stopp anfordern. So bleiben Bot und Dashboard getrennte Prozesse: Schließt man das
Dashboard, handelt der Bot ungestört weiter.

Ob wirklich ein Bot läuft, entscheidet im Zweifel die Sperrdatei bot.lock: Das
Betriebssystem gibt sie frei, sobald der Prozess endet – auch nach Absturz oder
Neustart des PCs. Prozessnummern (PIDs) werden von Windows wiederverwendet und
sind daher kein verlässliches Zeichen.
"""

import csv
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import PROCESS_START
from .state import replace_with_retry

log = logging.getLogger(__name__)

HEARTBEAT = "heartbeat.json"
STATUS = "status.json"
EQUITY = "equity_history.csv"
STOP_REQUEST = "stop.request"
LOCK = "bot.lock"

# Phasen, in denen der Bot läuft, aber MetaTrader 5 (noch) nicht erreicht
WAITING_PHASES = ("wartet auf MetaTrader 5", "verbindet mit MetaTrader 5", "Verbindungsproblem")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    replace_with_retry(tmp, path)


def read_json(path: Path):
    for _ in range(3):  # unter Windows kann die Datei kurz gesperrt sein, während der Bot sie ersetzt
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.05)
    return None


class InstanceLock:
    """Verhindert, dass zwei Bots (oder Bot und `train`) gleichzeitig denselben state-Ordner benutzen.
    Das Betriebssystem gibt die Sperre frei, sobald der Prozess endet."""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / LOCK
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True

    def release(self) -> None:
        if self.fh is not None:
            self.fh.close()  # Sperre wird mit dem Schließen bzw. Prozessende freigegeben
            self.fh = None


def lock_held(state_dir: str | Path) -> bool:
    """True, wenn gerade ein Bot (oder `train`) den state-Ordner benutzt."""
    probe = InstanceLock(state_dir)
    if probe.acquire():
        probe.release()
        return False
    return True


def pid_alive(pid) -> bool:
    """Prüft, ob ein Prozess noch läuft – ohne ihn zu stören (nur als Zusatzinfo).
    Achtung: os.kill(pid, 0) würde unter Windows den Prozess BEENDEN, daher ctypes."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        # Eigene Prototypen mit 64-Bit-HANDLE (nicht die globalen windll-Einstellungen verändern)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # Zugriff verweigert: Prozess existiert
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class BotStatus:
    """Schreibt Herzschlag und Momentaufnahmen. Ein Hintergrund-Thread hält den
    Herzschlag aktuell, auch wenn ein Durchlauf (z.B. Lernen) länger dauert."""

    def __init__(self, state_dir: str | Path, mode: str, interval: float = 5.0, equity_every: float = 300.0):
        self.dir = Path(state_dir)
        self.mode = mode
        self.interval = interval
        self.equity_every = equity_every
        self.phase = "startet"
        self.last_error = ""
        self.last_loop: str | None = None
        self.started_at = iso(utcnow())
        # Vom Dashboard gesetzt, damit es "seinen" Bot sicher erkennt (unter Windows ist die
        # PID des gestarteten Prozesses nicht die des eigentlichen Python-Interpreters).
        self.launch_id = os.environ.get("TRADINGAI_LAUNCH_ID", "")
        try:
            self.spawned_at = float(os.environ.get("TRADINGAI_SPAWNED_AT", "") or PROCESS_START)
        except ValueError:
            self.spawned_at = PROCESS_START
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_equity: tuple | None = None

    # -------------------------------------------------------------- Herzschlag
    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        # Nur eine Stopp-Anfrage aus einem FRÜHEREN Lauf verwerfen. Eine Anfrage, die nach
        # dem Start dieses Bots kam (z.B. "Bot stoppen" während er noch lädt), gilt.
        path = self.dir / STOP_REQUEST
        try:
            if path.stat().st_mtime < self.spawned_at - 1.0:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("Alte Stopp-Anfrage nicht lesbar: %s", exc)
        self.beat()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._halt.wait(self.interval):
            self.beat()

    def _payload(self, stopped: bool, phase: str, error: bool = False) -> dict:
        return {
            "pid": os.getpid(), "ppid": os.getppid(), "launch_id": self.launch_id,
            "time": iso(utcnow()), "started_at": self.started_at, "phase": phase,
            "mode": self.mode, "stopped": stopped, "error": error,
            "last_error": self.last_error, "last_loop": self.last_loop,
        }

    def beat(self) -> None:
        try:
            write_json_atomic(self.dir / HEARTBEAT, self._payload(False, self.phase))
        except OSError as exc:
            log.debug("Herzschlag konnte nicht geschrieben werden: %s", exc)

    def stop(self, reason: str = "beendet", error: bool = False) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self.interval)
        try:
            write_json_atomic(self.dir / HEARTBEAT, self._payload(True, reason, error))
        except OSError as exc:
            log.debug("Stopp-Status konnte nicht geschrieben werden: %s", exc)

    def stop_requested(self) -> bool:
        path = self.dir / STOP_REQUEST
        if not path.exists():
            return False
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # gesperrt – egal, die Anfrage gilt trotzdem
        return True

    # ---------------------------------------------------------- Momentaufnahme
    def write_snapshot(self, snapshot: dict) -> None:
        write_json_atomic(self.dir / STATUS, snapshot)

    def record_equity(self, balance: float, equity: float, when: datetime | None = None) -> bool:
        """Alle `equity_every` Sekunden und bei jeder Änderung des Kontostands einen Punkt speichern."""
        when = when or utcnow()
        last = self._last_equity
        if last is not None and (when - last[0]).total_seconds() < self.equity_every and abs(balance - last[1]) < 1e-9:
            return False
        path = self.dir / EQUITY
        new = not path.exists()
        try:
            with path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if new:
                    writer.writerow(["time", "balance", "equity"])
                writer.writerow([iso(when), round(balance, 2), round(equity, 2)])
        except PermissionError:
            return False  # z.B. in Excel geöffnet – nächster Versuch beim nächsten Durchlauf
        self._last_equity = (when, balance)
        return True


def request_stop(state_dir: str | Path) -> None:
    path = Path(state_dir) / STOP_REQUEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(iso(utcnow()), encoding="utf-8")


def read_bot_state(state_dir: str | Path, stale_after: float = 45.0, stall_after: float = 900.0,
                   now: datetime | None = None) -> dict:
    """Ermittelt aus Herzschlag und Sperrdatei, ob und wie der Bot läuft."""
    now = now or utcnow()
    state_dir = Path(state_dir)
    hb = read_json(state_dir / HEARTBEAT)
    if not hb:
        if lock_held(state_dir):
            return {"state": "starting", "label": "Startet …", "phase": "", "stop_pending": False}
        return {"state": "never", "label": "Noch nie gestartet", "phase": "", "stop_pending": False}
    seen = parse_iso(hb.get("time"))
    age = (now - seen).total_seconds() if seen else None
    out = {
        "pid": hb.get("pid"), "launch_id": hb.get("launch_id", ""), "phase": hb.get("phase", ""),
        "mode": hb.get("mode", ""), "started_at": hb.get("started_at"), "last_seen": hb.get("time"),
        "last_loop": hb.get("last_loop"), "last_error": hb.get("last_error", ""),
        "age_seconds": None if age is None else round(age, 1),
        "stop_pending": (state_dir / STOP_REQUEST).exists(),
    }
    if hb.get("stopped"):
        if hb.get("error"):
            out.update(state="stopped_error", label="Mit Fehler beendet")
        else:
            out.update(state="stopped", label="Gestoppt")
        out["stop_pending"] = False
    elif age is not None and age <= stale_after:
        phase = out["phase"] or ""
        progress = parse_iso(hb.get("last_loop")) or parse_iso(hb.get("started_at"))
        if phase.startswith(WAITING_PHASES):
            out.update(state="waiting", label="Wartet auf MetaTrader 5")
        elif progress is not None and (now - progress).total_seconds() > stall_after:
            out.update(state="stalled", label="Hängt")
        else:
            out.update(state="running", label="Läuft")
    elif lock_held(state_dir):
        out.update(state="unresponsive", label="Reagiert nicht")
    else:
        # Kein Bot hält die Sperre: Er wurde hart beendet (Absturz, Neustart, Task-Manager)
        out.update(state="crashed", label="Unerwartet beendet", stop_pending=False)
        try:
            (state_dir / STOP_REQUEST).unlink(missing_ok=True)
        except OSError:
            pass
    return out
