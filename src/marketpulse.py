#!/usr/bin/env python3

#MarketPulse


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
    return [
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
        for asset in config["market"]["assets"]
    ]


def evaluate_market(config: dict[str, Any], api_data: dict[str, Any], timestamp: str) -> list[dict[str, Any]]:
    """Evaluate all configured assets by looping through the asset list."""

    market_config = config["market"]
    primary_currency = market_config.get("primary_currency", "chf").lower()
    default_threshold = float(market_config.get("default_notok_drop_percent", -6.0))
    return [
        evaluate_asset(asset, api_data, primary_currency, default_threshold, timestamp)
        for asset in market_config["assets"]
    ]


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
    """Build the chart data used by the HTML dashboard."""

    primary_currency = primary_currency.upper()
    colors = ["#f7931a", "#627eea", "#14f195", "#f43f5e", "#a78bfa", "#eab308"]
    charts: list[dict[str, Any]] = []

    for index, asset in enumerate(assets):
        asset_id = asset["id"]
        points = []
        for row in history_rows:
            if row.get("asset_id") != asset_id:
                continue
            if str(row.get("currency", "")).upper() != primary_currency:
                continue
            timestamp = row.get("timestamp")
            price = to_float(row.get("price"))
            if timestamp and price is not None:
                points.append({"x": timestamp, "y": price})

        charts.append(
            {
                "id": asset_id,
                "symbol": asset.get("symbol", asset_id.upper()),
                "name": asset.get("name", asset_id),
                "color": colors[index % len(colors)],
                "points": points,
            }
        )

    return charts


def change_class(value: Any) -> str:
    """Choose the CSS class for a positive, negative, or missing percentage."""

    number = to_float(value)
    if number is None:
        return "neutral"
    return "up" if number >= 0 else "down"


def render_cards(rows: list[dict[str, Any]]) -> str:
    """Render HTML status cards for all monitored assets."""

    cards = []
    for index, row in enumerate(rows):
        status_class = "ok" if row["status"] == STATUS_OK else "notok"
        active_class = " active" if index == 0 else ""
        symbol = html.escape(str(row["symbol"]))
        cards.append(
            f"""
            <button class="card {status_class}{active_class}" type="button" data-symbol="{symbol}">
              <div class="card-top">
                <span class="symbol">{symbol}</span>
                <span class="pill">{html.escape(str(row['status']))}</span>
              </div>
              <h2>{html.escape(str(row['asset_name']))}</h2>
              <p class="price">{format_price(row['price'])} <span class="currency">{html.escape(str(row['currency']))}</span></p>
              <p class="change {change_class(row['change_24h_percent'])}">{format_change(row['change_24h_percent'])} <span class="muted-label">24h</span></p>
              <p class="reason">{html.escape(str(row['reason']))}</p>
            </button>
            """
        )
    return "\n".join(cards)


def render_filter_buttons(charts: list[dict[str, Any]]) -> str:
    """Render the chart filter buttons."""

    buttons = []
    for index, chart in enumerate(charts):
        symbol = html.escape(str(chart["symbol"]))
        active_class = " active" if index == 0 else ""
        buttons.append(f'<button class="filter{active_class}" type="button" data-symbol="{symbol}">{symbol}</button>')
    return "\n".join(buttons)


DASHBOARD_CSS = """
:root{color-scheme:dark;--bg:#08111f;--panel:#111c2e;--line:#26354f;--text:#eef4ff;--muted:#9caec8;--ok:#22c55e;--notok:#ef4444;--accent:#60a5fa}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font-family:Arial,Helvetica,sans-serif}
main{width:min(1100px,calc(100% - 32px));margin:auto;padding:32px 0}.header,.card,.chart,.meta{background:var(--panel);border:1px solid var(--line);border-radius:12px}
.header{padding:24px;margin-bottom:16px}h1{margin:0 0 12px;font-size:2rem}.summary,.meta{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted)}strong{color:var(--text)}.ok-text{color:var(--ok)}.notok-text{color:var(--notok)}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.card{color:inherit;text-align:left;padding:18px;cursor:pointer}.card:hover,.card.active,.filter:hover{border-color:var(--accent)}.card.notok{border-color:var(--notok)}.card.notok.active{border-color:var(--accent)}
.card-top{display:flex;justify-content:space-between;gap:12px}.symbol{color:var(--accent);font-weight:800;letter-spacing:.08em}.pill,.currency,.muted-label,.reason{color:var(--muted)}
h2{margin:14px 0 6px;font-size:1rem;color:var(--muted)}.price{margin:0;font-size:1.6rem;font-weight:800}.change.up{color:var(--ok)}.change.down{color:var(--notok)}.change.neutral{color:var(--muted)}.reason{min-height:42px;line-height:1.4}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:22px 0 12px}button{font:inherit}.filter{border:1px solid var(--line);border-radius:999px;background:var(--panel);color:var(--text);padding:8px 14px;cursor:pointer}.filter.active{background:var(--accent);border-color:var(--accent);color:#05101f}
.chart{height:420px;padding:18px}.meta{margin-top:18px;padding:14px 18px}code{color:#bfdbfe}@media(max-width:800px){.grid{grid-template-columns:1fr}.chart{height:320px}}
"""


DASHBOARD_SCRIPT = """
const charts = __CHARTS_JSON__;
const firstSymbol = charts.length ? charts[0].symbol : '';
const chart = new Chart(document.getElementById('priceChart'), {
  type: 'line',
  data: { datasets: datasetsFor(firstSymbol) },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'nearest', intersect: false },
    plugins: { legend: { labels: { color: '#eef4ff' } } },
    scales: {
      x: { type: 'category', ticks: { color: '#9caec8' }, grid: { color: '#1b2a40' } },
      y: { ticks: { color: '#9caec8' }, grid: { color: '#1b2a40' } },
    },
  },
});

function label(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('de-CH', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function datasetsFor(symbol) {
  return charts
    .filter((item) => item.symbol === symbol)
    .map((item) => ({
      label: item.symbol,
      data: item.points.map((point) => ({ x: label(point.x), y: point.y })),
      borderColor: item.color,
      backgroundColor: item.color,
      borderWidth: 2,
      pointRadius: 2,
      tension: 0.15,
    }));
}

function show(symbol) {
  document.querySelectorAll('[data-symbol]').forEach((button) => {
    button.classList.toggle('active', button.dataset.symbol === symbol);
  });
  chart.data.datasets = datasetsFor(symbol);
  chart.update();
}

document.querySelectorAll('[data-symbol]').forEach((button) => {
  button.addEventListener('click', () => show(button.dataset.symbol));
});
"""


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
    dashboard_script = DASHBOARD_SCRIPT.replace("__CHARTS_JSON__", json.dumps(charts))
    title = html.escape(config["project"].get("dashboard_title", "MarketPulse"))

    dashboard_html = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>{DASHBOARD_CSS}</style>
</head>
<body>
  <main>
    <section class="header">
      <h1>{title}</h1>
      <div class="summary">
        <span>Status: <strong class="{'notok-text' if overall_status == STATUS_NOTOK else 'ok-text'}">{overall_status}</strong></span>
        <span>Assets: <strong>{len(rows)}</strong></span>
        <span>NotOK: <strong>{notok_count}</strong></span>
        <span>Updated: <strong>{html.escape(generated_at)}</strong></span>
      </div>
    </section>

    <section class="grid">
      {render_cards(rows)}
    </section>

    <div class="toolbar">
      {render_filter_buttons(charts)}
    </div>
    <section class="chart">
      <canvas id="priceChart"></canvas>
    </section>

    <section class="meta">
      <span>History: <code>data/market_history.csv</code></span>
      <span>Log: <code>logs/marketpulse.log</code></span>
    </section>
  </main>

  <script>{dashboard_script}</script>
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
