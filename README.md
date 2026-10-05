# TradingAI – selbstlernender Trading-Bot für MetaTrader 5

Ein Python-Bot, der sich mit deinem **MetaTrader 5** verbindet und selbstständig handelt.
Er **lernt laufend dazu**, holt sich **Daten aus dem Internet** (Kurse, andere Märkte,
Nachrichten, Wirtschaftskalender), **wechselt zwischen Strategien** und **passt seine
Parameter an**, wenn sich der Markt verändert.

> ⚠️ **Wichtig – bitte lesen.** Trading mit Hebelprodukten (Forex, CFDs, Gold …) ist sehr
> riskant; die meisten Privatanleger verlieren Geld. **Keine KI kann Gewinne garantieren** –
> auch eine selbstlernende nicht. Märkte sind zum größten Teil Zufall, und was in der
> Vergangenheit funktioniert hat, kann morgen versagen. Gute Ergebnisse mit synthetischen
> Testdaten sagen **nichts** über echte Märkte aus. Nutze den Bot zuerst im Backtest, dann
> wochenlang im Paper-Modus und auf einem **Demokonto**. Echtes Geld nur, das du komplett
> verlieren kannst. Der Bot ist standardmäßig so eingestellt, dass er **Echtgeldkonten
> ablehnt**.

---

## Inhalt

1. [Was der Bot macht](#was-der-bot-macht)
2. [Wie die KI lernt](#wie-die-ki-lernt)
3. [Daten aus dem Internet](#daten-aus-dem-internet)
4. [Sicherheit und Risikomanagement](#sicherheit-und-risikomanagement)
5. [Installation](#installation)
6. [Schritt für Schritt zum ersten Trade](#schritt-für-schritt-zum-ersten-trade)
7. [Befehle](#befehle)
8. [Konfiguration](#konfiguration)
9. [Dateien im state-Ordner](#dateien-im-state-ordner)
10. [Projektstruktur und Tests](#projektstruktur-und-tests)
11. [Grenzen und Ideen](#grenzen-und-ideen)

---

## Was der Bot macht

Bei jeder neuen Kerze (z. B. jede Stunde bei `H1`) läuft für jedes Symbol dieser Ablauf:

```
Kurse holen ──► 4 Strategien geben Signale ──► Lernen aus dem letzten Ergebnis
     │                                                    │
     │                         (wöchentlich) Parameter optimieren, ML-Modell neu trainieren
     ▼                                                    ▼
Konsens bilden ──► Filter: Risiko, Spread, Wirtschaftstermine, Nachrichtenlage
     │
     ▼
Order an MetaTrader 5 (immer mit Stop-Loss und Take-Profit)
```

Die vier Strategien:

| Strategie        | Idee                                                                 |
|------------------|----------------------------------------------------------------------|
| `trend`          | Trendfolge: schneller vs. langsamer gleitender Durchschnitt + ADX-Filter |
| `mean_reversion` | Gegenbewegung nach starker Übertreibung (Z-Score), nur in ruhigen Märkten |
| `breakout`       | Ausbruch aus dem Donchian-Kanal (Turtle-Prinzip)                    |
| `ml`             | Gradient-Boosting-Modell, das aus über 20 Merkmalen die Richtung schätzt |

Derselbe Code läuft im **Backtest**, im **Paper-Trading** (Simulation mit Live-Kursen) und
**live** mit MetaTrader 5. Was im Backtest funktioniert, funktioniert also technisch genauso live.

## Wie die KI lernt

Der Bot lernt auf drei Ebenen:

**1. Laufend: Welche Strategie passt gerade? (Online-Lernen)**
Jede Strategie wird für jedes Symbol „im Schatten“ mitgehandelt – auch wenn ihr Signal nicht
ausgeführt wird. Nach jeder Kerze wird verbucht, wie viel jede Strategie verdient oder verloren
hätte (inkl. Spread-Kosten). Daraus entsteht ein Qualitäts-Score (ähnlich einer Sharpe-Ratio).
Ältere Ergebnisse verlieren mit jeder Kerze an Gewicht (`learning.decay`), so dass sich der Bot
an neue Marktphasen anpasst: Läuft der Markt im Trend, bekommt `trend` mehr Gewicht; wird er
seitwärts, übernimmt `mean_reversion`. Laufen alle Strategien schlecht, gewinnt die eingebaute
Option **„cash“** – der Bot bleibt dann einfach draußen.

**2. Wöchentlich: Bessere Parameter finden (Walk-Forward-Optimierung)**
Für jede Regel-Strategie werden viele Parameterkombinationen auf älteren Daten getestet. Die
besten werden dann auf den neuesten Daten geprüft, die sie vorher **nie gesehen** haben.
Neue Parameter werden **nur übernommen, wenn sie dort klar besser** sind. Das verhindert, dass
sich der Bot an Zufallsmuster „überanpasst“.

**3. Wöchentlich: ML-Modell neu trainieren**
Das Modell lernt aus Kursmustern, Volatilität, Uhrzeit **und Daten anderer Märkte** (VIX,
S&P 500, Dollar-Index, US-Zinsen). Es wird zeitlich korrekt validiert (älteste 75 % zum Lernen,
neueste 25 % zum Prüfen, mit Sicherheitslücke dazwischen). Ein neues Modell wird nur aktiv,
wenn es in der Prüfung **besser als Zufall** war. Außerdem werden ML-Signale auf Daten, die das
Modell schon im Training gesehen hat, nicht bewertet – so wird es nicht schöngerechnet.

Alles Gelernte wird im Ordner `state/` gespeichert und nach einem Neustart weiterverwendet.

## Daten aus dem Internet

| Quelle | Wofür | Kosten |
|--------|-------|--------|
| **Yahoo Finance** | Kurse für Paper-Trading/Backtests, Kontextmärkte (VIX, S&P 500, DXY, US10Y) als Lernmerkmale | kostenlos |
| **RSS-Nachrichten** (FXStreet, CNBC, Yahoo, Fed, EZB – frei konfigurierbar) | Nachrichten-Stimmung je Währung; Einstiege gegen eine klar negative Nachrichtenlage werden blockiert | kostenlos |
| **Wirtschaftskalender** (ForexFactory-Feed) | keine neuen Trades 30 min vor/nach wichtigen Terminen (z. B. US-Arbeitsmarktdaten, Zinsentscheide) | kostenlos |
| **Claude (optional)** | bessere Analyse der Nachrichten statt Wortliste (`internet.news_analyzer: claude`) | API-Kosten, braucht `ANTHROPIC_API_KEY` |

Jede Nachrichten-Messung wird in `state/sentiment_log.csv` gespeichert – so entsteht mit der
Zeit eine eigene Historie, die später als zusätzliches Lernmerkmal genutzt werden kann.
Fällt eine Internetquelle aus, läuft der Bot ohne sie weiter.

## Sicherheit und Risikomanagement

- **Echtgeld-Sperre:** Im Live-Modus prüft der Bot den Kontotyp. Bei einem Echtgeldkonto
  bricht er ab, solange `mt5.allow_real_account: false` ist.
- **Jeder Trade hat einen Stop-Loss** (Standard: 2 × ATR) und einen Take-Profit (3 × ATR).
- **Positionsgröße nach Risiko:** Ein Stop-Loss-Treffer kostet `risk_per_trade` (Standard 1 %)
  des Kapitals. Ist das Konto zu klein für das Mindestlot, wird nicht gehandelt.
- **Tagesverlust-Limit:** Ab 3 % Tagesverlust keine neuen Trades bis zum nächsten Tag.
- **Not-Aus:** Fällt das Kapital 15 % unter den Höchststand, schließt der Bot alle seine
  Positionen und handelt nicht mehr, bis du ihn mit `reset-killswitch` freigibst.
- **Max. offene Positionen**, optionaler **Spread-Filter** und **Trailing-Stop**.
- **Manuelle Trades werden nie angefasst** – der Bot erkennt seine Orders an der `magic`-Nummer.

## Installation

Voraussetzungen: **Python 3.10 oder neuer**.
Für den Live-Handel zusätzlich **Windows** und das **MetaTrader-5-Terminal** (das offizielle
Python-Paket `MetaTrader5` gibt es nur für Windows). Backtests und Paper-Trading laufen auch
unter Linux und macOS.

```bash
git clone https://github.com/EdiDerJedi/Claude.git tradingai
cd tradingai
python -m venv .venv
.venv\Scripts\activate          # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy config.example.yaml config.yaml   # Linux/macOS: cp config.example.yaml config.yaml
```

### MetaTrader 5 vorbereiten (nur für `mode: live`)

1. MetaTrader 5 von deinem Broker installieren und ein **Demokonto** eröffnen
   (Datei → Ein Konto eröffnen).
2. Einloggen. In **Extras → Optionen → Expert Advisors** den Haken bei
   **„Algorithmischen Handel erlauben“** setzen und oben in der Symbolleiste
   **„Algo Trading“** einschalten (grün).
3. Die Symbole, die du handeln willst, im Fenster **Marktübersicht** einblenden. Trage die
   Namen **genau so** in `config.yaml` ein (manche Broker nutzen Endungen wie `EURUSD.m`).
4. Das Terminal muss laufen, solange der Bot handelt. Login-Daten in `config.yaml` sind
   optional – ohne sie nutzt der Bot das im Terminal eingeloggte Konto.

## Schritt für Schritt zum ersten Trade

**1. Backtest mit synthetischen Daten** (funktioniert offline, prüft die Installation):

```bash
python -m tradingai backtest --source synthetic --bars 3000
```

**2. Backtest mit echten Kursen von Yahoo** (ca. 2 Jahre H1-Daten):

```bash
python -m tradingai backtest --source yahoo
python -m tradingai backtest --source yahoo --no-learn   # zum Vergleich: ohne Lernen
```

Ergebnisse (Kapitalkurve, Trades, Gelerntes) landen im Ordner `reports/`. Ein Backtest mit
Lernen dauert je Symbol und Jahr H1-Daten einige Minuten.

**3. Paper-Trading** (`mode: paper`) – simuliert mit Live-Kursen, ohne Broker:

```bash
python -m tradingai train   # einmal vorab lernen (optional, passiert sonst beim Start)
python -m tradingai run
```

**4. Demokonto über MetaTrader 5** – in `config.yaml` `mode: live` setzen, dann:

```bash
python -m tradingai run
```

Lass den Bot **mehrere Wochen** auf dem Demokonto laufen und vergleiche mit dem Backtest.

**5. Echtgeld** – nur nach erfolgreichen Demo-Wochen: `mt5.allow_real_account: true` und
`risk.risk_per_trade` eher auf `0.005` (0,5 %) senken.

### Dauerhaft laufen lassen

Für 24/5-Betrieb eignet sich ein Windows-VPS. Starte den Bot z. B. über die
**Windows-Aufgabenplanung** bei Systemstart mit
`C:\pfad\.venv\Scripts\python.exe -m tradingai run` (Arbeitsverzeichnis = Projektordner).
Mit `Strg+C` wird der Bot sauber beendet; offene Positionen behalten ihren Stop-Loss.

## Befehle

| Befehl | Beschreibung |
|--------|--------------|
| `python -m tradingai backtest [--source yahoo\|csv\|synthetic] [--symbols EURUSD XAUUSD] [--bars N] [--no-learn] [--no-ml]` | historischer Test inkl. Lernen |
| `python -m tradingai train` | sofort Parameter optimieren und ML-Modell trainieren |
| `python -m tradingai run [--once]` | Bot starten (`mode` aus `config.yaml`) |
| `python -m tradingai status` | Gewichte, gelernte Parameter, Modellqualität, Risiko-Status |
| `python -m tradingai news` | aktuelle Nachrichtenstimmung und wichtige Termine |
| `python -m tradingai reset-killswitch` | Not-Aus nach Prüfung zurücksetzen |

Mit `-c andere.yaml` lässt sich eine andere Konfiguration verwenden.

## Konfiguration

Alle Einstellungen stehen kommentiert in [`config.example.yaml`](config.example.yaml).
Die wichtigsten:

| Einstellung | Standard | Bedeutung |
|-------------|----------|-----------|
| `mode` | `paper` | `paper` = Simulation, `live` = MetaTrader 5 |
| `symbols`, `timeframe` | `[EURUSD, GBPUSD, XAUUSD]`, `H1` | was und auf welcher Zeitebene gehandelt wird |
| `risk.risk_per_trade` | `0.01` | Risiko je Trade (1 %) |
| `risk.max_drawdown` | `0.15` | Not-Aus-Schwelle |
| `learning.decay` | `0.99` | Gedächtnis des Online-Lernens (kleiner = passt sich schneller an) |
| `learning.entry_threshold` | `0.35` | wie einig sich die Strategien für einen Einstieg sein müssen |
| `learning.optimize_every_hours` / `retrain_every_hours` | `168` | wie oft neu gelernt wird |
| `internet.news_analyzer` | `lexicon` | `claude` für KI-Nachrichtenanalyse |
| `paper.data_source` | `yahoo` | Kursquelle für Paper/Backtest |

Eigene Kursdaten (z. B. aus MT5 exportiert: Ansicht → Symbole (Strg+U) → Reiter Balken →
Balken exportieren) als `data/EURUSD_H1.csv` ablegen und `--source csv` verwenden.

**Claude für die Nachrichtenanalyse:** einen API-Key unter
<https://console.anthropic.com> erstellen, als Umgebungsvariable `ANTHROPIC_API_KEY` setzen und
`internet.news_analyzer: claude` einstellen. Der Bot fragt nur, wenn neue Schlagzeilen
erschienen sind; bei Fehlern fällt er automatisch auf die Wortliste zurück.

## Dateien im state-Ordner

| Datei | Inhalt |
|-------|--------|
| `state.json` | gelernte Strategie-Gewichte, Parameter, Modellqualität, Risiko-Status, letzte Entscheidungen |
| `models/*.joblib` | trainierte ML-Modelle je Symbol |
| `trades.csv` | Journal aller geschlossenen Trades |
| `sentiment_log.csv` | Historie der Nachrichtenstimmung |
| `paper_account.json` | Kontostand und Positionen im Paper-Modus |
| `tradingai.log` | Protokoll (rotierend) |

Ordner löschen = Bot fängt mit dem Lernen von vorne an.

## Projektstruktur und Tests

```
tradingai/
  engine.py          Hauptlogik: Signale → Lernen → Entscheidung → Risiko → Order
  learning/          selector.py (Online-Lernen), optimizer.py (Walk-Forward), trainer.py (ML)
  strategies/        trend, mean_reversion, breakout, ml
  brokers/           mt5.py (MetaTrader 5), sim.py (Backtest- und Paper-Broker)
  data/              market_data.py (Yahoo/CSV/synthetisch), news.py, calendar.py, symbols.py
  risk.py            Positionsgröße, Limits, Not-Aus
  features.py        Merkmale für das ML-Modell (ohne Blick in die Zukunft)
  backtest.py, cli.py, config.py, state.py
tests/               automatische Tests (MT5 wird dort nachgebildet)
```

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Die Tests prüfen u. a., dass Indikatoren, Merkmale und Strategien **keine zukünftigen Daten**
verwenden, dass Stop-Loss, Kurslücken und Spread korrekt simuliert werden, dass der Not-Aus
greift und dass die MT5-Orders korrekt aufgebaut werden (gegen ein nachgebautes MT5-Modul).

## Grenzen und Ideen

- Die Simulation nutzt einen festen Spread und Näherungswerte für Tick-Werte; echte
  Ausführung (Slippage, Swap, Kommission) ist schlechter. Live kommen die echten Werte vom Broker.
- Yahoo-Kurse weichen leicht von den Kursen deines Brokers ab und sind verzögert –
  für Paper-Trading reicht das, für den Livehandel nutzt der Bot die MT5-Kurse.
- Nur MetaTrader **5** wird unterstützt (für MT4 gibt es keine offizielle Python-Schnittstelle).
- Erweiterungsideen: weitere Strategien in `tradingai/strategies/` (einfach von `Strategy`
  erben und in `RULE_STRATEGIES` eintragen), Nachrichten-Historie als ML-Merkmal,
  Benachrichtigungen per Telegram.
