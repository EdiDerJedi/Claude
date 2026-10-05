"""Wirtschaftskalender: keine neuen Trades kurz vor/nach wichtigen Nachrichten (z.B. NFP, Zinsentscheid)."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)


@dataclass
class CalendarEvent:
    time: datetime  # UTC, zeitzonenbewusst
    currency: str
    impact: str
    title: str


def parse_events(data: list) -> list:
    events = []
    for row in data or []:
        try:
            when = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        events.append(
            CalendarEvent(
                time=when.astimezone(timezone.utc),
                currency=str(row.get("country", "")).upper(),
                impact=str(row.get("impact", "")),
                title=str(row.get("title", "")),
            )
        )
    return events


class EconomicCalendar:
    def __init__(self, url: str, impacts=("High",), before_minutes: float = 30, after_minutes: float = 30,
                 refresh_hours: float = 1.0, fetch=None):
        self.url = url
        self.impacts = {i.lower() for i in impacts}
        self.before = timedelta(minutes=before_minutes)
        self.after = timedelta(minutes=after_minutes)
        self.refresh = timedelta(hours=refresh_hours)
        self._fetch = fetch or self._http_fetch
        self.events: list = []
        self.last_refresh: datetime | None = None

    def _http_fetch(self) -> list:
        resp = requests.get(self.url, timeout=10, headers={"User-Agent": "Mozilla/5.0 TradingAI"})
        resp.raise_for_status()
        return resp.json()

    def update(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self.last_refresh and now - self.last_refresh < self.refresh:
            return
        self.last_refresh = now
        try:
            self.events = parse_events(self._fetch())
            log.info("Wirtschaftskalender: %d Termine geladen", len(self.events))
        except Exception as exc:
            log.warning("Wirtschaftskalender nicht verfügbar: %s", exc)

    def blocking_event(self, currencies, now: datetime | None = None) -> CalendarEvent | None:
        now = now or datetime.now(timezone.utc)
        wanted = {c.upper() for c in currencies}
        for ev in self.events:
            if ev.currency in wanted and ev.impact.lower() in self.impacts:
                if ev.time - self.before <= now <= ev.time + self.after:
                    return ev
        return None

    def upcoming(self, currencies, now: datetime | None = None, hours: float = 24) -> list:
        now = now or datetime.now(timezone.utc)
        wanted = {c.upper() for c in currencies}
        horizon = now + timedelta(hours=hours)
        return [e for e in self.events if e.currency in wanted and now <= e.time <= horizon]
