#!/usr/bin/env python3
"""
MarketPulse
===========

Dieses Programm ist fuer ein LB3-Projekt mit WSL gebaut.
Es ruft Marktkurse ueber eine API ab, bewertet jeden Wert mit OK/NotOK,
schreibt Log-Dateien, speichert einen CSV-Verlauf und kann bei NotOK
automatisch eine E-Mail senden.

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
    """Convert one API result into one OK/NotOK row for logs."""

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
    """Append all current rows to a CSV file for the price history."""

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


def build_email_body(rows: list[dict[str, Any]]) -> str:
    """Create a compact text E-Mail with all asset states."""

    lines = ["MarketPulse automatic report", "", "Assets:"]
    for row in rows:
        lines.append(
            f"- {row['status']} {row['symbol']}: {row['price']} {row['currency']} "
            f"24h={row['change_24h_percent']} reason={row['reason']}"
        )
    return "\n".join(lines)


def send_email(config: dict[str, Any], rows: list[dict[str, Any]]) -> None:
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
    message.set_content(build_email_body(rows))

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
    logger = configure_logger(log_file)
    timestamp = utc_now().isoformat(timespec="seconds")

    try:
        api_data = fetch_market_data(config)
        rows = evaluate_market(config, api_data, timestamp)
    except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        rows = build_error_rows(config, timestamp, error)

    append_history(history_file, rows)
    log_rows(logger, rows)

    if should_send_email(config, rows):
        try:
            send_email(config, rows)
            logger.info("OK notification=email sent=true")
        except Exception as error:
            logger.error("NotOK notification=email sent=false reason=%s", error)
    else:
        logger.info("OK notification=email skipped=true")

    notok_count = sum(1 for row in rows if row["status"] == STATUS_NOTOK)
    print(f"MarketPulse finished. NotOK={notok_count}.")
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
