"""Live-Status des Bots für das Dashboard.

Der Bot schreibt in seinen state-Ordner:
- heartbeat.json      alle paar Sekunden ein Lebenszeichen (auch während er lernt)
- status.json         Momentaufnahme: Konto, offene Positionen, Risiko, Nachrichten
- equity_history.csv  Verlauf von Kontostand und Equity

Das Dashboard liest nur diese Dateien und kann über stop.request einen sauberen
Stopp anfordern. So bleiben Bot und Dashboard getrennte Prozesse: Schließt man das
Dashboard, handelt der Bot ungestört weiter.
"""

import csv
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .state import replace_with_retry

log = logging.getLogger(__name__)

HEARTBEAT = "heartbeat.json"
STATUS = "status.json"
EQUITY = "equity_history.csv"
STOP_REQUEST = "stop.request"


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
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    """Prüft, ob ein Prozess noch läuft – ohne ihn zu stören.
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
            # Zugriff verweigert heißt: Prozess existiert (gehört nur jemand anderem)
            return ctypes.get_last_error() == 5
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
        self.started_at = iso(utcnow())
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_equity: tuple | None = None

    # -------------------------------------------------------------- Herzschlag
    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        # Eine liegengebliebene Stopp-Anfrage darf den neuen Bot nicht sofort beenden
        (self.dir / STOP_REQUEST).unlink(missing_ok=True)
        self.beat()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._halt.wait(self.interval):
            self.beat()

    def beat(self) -> None:
        try:
            write_json_atomic(self.dir / HEARTBEAT, {
                "pid": os.getpid(), "time": iso(utcnow()), "started_at": self.started_at,
                "phase": self.phase, "mode": self.mode, "stopped": False,
            })
        except OSError as exc:
            log.debug("Herzschlag konnte nicht geschrieben werden: %s", exc)

    def stop(self, reason: str = "beendet") -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self.interval)
        try:
            write_json_atomic(self.dir / HEARTBEAT, {
                "pid": os.getpid(), "time": iso(utcnow()), "started_at": self.started_at,
                "phase": reason, "mode": self.mode, "stopped": True,
            })
        except OSError as exc:
            log.debug("Stopp-Status konnte nicht geschrieben werden: %s", exc)

    def stop_requested(self) -> bool:
        path = self.dir / STOP_REQUEST
        if path.exists():
            path.unlink(missing_ok=True)
            return True
        return False

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


def read_bot_state(state_dir: str | Path, stale_after: float = 45.0, now: datetime | None = None) -> dict:
    """Ermittelt aus dem Herzschlag, ob der Bot läuft."""
    now = now or utcnow()
    hb = read_json(Path(state_dir) / HEARTBEAT)
    if not hb:
        return {"state": "never", "label": "Noch nie gestartet"}
    seen = parse_iso(hb.get("time"))
    age = (now - seen).total_seconds() if seen else None
    out = {
        "pid": hb.get("pid"), "phase": hb.get("phase", ""), "mode": hb.get("mode", ""),
        "started_at": hb.get("started_at"), "last_seen": hb.get("time"),
        "age_seconds": None if age is None else round(age, 1),
        "stop_pending": (Path(state_dir) / STOP_REQUEST).exists(),
    }
    if hb.get("stopped"):
        out.update(state="stopped", label="Gestoppt")
    elif age is not None and age <= stale_after:
        out.update(state="running", label="Läuft")
    elif pid_alive(hb.get("pid")):
        out.update(state="unresponsive", label="Reagiert nicht")
    else:
        out.update(state="crashed", label="Unerwartet beendet")
    return out
