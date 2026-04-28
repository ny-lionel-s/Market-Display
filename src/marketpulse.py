#!/usr/bin/env python3
"""
MarketPulse
===========

Dieses Programm ist fuer ein LB3-Projekt mit WSL gebaut.
Es ruft Marktkurse ueber eine API ab, bewertet jeden Wert mit OK/NotOK,
schreibt Log-Dateien, speichert einen CSV-Verlauf, erzeugt ein HTML-Dashboard
und kann bei NotOK automatisch eine E-Mail senden.

Bewusst sichtbar im Code:
- Funktionen: jede Aufgabe ist in eine Funktion ausgelagert.
- Arrays/Listen: assets, currencies, rows, history_rows.
- Schleifen: alle konfigurierten Assets werden automatisch durchlaufen.
- cron-tauglich: das Skript hat keine Eingabeaufforderung und laeuft automatisch.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import logging
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = BASE_DIR / "config.json"
STATUS_OK = "OK"
STATUS_NOTOK = "NotOK"

HISTORY_FIELDS = [
    "timestamp",
    "asset_id",
    "asset_name",
    "symbol",
    "currency",
    "price",
    "change_24h_percent",
    "last_updated",
    "status",
    "reason",
]


def utc_now() -> dt.datetime:
    """Return a timezone-aware UTC timestamp for logs and reports."""

    return dt.datetime.now(dt.timezone.utc)


def load_config(config_path: Path) -> dict[str, Any]:
    """Load and validate the JSON configuration file."""

    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)

    assets = config.get("market", {}).get("assets", [])
    if not assets:
        raise ValueError("No market assets configured in config.json")

    currencies = config.get("market", {}).get("currencies", [])
    if not currencies:
        raise ValueError("No currencies configured in config.json")

    return config


def project_path(path_value: str) -> Path:
    """Resolve relative paths from the project root, not from cron's folder."""

    path = Path(path_value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def configure_logger(log_file: Path) -> logging.Logger:
    """Create a logger that writes a permanent OK/NotOK log file."""

    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("marketpulse")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def build_api_url(config: dict[str, Any]) -> str:
    """Build the CoinGecko API URL from arrays in config.json."""

    market_config = config["market"]
    assets = market_config["assets"]
    currencies = market_config["currencies"]

    coin_ids = ",".join(asset["id"] for asset in assets)
    currency_ids = ",".join(currency.lower() for currency in currencies)

    params = {
        "ids": coin_ids,
        "vs_currencies": currency_ids,
        "include_24hr_change": "true",
        "include_last_updated_at": "true",
    }
    return f"{market_config['api_url']}?{urllib.parse.urlencode(params)}"


def fetch_market_data(config: dict[str, Any]) -> dict[str, Any]:
    """Fetch live market data from the configured API."""

    timeout = int(config["market"].get("timeout_seconds", 15))
    url = build_api_url(config)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "MarketPulse-LB3/1.0",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"API returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def unix_to_iso(value: Any) -> str:
    """Convert an API Unix timestamp to a readable ISO string."""

    if value in (None, ""):
        return ""
    try:
        timestamp = dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
        return timestamp.isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def evaluate_asset(
    asset: dict[str, Any],
    api_data: dict[str, Any],
    primary_currency: str,
    default_threshold: float,
    timestamp: str,
) -> dict[str, Any]:
    """Convert one API result into one OK/NotOK row for logs and dashboard."""

    asset_id = asset["id"]
    asset_data = api_data.get(asset_id, {})
    currency = primary_currency.lower()
    price = asset_data.get(currency)
    change_key = f"{currency}_24h_change"
    change_24h = asset_data.get(change_key)
    last_updated = unix_to_iso(asset_data.get("last_updated_at"))
    threshold = float(asset.get("notok_drop_percent", default_threshold))

    status = STATUS_OK
    reason = "Price fetched successfully"

    if price is None:
        status = STATUS_NOTOK
        reason = "Missing price in API response"
    elif change_24h is None:
        status = STATUS_OK
        reason = "Price fetched; 24h change was not available"
    elif float(change_24h) <= threshold:
        status = STATUS_NOTOK
        reason = f"24h change below threshold ({float(change_24h):.2f}% <= {threshold:.2f}%)"

    return {
        "timestamp": timestamp,
        "asset_id": asset_id,
        "asset_name": asset.get("name", asset_id),
        "symbol": asset.get("symbol", asset_id.upper()),
        "currency": currency.upper(),
        "price": price if price is not None else "",
        "change_24h_percent": change_24h if change_24h is not None else "",
        "last_updated": last_updated,
        "status": status,
        "reason": reason,
    }


def build_error_rows(config: dict[str, Any], timestamp: str, error: Exception) -> list[dict[str, Any]]:
    """Create NotOK rows when the API call itself failed."""

    primary_currency = config["market"].get("primary_currency", "chf").upper()
    rows = []
    for asset in config["market"]["assets"]:
        rows.append(
            {
                "timestamp": timestamp,
                "asset_id": asset["id"],
                "asset_name": asset.get("name", asset["id"]),
                "symbol": asset.get("symbol", asset["id"].upper()),
                "currency": primary_currency,
                "price": "",
                "change_24h_percent": "",
                "last_updated": "",
                "status": STATUS_NOTOK,
                "reason": f"API call failed: {error}",
            }
        )
    return rows


def evaluate_market(config: dict[str, Any], api_data: dict[str, Any], timestamp: str) -> list[dict[str, Any]]:
    """Evaluate all configured assets by looping through the asset list."""

    market_config = config["market"]
    primary_currency = market_config.get("primary_currency", "chf").lower()
    default_threshold = float(market_config.get("default_notok_drop_percent", -6.0))

    rows = []
    for asset in market_config["assets"]:
        rows.append(evaluate_asset(asset, api_data, primary_currency, default_threshold, timestamp))
    return rows


def append_history(history_file: Path, rows: list[dict[str, Any]]) -> None:
    """Append all current rows to a CSV file for the dashboard history."""

    history_file.parent.mkdir(parents=True, exist_ok=True)
    file_exists = history_file.exists()

    with history_file.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS)
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def log_rows(logger: logging.Logger, rows: list[dict[str, Any]]) -> None:
    """Write one clear OK/NotOK line per asset into the log file."""

    for row in rows:
        message = (
            f"{row['status']} asset={row['symbol']} price={row['price']} "
            f"currency={row['currency']} change24h={row['change_24h_percent']} "
            f"reason={row['reason']}"
        )
        if row["status"] == STATUS_OK:
            logger.info(message)
        else:
            logger.error(message)


def read_history(history_file: Path, limit: int) -> list[dict[str, str]]:
    """Read recent CSV rows so the dashboard can show a price history chart."""

    if not history_file.exists():
        return []

    with history_file.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    return rows[-limit:]


def to_float(value: Any) -> float | None:
    """Convert CSV/API values to float for charts and formatting."""

    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_price(value: Any) -> str:
    """Format prices nicely for the HTML dashboard."""

    number = to_float(value)
    if number is None:
        return "n/a"
    if number >= 100:
        return f"{number:,.2f}"
    return f"{number:,.4f}"


def format_change(value: Any) -> str:
    """Format percentage changes for readable status cards."""

    number = to_float(value)
    if number is None:
        return "n/a"
    return f"{number:+.2f}%"


def build_chart_data(
    history_rows: list[dict[str, str]],
    assets: list[dict[str, Any]],
    primary_currency: str = "USD",
) -> list[dict[str, Any]]:
    """Build one chart payload per asset with labels, prices, and stats for the dashboard."""

    primary_currency = primary_currency.upper()
    palette = [
        {"line": "#f7931a", "glow": "rgba(247, 147, 26, 0.35)"},
        {"line": "#627eea", "glow": "rgba(98, 126, 234, 0.35)"},
        {"line": "#14f195", "glow": "rgba(20, 241, 149, 0.35)"},
        {"line": "#f43f5e", "glow": "rgba(244, 63, 94, 0.35)"},
        {"line": "#a78bfa", "glow": "rgba(167, 139, 250, 0.35)"},
        {"line": "#eab308", "glow": "rgba(234, 179, 8, 0.35)"},
    ]
    charts: list[dict[str, Any]] = []

    for index, asset in enumerate(assets):
        asset_id = asset["id"]
        labels: list[str] = []
        prices: list[float | None] = []
        for row in history_rows:
            if row.get("asset_id") != asset_id:
                continue
            if str(row.get("currency", "")).upper() != primary_currency:
                continue
            timestamp = row.get("timestamp", "")
            if not timestamp:
                continue
            labels.append(timestamp)
            prices.append(to_float(row.get("price")))

        valid_prices = [price for price in prices if price is not None]
        stats = {
            "min": min(valid_prices) if valid_prices else None,
            "max": max(valid_prices) if valid_prices else None,
            "avg": sum(valid_prices) / len(valid_prices) if valid_prices else None,
            "first": valid_prices[0] if valid_prices else None,
            "last": valid_prices[-1] if valid_prices else None,
        }
        if stats["first"] and stats["last"]:
            stats["range_change_percent"] = ((stats["last"] - stats["first"]) / stats["first"]) * 100
        else:
            stats["range_change_percent"] = None

        color = palette[index % len(palette)]
        charts.append(
            {
                "id": asset_id,
                "symbol": asset.get("symbol", asset_id.upper()),
                "name": asset.get("name", asset_id),
                "color": color["line"],
                "glow": color["glow"],
                "labels": labels,
                "prices": prices,
                "stats": stats,
            }
        )

    return charts


def render_cards(rows: list[dict[str, Any]]) -> str:
    """Render HTML status cards for all monitored assets."""

    cards = []
    for row in rows:
        status_class = "ok" if row["status"] == STATUS_OK else "notok"
        symbol = html.escape(str(row["symbol"]))
        change_value = to_float(row["change_24h_percent"])
        change_class = "neutral"
        if change_value is not None:
            change_class = "up" if change_value >= 0 else "down"
        change_arrow = "&uarr;" if change_class == "up" else ("&darr;" if change_class == "down" else "&middot;")
        cards.append(
            f"""
            <a class="card {status_class}" href="#chart-{symbol}" data-symbol="{symbol}" title="Zum {symbol}-Chart springen">
              <div class="card-top">
                <span class="symbol">{symbol}</span>
                <span class="pill">{html.escape(str(row['status']))}</span>
              </div>
              <h2>{html.escape(str(row['asset_name']))}</h2>
              <p class="price">{format_price(row['price'])} <span class="currency">{html.escape(str(row['currency']))}</span></p>
              <p class="change {change_class}"><span class="arrow">{change_arrow}</span> {format_change(row['change_24h_percent'])} <span class="muted-label">24h</span></p>
              <p class="reason">{html.escape(str(row['reason']))}</p>
            </a>
            """
        )
    return "\n".join(cards)


def render_chart_panels(charts: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """Render one chart panel per asset (each gets its own canvas + stats header)."""

    rows_by_symbol = {row["symbol"]: row for row in rows}
    panels = []
    for chart in charts:
        symbol = html.escape(str(chart["symbol"]))
        name = html.escape(str(chart["name"]))
        row = rows_by_symbol.get(chart["symbol"], {})
        stats = chart["stats"]
        currency = html.escape(str(row.get("currency", "USD")))
        latest_price = format_price(row.get("price")) if row else format_price(stats.get("last"))
        change_value = to_float(row.get("change_24h_percent")) if row else None
        change_class = "neutral"
        if change_value is not None:
            change_class = "up" if change_value >= 0 else "down"
        change_text = format_change(row.get("change_24h_percent")) if row else "n/a"
        min_text = format_price(stats.get("min")) if stats.get("min") is not None else "n/a"
        max_text = format_price(stats.get("max")) if stats.get("max") is not None else "n/a"
        avg_text = format_price(stats.get("avg")) if stats.get("avg") is not None else "n/a"
        range_change = stats.get("range_change_percent")
        range_class = "neutral"
        if range_change is not None:
            range_class = "up" if range_change >= 0 else "down"
        range_text = f"{range_change:+.2f}%" if range_change is not None else "n/a"

        panels.append(
            f"""
            <section class="chart-panel" id="chart-{symbol}">
              <header class="chart-header">
                <div class="chart-ident">
                  <span class="chart-dot" style="background: {chart['color']}; box-shadow: 0 0 18px {chart['glow']};"></span>
                  <div>
                    <p class="toolbar-title">{symbol} &middot; {name}</p>
                    <h3 class="chart-bigprice">{latest_price}<span class="currency">{currency}</span></h3>
                  </div>
                </div>
                <span class="chart-change {change_class}">{change_text} <span class="muted-label">24h</span></span>
              </header>
              <div class="chart-inline-stats">
                <span><em>Min</em>{min_text}</span>
                <span class="sep">&middot;</span>
                <span><em>Avg</em>{avg_text}</span>
                <span class="sep">&middot;</span>
                <span><em>Max</em>{max_text}</span>
                <span class="sep">&middot;</span>
                <span><em>Range</em><b class="{range_class}">{range_text}</b></span>
              </div>
              <div class="chart-canvas-wrap">
                <canvas id="canvas-{symbol}" data-symbol="{symbol}" aria-label="{name} price history chart"></canvas>
              </div>
            </section>
            """
        )
    return "\n".join(panels)


def generate_dashboard(
    config: dict[str, Any],
    rows: list[dict[str, Any]],
    history_file: Path,
    dashboard_file: Path,
) -> None:
    """Generate a self-contained HTML market display for the latest run."""

    dashboard_file.parent.mkdir(parents=True, exist_ok=True)
    history_limit = int(config["paths"].get("dashboard_history_rows", 160))
    history_rows = read_history(history_file, history_limit)
    charts = build_chart_data(
        history_rows,
        config["market"]["assets"],
        primary_currency=config["market"].get("primary_currency", "usd"),
    )

    notok_count = sum(1 for row in rows if row["status"] == STATUS_NOTOK)
    overall_status = STATUS_NOTOK if notok_count else STATUS_OK
    generated_at = utc_now().isoformat(timespec="seconds")
    cards_html = render_cards(rows)
    chart_panels_html = render_chart_panels(charts, rows)
    charts_json = json.dumps(charts)
    title = html.escape(config["project"].get("dashboard_title", "MarketPulse"))

    dashboard_html = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
  <style>
    :root {{
      color-scheme: dark;
      --bg-0: #050912;
      --bg-1: #0b1426;
      --panel: rgba(15, 23, 42, 0.72);
      --panel-strong: rgba(15, 23, 42, 0.92);
      --line: rgba(148, 163, 184, 0.18);
      --line-strong: rgba(148, 163, 184, 0.32);
      --text: #f1f5fb;
      --muted: #94a3b8;
      --muted-soft: #cbd5e1;
      --ok: #22c55e;
      --notok: #ef4444;
      --up: #22c55e;
      --down: #ef4444;
      --accent: #60a5fa;
      --accent-2: #a78bfa;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Inter", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-feature-settings: "ss01", "cv11", "tnum";
      background:
        radial-gradient(1200px 600px at 10% -10%, rgba(96, 165, 250, 0.18), transparent 60%),
        radial-gradient(900px 500px at 90% 0%, rgba(167, 139, 250, 0.16), transparent 60%),
        radial-gradient(700px 500px at 50% 110%, rgba(20, 241, 149, 0.10), transparent 60%),
        linear-gradient(180deg, var(--bg-0), var(--bg-1));
      color: var(--text);
      -webkit-font-smoothing: antialiased;
    }}
    main {{ width: min(1280px, calc(100% - 36px)); margin: 0 auto; padding: 48px 0 64px; }}
    .hero {{
      position: relative;
      overflow: hidden;
      padding: 36px;
      border: 1px solid var(--line);
      border-radius: 32px;
      background:
        linear-gradient(135deg, rgba(96, 165, 250, 0.12), rgba(167, 139, 250, 0.08) 40%, rgba(15, 23, 42, 0.6)),
        var(--panel-strong);
      box-shadow: 0 30px 90px rgba(0, 0, 0, 0.45);
      margin-bottom: 28px;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: -20% -10% auto auto;
      width: 380px; height: 380px;
      background: radial-gradient(circle, rgba(96, 165, 250, 0.32), transparent 60%);
      filter: blur(20px);
      pointer-events: none;
    }}
    .hero-row {{ display: flex; justify-content: space-between; gap: 24px; align-items: end; flex-wrap: wrap; position: relative; z-index: 1; }}
    .brand {{ display: inline-flex; align-items: center; gap: 10px; padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line-strong); background: rgba(255, 255, 255, 0.04); color: var(--muted); font-size: 0.78rem; font-weight: 800; letter-spacing: 0.22em; text-transform: uppercase; }}
    .brand-dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 14px var(--ok); animation: pulse 1.6s ease-in-out infinite; }}
    .brand.notok .brand-dot {{ background: var(--notok); box-shadow: 0 0 14px var(--notok); }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; transform: scale(1); }} 50% {{ opacity: 0.5; transform: scale(1.4); }} }}
    h1 {{ margin: 16px 0 0; font-size: clamp(2.4rem, 5.4vw, 4.6rem); letter-spacing: -0.04em; line-height: 1; font-weight: 900; background: linear-gradient(120deg, #ffffff 0%, #c7d2fe 50%, #a78bfa 100%); -webkit-background-clip: text; background-clip: text; color: transparent; }}
    .subtitle {{ margin: 14px 0 0; color: var(--muted); max-width: 720px; line-height: 1.6; font-size: 1rem; }}
    .hero-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-top: 24px; position: relative; z-index: 1; }}
    .hero-stat {{ padding: 14px 16px; border: 1px solid var(--line); border-radius: 18px; background: rgba(255,255,255,0.03); }}
    .hero-stat-label {{ font-size: 0.72rem; letter-spacing: 0.18em; text-transform: uppercase; color: var(--muted); font-weight: 800; }}
    .hero-stat-value {{ margin-top: 6px; font-size: 1.4rem; font-weight: 900; letter-spacing: -0.02em; }}
    .hero-stat-value.ok {{ color: var(--ok); }}
    .hero-stat-value.notok {{ color: var(--notok); }}

    .section-title {{ margin: 28px 0 14px; font-size: 0.78rem; letter-spacing: 0.24em; text-transform: uppercase; color: var(--muted); font-weight: 900; }}
    .trend-toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 0 0 16px; }}
    .trend-btn {{
      border: 1px solid var(--line-strong);
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.65);
      color: var(--muted-soft);
      font-size: 0.75rem;
      font-weight: 800;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      padding: 7px 12px;
    }}
    .trend-btn:hover {{ border-color: rgba(96, 165, 250, 0.7); color: var(--text); }}
    .trend-btn.active {{
      background: rgba(96, 165, 250, 0.24);
      border-color: rgba(96, 165, 250, 0.85);
      color: #ffffff;
      box-shadow: 0 0 0 1px rgba(96, 165, 250, 0.25) inset;
    }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }}
    a.card {{ text-decoration: none; color: inherit; display: block; }}
    .card, .chart-panel, .meta-panel {{
      border: 1px solid var(--line);
      border-radius: 22px;
      background: var(--panel);
      backdrop-filter: blur(18px);
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.28);
    }}
    .card {{ padding: 22px; position: relative; overflow: hidden; transition: transform 200ms ease, border-color 200ms ease, box-shadow 200ms ease; }}
    .card:hover {{ transform: translateY(-3px); border-color: rgba(96, 165, 250, 0.6); box-shadow: 0 28px 80px rgba(96, 165, 250, 0.18); }}
    .card::before {{ content: ""; position: absolute; inset: 0 auto 0 0; width: 4px; background: linear-gradient(180deg, var(--ok), transparent); }}
    .card.notok::before {{ background: linear-gradient(180deg, var(--notok), transparent); }}
    .card-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .symbol {{ color: var(--accent); font-weight: 900; letter-spacing: 0.16em; font-size: 0.92rem; }}
    .pill {{ padding: 5px 10px; border-radius: 999px; background: rgba(148, 163, 184, 0.12); font-size: 0.7rem; font-weight: 800; letter-spacing: 0.12em; }}
    .card.ok .pill {{ color: var(--ok); background: rgba(34, 197, 94, 0.12); }}
    .card.notok .pill {{ color: var(--notok); background: rgba(239, 68, 68, 0.14); }}
    h2 {{ margin: 14px 0 4px; font-size: 0.92rem; color: var(--muted); font-weight: 700; letter-spacing: 0.02em; }}
    .price {{ margin: 0; font-size: 2rem; font-weight: 900; letter-spacing: -0.04em; }}
    .price .currency {{ font-size: 0.85rem; color: var(--muted); margin-left: 6px; letter-spacing: 0.12em; font-weight: 800; }}
    .change {{ margin: 10px 0 0; font-weight: 800; font-size: 0.95rem; display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; }}
    .change .arrow {{ font-size: 0.85rem; }}
    .change .muted-label {{ color: var(--muted); font-weight: 700; margin-left: 6px; }}
    .change.up {{ color: var(--up); background: rgba(34, 197, 94, 0.1); }}
    .change.down {{ color: var(--down); background: rgba(239, 68, 68, 0.12); }}
    .change.neutral {{ color: var(--muted); background: rgba(148, 163, 184, 0.1); }}
    .reason {{ min-height: 32px; margin: 14px 0 0; color: var(--muted-soft); font-size: 0.86rem; line-height: 1.5; }}

    .chart-panel {{
      margin-top: 18px;
      padding: 36px 40px 28px;
      scroll-margin-top: 28px;
      background:
        linear-gradient(160deg, rgba(96, 165, 250, 0.06), rgba(15, 23, 42, 0.7) 40%),
        var(--panel-strong);
      color: var(--text);
      border: 1px solid var(--line);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      font-family: "Inter", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .chart-header {{ display: flex; justify-content: space-between; gap: 20px; align-items: baseline; flex-wrap: wrap; margin-bottom: 8px; padding-bottom: 18px; border-bottom: 1px solid var(--line); }}
    .chart-ident {{ display: flex; align-items: center; gap: 14px; }}
    .chart-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
    .toolbar-title {{ margin: 0 0 6px; color: var(--muted); font-size: 0.72rem; font-weight: 700; letter-spacing: 0.18em; text-transform: uppercase; }}
    .chart-bigprice {{ margin: 0; font-size: 2.6rem; font-weight: 800; letter-spacing: -0.04em; line-height: 1; color: var(--text); font-feature-settings: "tnum"; }}
    .chart-bigprice .currency {{ font-size: 0.78rem; color: var(--muted); margin-left: 8px; letter-spacing: 0.16em; font-weight: 700; vertical-align: middle; }}
    .chart-change {{ font-weight: 700; font-size: 0.92rem; padding: 6px 14px; border-radius: 999px; letter-spacing: 0.02em; }}
    .chart-change.up {{ color: #34d399; background: rgba(5, 150, 105, 0.20); }}
    .chart-change.down {{ color: #f87171; background: rgba(185, 28, 28, 0.20); }}
    .chart-change.neutral {{ color: var(--muted-soft); background: rgba(107, 114, 128, 0.18); }}
    .chart-change .muted-label {{ color: currentColor; opacity: 0.6; margin-left: 4px; font-weight: 600; }}
    .chart-inline-stats {{ display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin: 18px 0 24px; color: var(--muted-soft); font-size: 0.9rem; font-feature-settings: "tnum"; }}
    .chart-inline-stats em {{ font-style: normal; color: var(--muted); font-size: 0.66rem; letter-spacing: 0.2em; text-transform: uppercase; font-weight: 700; margin-right: 8px; }}
    .chart-inline-stats b {{ font-weight: 800; }}
    .chart-inline-stats .sep {{ color: var(--line-strong); }}
    .chart-inline-stats .up {{ color: #34d399; }}
    .chart-inline-stats .down {{ color: #f87171; }}
    .chart-canvas-wrap {{ position: relative; height: 420px; }}
    button {{
      border: 1px solid var(--line-strong);
      border-radius: 12px;
      background: rgba(15, 23, 42, 0.7);
      color: var(--text);
      cursor: pointer;
      font: inherit;
      font-weight: 700;
      font-size: 0.85rem;
      letter-spacing: 0.06em;
      padding: 8px 14px;
      transition: background 160ms ease, border-color 160ms ease, transform 160ms ease, opacity 160ms ease;
    }}
    button:hover {{ background: rgba(96, 165, 250, 0.16); border-color: rgba(96, 165, 250, 0.7); transform: translateY(-1px); }}
    .meta-panel {{ margin-top: 24px; padding: 20px 24px; color: var(--muted); display: flex; justify-content: space-between; gap: 18px; flex-wrap: wrap; font-size: 0.88rem; }}
    canvas {{ width: 100% !important; height: 100% !important; }}
    code {{ color: #bae6fd; font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.85rem; }}
    @media (max-width: 980px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .chart-stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 640px) {{
      main {{ width: min(100% - 20px, 1280px); padding: 24px 0; }}
      .grid {{ grid-template-columns: 1fr; }}
      .hero {{ padding: 24px; border-radius: 24px; }}
      .chart-panel {{ padding: 20px; }}
      .chart-canvas-wrap {{ height: 280px; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="hero-row">
        <div>
          <span class="brand {'notok' if overall_status == STATUS_NOTOK else ''}"><span class="brand-dot"></span> MarketPulse Live</span>
          <h1>{title}</h1>
          <p class="subtitle">Crypto-Marktueberwachung mit OK/NotOK-Logik. Kurse via CoinGecko, automatisch via cron, mit CSV-Verlauf, Logging und E-Mail-Alerts.</p>
        </div>
      </div>
      <div class="hero-stats">
        <div class="hero-stat">
          <div class="hero-stat-label">Status</div>
          <div class="hero-stat-value {'notok' if overall_status == STATUS_NOTOK else 'ok'}">{overall_status}</div>
        </div>
        <div class="hero-stat">
          <div class="hero-stat-label">Assets</div>
          <div class="hero-stat-value">{len(rows)}</div>
        </div>
        <div class="hero-stat">
          <div class="hero-stat-label">NotOK</div>
          <div class="hero-stat-value {'notok' if notok_count else 'ok'}">{notok_count}</div>
        </div>
        <div class="hero-stat">
          <div class="hero-stat-label">Updated</div>
          <div class="hero-stat-value" style="font-size:0.95rem;">{html.escape(generated_at)}</div>
        </div>
      </div>
    </section>

    <p class="section-title">Marktuebersicht</p>
    <section class="grid">
      {cards_html}
    </section>

    <p class="section-title">Kursverlauf</p>
    <div class="trend-toolbar" role="group" aria-label="Trend Zeitraum waehlen">
      <button type="button" class="trend-btn active" data-trend="1min">1min</button>
      <button type="button" class="trend-btn" data-trend="5min">5min</button>
      <button type="button" class="trend-btn" data-trend="1h">1h</button>
      <button type="button" class="trend-btn" data-trend="1d">1d</button>
    </div>
    {chart_panels_html}

    <section class="meta-panel">
      <span>Generated: <code>{html.escape(generated_at)}</code></span>
      <span>NotOK assets: <code>{notok_count}</code></span>
      <span>History: <code>data/market_history.csv</code></span>
      <span>Log: <code>logs/marketpulse.log</code></span>
    </section>
  </main>

  <script>
    const charts = {charts_json};
    const chartInstances = {{}};
    const trendConfig = {{
      '1min': {{ windowMs: 60 * 1000 }},
      '5min': {{ windowMs: 5 * 60 * 1000 }},
      '1h': {{ windowMs: 60 * 60 * 1000 }},
      '1d': {{ windowMs: 24 * 60 * 60 * 1000 }},
    }};
    let activeTrend = '1min';

    function shortLabel(iso) {{
      const date = new Date(iso);
      if (Number.isNaN(date.getTime())) return iso;
      const day = String(date.getDate()).padStart(2, '0');
      const month = String(date.getMonth() + 1).padStart(2, '0');
      const hour = String(date.getHours()).padStart(2, '0');
      const minute = String(date.getMinutes()).padStart(2, '0');
      return `${{day}}.${{month}} ${{hour}}:${{minute}}`;
    }}

    function formatPrice(value) {{
      if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
      return new Intl.NumberFormat('de-CH', {{ minimumFractionDigits: value >= 100 ? 2 : 4, maximumFractionDigits: value >= 100 ? 2 : 4 }}).format(value);
    }}

    function trendSlice(labels, prices, trendKey) {{
      const config = trendConfig[trendKey] || trendConfig['1h'];
      const entries = labels
        .map((label, index) => ({{
          rawPrice: prices[index],
          label,
          timestamp: Date.parse(label),
        }}))
        .map((entry) => ({{
          ...entry,
          price:
            entry.rawPrice === null || entry.rawPrice === undefined || entry.rawPrice === ''
              ? Number.NaN
              : Number(entry.rawPrice),
        }}))
        .filter((entry) => !Number.isNaN(entry.timestamp) && Number.isFinite(entry.price))
        .sort((a, b) => a.timestamp - b.timestamp);

      if (!entries.length) {{
        return {{ labels: labels.slice(), prices: prices.slice() }};
      }}

      const latestTimestamp = entries[entries.length - 1].timestamp;
      const cutoffTimestamp = latestTimestamp - config.windowMs;
      const filtered = entries.filter((entry) => entry.timestamp >= cutoffTimestamp);
      const selected = filtered.length ? filtered : [entries[entries.length - 1]];

      return {{
        labels: selected.map((entry) => entry.label),
        prices: selected.map((entry) => entry.price),
      }};
    }}

    function setActiveTrendButton(trendKey) {{
      document.querySelectorAll('button.trend-btn').forEach((button) => {{
        button.classList.toggle('active', button.dataset.trend === trendKey);
      }});
    }}

    function applyTrend(instance, trendKey) {{
      const sliced = trendSlice(instance.$sourceLabels, instance.$sourcePrices, trendKey);
      instance.data.labels = sliced.labels.map(shortLabel);
      instance.data.datasets[0].data = sliced.prices;
      instance.resetZoom?.();
      instance.update();
    }}

    function updateAllChartsForTrend(trendKey) {{
      activeTrend = trendKey;
      setActiveTrendButton(trendKey);
      Object.values(chartInstances).forEach((instance) => applyTrend(instance, trendKey));
    }}

    charts.forEach((chart) => {{
      const canvas = document.getElementById(`canvas-${{chart.symbol}}`);
      if (!canvas) return;
      const slicedTrend = trendSlice(chart.labels, chart.prices, activeTrend);

      const instance = new Chart(canvas, {{
        type: 'line',
        data: {{
          labels: slicedTrend.labels.map(shortLabel),
          datasets: [{{
            label: chart.symbol,
            data: slicedTrend.prices,
            borderColor: chart.color,
            borderWidth: 2.2,
            tension: 0,
            spanGaps: true,
            fill: false,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: chart.color,
            pointHoverBorderColor: '#0b1426',
            pointHoverBorderWidth: 2,
          }}]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          layout: {{ padding: {{ right: 16, top: 16, bottom: 4, left: 4 }} }},
          interaction: {{ mode: 'index', intersect: false }},
          animation: {{ duration: 700, easing: 'easeOutCubic' }},
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{
              backgroundColor: 'rgba(11, 20, 38, 0.96)',
              borderColor: 'rgba(148, 163, 184, 0.18)',
              borderWidth: 1,
              titleColor: '#94a3b8',
              titleFont: {{ size: 11, weight: '600' }},
               bodyColor: '#f1f5fb',
               bodyFont: {{ size: 13, weight: '700' }},
                padding: 12,
                cornerRadius: 10,
                displayColors: false,
                callbacks: {{
                  title: (items) => items[0] ? items[0].label : '',
                  label: (item) => `${{formatPrice(item.raw)}}`
                }}
              }},
              zoom: {{
              limits: {{ x: {{ min: 'original', max: 'original' }}, y: {{ min: 'original', max: 'original' }} }},
              pan: {{ enabled: true, mode: 'x', modifierKey: 'shift' }},
              zoom: {{
                wheel: {{ enabled: true, speed: 0.08 }},
                pinch: {{ enabled: true }},
                drag: {{ enabled: true, backgroundColor: chart.glow, borderColor: chart.color, borderWidth: 1 }},
                mode: 'x'
              }}
            }}
          }},
           scales: {{
             x: {{
               ticks: {{
                 color: 'rgba(148, 163, 184, 0.65)',
                 font: {{ size: 10, weight: '600' }},
                 maxTicksLimit: 8,
               }},
               grid: {{ display: false }},
               border: {{ display: false }}
             }},
             y: {{
               position: 'right',
              ticks: {{
                color: 'rgba(148, 163, 184, 0.6)',
                font: {{ size: 10, weight: '600' }},
                maxTicksLimit: 4,
                padding: 8,
                callback: (value) => formatPrice(value)
              }},
              grid: {{ display: false }},
              border: {{ display: false }}
            }}
          }}
        }}
      }});

      instance.$sourceLabels = chart.labels.slice();
      instance.$sourcePrices = chart.prices.slice();
      chartInstances[chart.symbol] = instance;

      canvas.addEventListener('dblclick', () => chartInstances[chart.symbol]?.resetZoom?.());
    }});

    document.querySelectorAll('button.trend-btn').forEach((button) => {{
      button.addEventListener('click', () => {{
        const trendKey = button.dataset.trend;
        if (!trendConfig[trendKey] || trendKey === activeTrend) return;
        updateAllChartsForTrend(trendKey);
      }});
    }});

    setActiveTrendButton(activeTrend);
  </script>
</body>
</html>
"""

    dashboard_file.write_text(dashboard_html, encoding="utf-8")


def should_send_email(config: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    """Decide if an E-Mail should be sent for this run."""

    email_config = config.get("email", {})
    if not email_config.get("enabled", False):
        return False

    send_when = str(email_config.get("send_when", "notok")).lower()
    has_notok = any(row["status"] == STATUS_NOTOK for row in rows)
    return send_when == "always" or (send_when == "notok" and has_notok)


def env_value(email_config: dict[str, Any], key: str) -> str:
    """Read secret E-Mail settings from environment variables."""

    env_name = email_config.get(key, "")
    return os.environ.get(env_name, "") if env_name else ""


def build_email_body(rows: list[dict[str, Any]], dashboard_file: Path) -> str:
    """Create a compact text E-Mail with all asset states."""

    lines = [
        "MarketPulse automatic report",
        "",
        f"Dashboard: {dashboard_file}",
        "",
        "Assets:",
    ]
    for row in rows:
        lines.append(
            f"- {row['status']} {row['symbol']}: {row['price']} {row['currency']} "
            f"24h={row['change_24h_percent']} reason={row['reason']}"
        )
    return "\n".join(lines)


def send_email(config: dict[str, Any], rows: list[dict[str, Any]], dashboard_file: Path) -> None:
    """Send an SMTP E-Mail if the project is configured for notification."""

    email_config = config["email"]
    smtp_host = email_config.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(email_config.get("smtp_port", 587))
    smtp_user = env_value(email_config, "smtp_user_env")
    smtp_password = env_value(email_config, "smtp_password_env")
    recipient = env_value(email_config, "recipient_env")
    sender = env_value(email_config, "sender_env") or smtp_user

    missing = []
    for name, value in {
        email_config.get("smtp_user_env", "SMTP_USER"): smtp_user,
        email_config.get("smtp_password_env", "SMTP_PASSWORD"): smtp_password,
        email_config.get("recipient_env", "ALERT_TO"): recipient,
    }.items():
        if not value:
            missing.append(name)

    if missing:
        raise RuntimeError(f"Missing E-Mail environment variables: {', '.join(missing)}")

    subject_status = STATUS_NOTOK if any(row["status"] == STATUS_NOTOK for row in rows) else STATUS_OK
    message = EmailMessage()
    message["Subject"] = f"MarketPulse {subject_status}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(build_email_body(rows, dashboard_file))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.starttls(context=context)
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)


def run(config_path: Path) -> int:
    """Run one complete monitoring cycle for cron."""

    config = load_config(config_path)
    paths = config["paths"]
    history_file = project_path(paths["history_csv"])
    log_file = project_path(paths["log_file"])
    dashboard_file = project_path(paths["dashboard_html"])
    logger = configure_logger(log_file)
    timestamp = utc_now().isoformat(timespec="seconds")

    try:
        api_data = fetch_market_data(config)
        rows = evaluate_market(config, api_data, timestamp)
    except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        rows = build_error_rows(config, timestamp, error)

    append_history(history_file, rows)
    log_rows(logger, rows)
    generate_dashboard(config, rows, history_file, dashboard_file)

    if should_send_email(config, rows):
        try:
            send_email(config, rows, dashboard_file)
            logger.info("OK notification=email sent=true")
        except Exception as error:  # The monitor must still finish and keep the dashboard/logs.
            logger.error("NotOK notification=email sent=false reason=%s", error)
    else:
        logger.info("OK notification=email skipped=true")

    notok_count = sum(1 for row in rows if row["status"] == STATUS_NOTOK)
    print(f"MarketPulse finished. NotOK={notok_count}. Dashboard={dashboard_file}")
    return 0


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for manual runs and cron runs."""

    parser = argparse.ArgumentParser(description="MarketPulse market monitor")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to config.json",
    )
    return parser.parse_args()


def main() -> int:
    """Program entry point."""

    args = parse_args()
    config_path = args.config if args.config.is_absolute() else BASE_DIR / args.config
    return run(config_path)


if __name__ == "__main__":
    raise SystemExit(main())
