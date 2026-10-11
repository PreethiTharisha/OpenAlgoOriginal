"""
OpenAlgo NIFTY Option 45-Second Timed Trade
-------------------------------------------
Purpose:
  BUY one pre-defined option symbol at 09:15:01 IST
  SELL the same quantity at 09:15:45 IST
  No new action at/after 09:15:50 IST

Notes:
- This script intentionally has NO EMA/RSI/OI/price/candle/filter logic.
- It dispatches MARKET orders based only on the local PC clock.
- Actual broker/exchange execution time cannot be guaranteed to the microsecond.
- Synchronize Windows clock before running.
"""

import os
import sys
import time
import json
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import requests

# =============================================================================
# USER SETTINGS -- CHANGE THESE ONLY
# =============================================================================

SYMBOL = "NIFTY13OCT2622700CE"
# Example alternative:
# SYMBOL = "NIFTY13OCT2622600PE"

QUANTITY = 75                 # IMPORTANT: set valid quantity for the contract/broker
EXCHANGE = "NFO"
PRODUCT = "MIS"
STRATEGY = "NIFTY_0915_45SEC"
OPENALGO_HOST = "http://127.0.0.1:5000"

# True = sends real order requests to the OpenAlgo instance.
# False = timing test only; no orders are sent.
LIVE_TRADING = true

# Timing requested
BUY_AT = dtime(9, 25, 1, 0)
SELL_AT = dtime(9, 25, 45, 0)
HARD_STOP_AT = dtime(9, 25, 46, 0)

# If Windows wakes up materially late, do NOT send a stale order.
# This does not add a trading filter; it protects the exact-timing requirement.
MAX_TRIGGER_LATENESS_MS = 250

# =============================================================================
# IST / CLOCK
# =============================================================================

IST = timezone(timedelta(hours=5, minutes=30))

def ist_now():
    return datetime.now(IST)

def stamp():
    now = ist_now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond:06d} IST"

def log(msg):
    print(f"[{stamp()}] {msg}", flush=True)

# =============================================================================
# INSTANCE .ENV LOADER
# =============================================================================

def load_instance_env():
    """
    Loads OpenAlgo's instance/.env without requiring python-dotenv.
    Existing process environment variables are not overwritten.
    """
    candidates = [
        Path.cwd() / "instance" / ".env",
        Path(__file__).resolve().parent / "instance" / ".env",
        Path(r"D:\OpenAlgo\instance\.env"),
        Path(r"D:\Preethi-Algo\instance\.env"),
    ]

    for env_path in candidates:
        if not env_path.exists():
            continue

        try:
            for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                if key and key not in os.environ:
                    os.environ[key] = value

            log(f"Loaded environment from: {env_path}")
            return env_path
        except Exception as exc:
            log(f"WARNING: could not read {env_path}: {exc}")

    return None

def get_openalgo_api_key():
    """
    Uses common app-key names only.
    Do not substitute a broker secret/API key unless that is actually your
    OpenAlgo application API key.
    """
    key_names = (
        "OPENALGO_API_KEY",
        "OPENALGO_APIKEY",
        "API_KEY",
        "APIKEY",
    )
    for key_name in key_names:
        value = os.getenv(key_name)
        if value and value.strip():
            return value.strip(), key_name

    return None, None

# =============================================================================
# ORDER DISPATCH
# =============================================================================

PLACEORDER_URL = OPENALGO_HOST.rstrip("/") + "/api/v1/placeorder"

def order_payload(api_key, action):
    return {
        "apikey": api_key,
        "strategy": STRATEGY,
        "symbol": SYMBOL,
        "action": action,
        "exchange": EXCHANGE,
        "pricetype": "MARKET",
        "product": PRODUCT,
        "quantity": str(QUANTITY),
    }

def send_order(action, api_key):
    sent_ns = time.perf_counter_ns()
    wall_sent = ist_now()

    if not LIVE_TRADING:
        log(
            f"DRY RUN -> {action} {SYMBOL} | qty={QUANTITY} | "
            f"local dispatch={wall_sent.strftime('%H:%M:%S.%f')}"
        )
        return {
            "dry_run": True,
            "action": action,
            "dispatch_time": wall_sent.isoformat(),
        }

    # Dedicated connection for each order. Small timeout prevents the script
    # from hanging indefinitely; SELL scheduling runs independently.
    try:
        with requests.Session() as session:
            response = session.post(
                PLACEORDER_URL,
                json=order_payload(api_key, action),
                timeout=(1.0, 4.0),
            )

        elapsed_ms = (time.perf_counter_ns() - sent_ns) / 1_000_000
        try:
            body = response.json()
        except Exception:
            body = response.text[:500]

        log(
            f"{action} request returned HTTP {response.status_code} "
            f"in {elapsed_ms:.3f} ms | response={body}"
        )
        return body

    except Exception as exc:
        elapsed_ms = (time.perf_counter_ns() - sent_ns) / 1_000_000
        log(f"{action} REQUEST ERROR after {elapsed_ms:.3f} ms: {exc}")
        return None

# =============================================================================
# HIGH-PRECISION SCHEDULER
# =============================================================================

def build_target(today, clock_time):
    return datetime.combine(today, clock_time, tzinfo=IST)

def wait_until_precise(target_dt):
    """
    Sleep efficiently until close to target, then busy-spin for the final
    ~30 ms. Uses perf_counter_ns for the final monotonic countdown.

    This improves local dispatch precision but Windows/Python is NOT a
    hard-real-time system.
    """
    now = ist_now()
    seconds_remaining = (target_dt - now).total_seconds()

    if seconds_remaining <= 0:
        return

    target_perf_ns = time.perf_counter_ns() + int(seconds_remaining * 1_000_000_000)

    while True:
        remaining_ns = target_perf_ns - time.perf_counter_ns()

        if remaining_ns <= 0:
            return

        remaining_s = remaining_ns / 1_000_000_000

        if remaining_s > 1.0:
            time.sleep(min(remaining_s - 0.25, 0.5))
        elif remaining_s > 0.100:
            time.sleep(remaining_s - 0.030)
        elif remaining_s > 0.030:
            time.sleep(remaining_s - 0.010)
        else:
            # Final busy-spin avoids normal sleep granularity near trigger.
            pass

def timed_order(action, target_dt, hard_stop_dt, api_key):
    wait_until_precise(target_dt)

    actual = ist_now()
    late_ms = (actual - target_dt).total_seconds() * 1000.0

    if actual >= hard_stop_dt:
        log(
            f"{action} SKIPPED: hard stop {hard_stop_dt.strftime('%H:%M:%S')} "
            f"already reached."
        )
        return

    if late_ms > MAX_TRIGGER_LATENESS_MS:
        log(
            f"{action} SKIPPED: trigger was {late_ms:.3f} ms late, "
            f"which exceeds MAX_TRIGGER_LATENESS_MS={MAX_TRIGGER_LATENESS_MS}."
        )
        return

    log(
        f"TRIGGER {action} | target={target_dt.strftime('%H:%M:%S.%f')} | "
        f"actual={actual.strftime('%H:%M:%S.%f')} | late={late_ms:.3f} ms"
    )
    send_order(action, api_key)

# =============================================================================
# MAIN
# =============================================================================

def main():
    load_instance_env()
    api_key, api_key_name = get_openalgo_api_key()

    if LIVE_TRADING and not api_key:
        log(
            "ERROR: OpenAlgo application API key not found. "
            "Set OPENALGO_API_KEY in instance/.env."
        )
        sys.exit(1)

    if not api_key:
        # Dry-run mode does not need a real key.
        api_key = "DRY_RUN"
        api_key_name = "DRY_RUN"

    now = ist_now()
    today = now.date()

    buy_dt = build_target(today, BUY_AT)
    sell_dt = build_target(today, SELL_AT)
    hard_stop_dt = build_target(today, HARD_STOP_AT)

    log("=" * 78)
    log("START NIFTY 09:15 45-SECOND SINGLE-SYMBOL STRATEGY")
    log(f"Symbol       : {SYMBOL}")
    log(f"Exchange     : {EXCHANGE}")
    log(f"Quantity     : {QUANTITY}")
    log(f"Product      : {PRODUCT}")
    log(f"BUY target   : {buy_dt.strftime('%Y-%m-%d %H:%M:%S.%f IST')}")
    log(f"SELL target  : {sell_dt.strftime('%Y-%m-%d %H:%M:%S.%f IST')}")
    log(f"Hard stop    : {hard_stop_dt.strftime('%Y-%m-%d %H:%M:%S.%f IST')}")
    log(f"Live trading : {LIVE_TRADING}")
    log(f"API key var  : {api_key_name}")
    log("=" * 78)

    if now >= hard_stop_dt:
        log("Current time is already at/after 09:15:50 IST. Nothing will be sent.")
        return

    # If started between 09:15:00 and 09:15:50, do not enter late.
    if now >= buy_dt:
        log(
            "Script was started after the 09:15:01 BUY trigger. "
            "Late entry is not allowed, so nothing will be sent."
        )
        return

    # BUY and SELL are independently scheduled.
    # Therefore a slow BUY HTTP response cannot push the SELL trigger beyond 09:15:45.
    buy_thread = threading.Thread(
        target=timed_order,
        args=("BUY", buy_dt, hard_stop_dt, api_key),
        name="BUY_091500",
        daemon=False,
    )
    sell_thread = threading.Thread(
        target=timed_order,
        args=("SELL", sell_dt, hard_stop_dt, api_key),
        name="SELL_091545",
        daemon=False,
    )

    buy_thread.start()
    sell_thread.start()

    buy_thread.join()

    # SELL thread is allowed to continue until its own exact target.
    sell_thread.join()

    now = ist_now()
    if now < hard_stop_dt:
        wait_until_precise(hard_stop_dt)

    log("09:15:50 hard stop reached. Strategy finished. No further action.")

if __name__ == "__main__":
    main()
