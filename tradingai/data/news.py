"""Nachrichten aus dem Internet (RSS) und deren Stimmung (Sentiment) je Währung/Markt."""

import csv
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from .symbols import sentiment_legs

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; TradingAI/0.1; +https://github.com)"

ASSET_KEYWORDS = {
    "USD": ["dollar", "usd", "greenback", "fed", "fomc", "powell", "federal reserve", "treasury yields",
            "u.s. economy", "us economy", "nonfarm", "payrolls", "u.s. inflation", "us inflation", "us cpi"],
    "EUR": ["euro", "eur", "ecb", "eurozone", "euro zone", "euro area", "lagarde"],
    "GBP": ["pound", "sterling", "gbp", "bank of england", "boe", "uk economy", "uk inflation"],
    "JPY": ["yen", "jpy", "bank of japan", "boj", "japanese economy"],
    "CHF": ["swiss franc", "chf", "snb", "swiss national bank"],
    "AUD": ["aussie", "australian dollar", "aud", "rba", "reserve bank of australia"],
    "NZD": ["kiwi", "new zealand dollar", "nzd", "rbnz"],
    "CAD": ["loonie", "canadian dollar", "cad", "bank of canada"],
    "XAU": ["gold", "xau", "bullion"],
    "XAG": ["silver", "xag"],
    "OIL": ["oil", "crude", "brent", "wti", "opec"],
    "CRYPTO": ["bitcoin", "btc", "crypto", "ether", "ethereum"],
    "EQ_US": ["wall street", "s&p 500", "s&p", "nasdaq", "dow jones", "us stocks", "u.s. stocks"],
    "EQ_EU": ["dax", "stoxx", "european stocks", "european shares"],
    "EQ_UK": ["ftse", "uk stocks", "british stocks"],
    "EQ_JP": ["nikkei", "japanese stocks", "topix"],
}
ASSETS = list(ASSET_KEYWORDS)

POSITIVE = ["rise", "rises", "rising", "rose", "gain", "gains", "gained", "surge", "surges", "surged", "jump",
            "jumps", "jumped", "rally", "rallies", "rallied", "climb", "climbs", "climbed", "strong", "stronger",
            "strength", "beat", "beats", "upbeat", "optimism", "boost", "boosts", "record high", "hawkish",
            "rate hike", "hikes", "higher", "upgrade", "robust", "expansion", "bullish", "firmer", "soar",
            "soars", "rebound", "rebounds", "recovery", "outperform", "advance", "advances"]
NEGATIVE = ["fall", "falls", "fell", "falling", "drop", "drops", "dropped", "plunge", "plunges", "plunged",
            "slump", "slumps", "slide", "slides", "decline", "declines", "declined", "weak", "weaker",
            "weakness", "miss", "misses", "downbeat", "pessimism", "dovish", "rate cut", "cuts", "lower",
            "downgrade", "recession", "contraction", "bearish", "softer", "tumble", "tumbles", "sink",
            "sinks", "crisis", "selloff", "sell-off", "slowdown", "underperform", "retreat", "retreats"]


def _phrase_regex(words) -> re.Pattern:
    alts = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"(?<![a-z])(" + "|".join(alts) + r")(?![a-z])")


POS_RE = _phrase_regex(POSITIVE)
NEG_RE = _phrase_regex(NEGATIVE)
ASSET_RE = {a: _phrase_regex(k) for a, k in ASSET_KEYWORDS.items()}
PAIR_RE = re.compile(r"\b([a-z]{3})/([a-z]{3})\b")
OTHER_DOLLARS = re.compile(r"(australian|canadian|new zealand|hong kong|singapore|taiwan) dollar")


@dataclass
class NewsItem:
    title: str
    summary: str
    published: datetime | None
    source: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary}"

    @property
    def key(self) -> str:
        return hashlib.sha1(self.title.strip().lower().encode()).hexdigest()


# ------------------------------------------------------------------ RSS lesen
def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(xml_text: str, source: str = "") -> list:
    """Unterstützt RSS 2.0 und Atom."""
    root = ET.fromstring(xml_text)
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {}
        for child in el:
            ctag = child.tag.split("}")[-1]
            fields.setdefault(ctag, (child.text or "").strip())
        title = _strip_html(fields.get("title", ""))
        if not title:
            continue
        summary = _strip_html(fields.get("description") or fields.get("summary") or fields.get("content", ""))
        published = _parse_date(fields.get("pubDate") or fields.get("published") or fields.get("updated")
                                or fields.get("date"))
        items.append(NewsItem(title, summary[:500], published, source))
    return items


def fetch_feeds(urls, timeout: float = 10.0, session: requests.Session | None = None) -> list:
    session = session or requests.Session()
    items, seen = [], set()
    for url in urls:
        try:
            resp = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            for item in parse_feed(resp.text, source=url):
                if item.key not in seen:
                    seen.add(item.key)
                    items.append(item)
        except Exception as exc:
            log.warning("News-Feed %s nicht lesbar: %s", url, exc)
    return items


# ------------------------------------------------------------- Sentiment
def _recency_weight(item: NewsItem, now: datetime, half_life_hours: float) -> float:
    if item.published is None:
        return 0.5
    age_h = max((now - item.published).total_seconds() / 3600.0, 0.0)
    return 0.5 ** (age_h / half_life_hours)


class LexiconSentiment:
    """Schnelle, kostenlose Stimmungsanalyse über Schlüsselwörter."""

    def __init__(self, half_life_hours: float = 6.0):
        self.half_life_hours = half_life_hours

    @staticmethod
    def polarity(text: str) -> float:
        t = text.lower()
        pos, neg = len(POS_RE.findall(t)), len(NEG_RE.findall(t))
        return 0.0 if pos + neg == 0 else (pos - neg) / (pos + neg)

    def item_scores(self, item: NewsItem) -> dict:
        """Sentiment-Beitrag einer Nachricht je Asset."""
        text = item.text.lower()
        pol = self.polarity(item.title) or self.polarity(item.text)
        if pol == 0:
            return {}
        out = {}
        # "EUR/USD rises" -> gut für EUR, schlecht für USD
        for base, quote in PAIR_RE.findall(text):
            b, q = base.upper(), quote.upper()
            if b in ASSET_KEYWORDS and q in ASSET_KEYWORDS:
                out[b] = out.get(b, 0.0) + pol
                out[q] = out.get(q, 0.0) - pol
        if out:
            return out
        for asset, regex in ASSET_RE.items():
            hay = OTHER_DOLLARS.sub(" ", text) if asset == "USD" else text
            if regex.search(hay):
                out[asset] = pol
        return out

    def analyze(self, items, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        num = {a: 0.0 for a in ASSETS}
        den = {a: 0.0 for a in ASSETS}
        for item in items:
            w = _recency_weight(item, now, self.half_life_hours)
            for asset, s in self.item_scores(item).items():
                num[asset] += w * s
                den[asset] += w
        # +1 im Nenner: wenige Nachrichten -> Wert nahe 0 (wenig Gewissheit)
        return {a: max(-1.0, min(1.0, num[a] / (den[a] + 1.0))) for a in ASSETS if den[a] > 0}


SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset": {"type": "string", "enum": ASSETS},
                    "score": {"type": "number"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["asset", "score", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sentiments"],
    "additionalProperties": False,
}

CLAUDE_SYSTEM = (
    "You are a macro and FX news analyst. You receive recent financial news headlines. "
    "For each asset that the news materially affects, estimate the short-term (next 1-24 hours) "
    "directional impact on that asset's value: score from -1.0 (strongly bearish) to +1.0 (strongly bullish), "
    "and a confidence from 0.0 to 1.0. Assets are currencies (USD, EUR, GBP, JPY, CHF, AUD, NZD, CAD), "
    "gold (XAU), silver (XAG), crude oil (OIL), crypto (CRYPTO) and equity indices "
    "(EQ_US, EQ_EU, EQ_UK, EQ_JP). Hawkish central bank news is bullish for that currency, dovish is bearish. "
    "For currency pairs like 'EUR/USD rises', the base currency strengthens and the quote currency weakens. "
    "Only include assets with a clear signal; omit everything else. Keep each reason under 20 words."
)


class ClaudeSentiment:
    """Stimmungsanalyse mit Claude (benötigt ANTHROPIC_API_KEY). Fällt bei Fehlern auf das Lexikon zurück."""

    def __init__(self, model: str = "claude-opus-5-5", max_items: int = 60, client=None,
                 fallback: LexiconSentiment | None = None):
        self.model = model
        self.max_items = max_items
        self.fallback = fallback or LexiconSentiment()
        self._client = client
        self._cache_key = None
        self._cache_result: dict = {}

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _prompt(self, items, now: datetime) -> str:
        lines = []
        for item in items:
            age = ""
            if item.published is not None:
                hours = (now - item.published).total_seconds() / 3600.0
                age = f"[{hours:.1f}h ago] "
            lines.append(f"- {age}{item.title}" + (f" — {item.summary[:200]}" if item.summary else ""))
        return "Current time (UTC): " + now.strftime("%Y-%m-%d %H:%M") + "\n\nHeadlines:\n" + "\n".join(lines)

    def analyze(self, items, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        items = sorted(items, key=lambda i: i.published or now, reverse=True)[: self.max_items]
        if not items:
            return {}
        key = hashlib.sha1("".join(i.key for i in items).encode()).hexdigest()
        if key == self._cache_key:
            return self._cache_result  # keine neuen Nachrichten -> keine neuen API-Kosten
        try:
            result = self._ask_claude(items, now)
        except Exception as exc:
            log.warning("Claude-Analyse fehlgeschlagen (%s) – nutze Lexikon", exc)
            return self.fallback.analyze(items, now)
        self._cache_key, self._cache_result = key, result
        return result

    def _ask_claude(self, items, now: datetime) -> dict:
        import anthropic

        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SENTIMENT_SCHEMA}},
                system=CLAUDE_SYSTEM,
                messages=[{"role": "user", "content": self._prompt(items, now)}],
            )
        except anthropic.RateLimitError as exc:
            raise RuntimeError("Rate-Limit erreicht") from exc
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"API-Fehler {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise RuntimeError("keine Verbindung zur Claude-API") from exc

        if response.stop_reason == "refusal":
            raise RuntimeError("Anfrage wurde abgelehnt")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Antwort abgeschnitten (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise RuntimeError("leere Antwort")
        data = json.loads(text)
        out = {}
        for entry in data.get("sentiments", []):
            asset = entry.get("asset")
            if asset not in ASSET_KEYWORDS:
                continue
            score = max(-1.0, min(1.0, float(entry.get("score", 0.0))))
            conf = max(0.0, min(1.0, float(entry.get("confidence", 0.0))))
            out[asset] = score * conf
            log.debug("Claude: %s %+.2f (%.0f%%) – %s", asset, score, conf * 100, entry.get("reason", ""))
        return out


# --------------------------------------------------------------- Monitor
class NewsMonitor:
    """Lädt regelmäßig Nachrichten, berechnet Sentiment und protokolliert es als Lerndaten."""

    def __init__(self, feeds, analyzer, refresh_minutes: float = 15.0, max_age_hours: float = 12.0,
                 log_path: str | Path | None = None, fetcher=fetch_feeds):
        self.feeds = list(feeds)
        self.analyzer = analyzer
        self.refresh = timedelta(minutes=refresh_minutes)
        self.max_age = timedelta(hours=max_age_hours)
        self.log_path = Path(log_path) if log_path else None
        self.fetcher = fetcher
        self.last_refresh: datetime | None = None
        self.items: list = []
        self.sentiment: dict = {}

    def update(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if self.last_refresh and now - self.last_refresh < self.refresh:
            return False
        self.last_refresh = now
        fresh = self.fetcher(self.feeds)
        if not fresh:
            return False
        self.items = [i for i in fresh if i.published is None or now - i.published <= self.max_age]
        self.sentiment = self.analyzer.analyze(self.items, now)
        self._log(now)
        log.info("News: %d Meldungen, Sentiment: %s", len(self.items),
                 ", ".join(f"{a} {s:+.2f}" for a, s in sorted(self.sentiment.items(), key=lambda x: -abs(x[1]))[:6])
                 or "neutral")
        return True

    def pair_sentiment(self, symbol: str) -> float | None:
        base, quote = sentiment_legs(symbol)
        if base is None:
            return None
        value = self.sentiment.get(base, 0.0) - (self.sentiment.get(quote, 0.0) if quote else 0.0)
        return max(-1.0, min(1.0, value))

    def _log(self, now: datetime) -> None:
        """Speichert jede Messung – so entsteht eine Historie, die später als Lernmerkmal dienen kann."""
        if not self.log_path or not self.sentiment:
            return
        new = not self.log_path.exists()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(["time", "asset", "score", "items"])
            for asset, score in sorted(self.sentiment.items()):
                writer.writerow([now.isoformat(), asset, f"{score:.4f}", len(self.items)])


def make_analyzer(kind: str, model: str):
    if kind == "claude":
        return ClaudeSentiment(model=model)
    return LexiconSentiment()
