#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NIFTY50 EMA5/EMA9 ITM3 Options Strategy for OpenAlgo
======================================================

Strategy implemented
--------------------
1. Signal source: NIFTY spot index (NSE_INDEX), 1-minute CLOSED candles only.
2. EMA crossover: EMA5 vs EMA9.
3. Bullish crossover: EMA5 crosses above EMA9.
4. Bearish crossover: EMA5 crosses below EMA9.
5. Confirmation: wait 1 minute after a fresh crossover. During the confirmation
   window the move must continue in the same direction (EMA relationship remains
   valid, EMA5 slope remains directional, and closes remain monotonic from the
   crossover candle).
6. Startup catch-up: if the bot starts after a crossover that occurred within the
   last 15 minutes, it may enter without waiting for a new crossover ONLY when at
   least 1 minute have elapsed and the latest completed 1-minute candle confirms
   the direction: price continues in the signal direction, EMA5 slope continues,
   EMA5 remains on the correct side of EMA9, and EMA separation is not shrinking.
7. Bullish confirmed: square off strategy PUTs, then BUY nearest-expiry NIFTY
   ITM3 CE at MARKET.
8. Bearish confirmed: square off strategy CALLs, then BUY nearest-expiry NIFTY
   ITM3 PE at MARKET.
9. Expiry: dynamically obtained from OpenAlgo expiry(); expired dates are
   explicitly skipped, the nearest current/future expiry is verified, and the
   selector force-refreshes once if optionsymbol() fails during rollover.
10. Option contract: dynamically resolved by optionsymbol(offset="ITM3").
   No hard-coded strike interval or option symbol construction.
11. Quantity: LOTS * the current contract lot size returned by optionsymbol().
12. Stop-loss: fixed 5% below ACTUAL filled average price.
13. Profit management: below +5% peak uses only the -5% hard stop; +5% to <+10% protects at -1%; +10% to <+20% uses 7pp; +20% to <+50% uses 5pp; +50% to <+70% uses 7pp; +70%+ uses 10pp. The floor never moves backward.
14. Loss management: absolute fixed -5% hard stop plus a continuous 5-percentage-point trailing floor from the best P&L seen.
15. Loss trail: floor=max(-5%, peak_PnL-5pp). It ratchets upward only and never loosens.
    At peak +5% the floor reaches breakeven; at +10% it locks +5%.
15. On an opposite EMA crossover, the dedicated 2-3 minute reversal controller has absolute priority: startup/fresh entry logic may prepare the new side but cannot close the existing wrong-side option before reversal confirmation.
16. No new entries at/after 15:28:00 IST.
17. Force-close all strategy-managed positions at/after 15:29:00 IST.
18. Main loop monitors once per second; heartbeat is logged every 30 seconds.
19. Position state is persisted locally and reconciled with broker positionbook().

IMPORTANT SAFETY DEFAULT
------------------------
The bot refuses live trading unless ALLOW_LIVE_TRADING=true is explicitly set.
Run in OpenAlgo Analyzer mode first.

Required environment variable:
    OPENALGO_API_KEY=<your OpenAlgo app API key>

Optional environment variables:
    HOST_SERVER=http://127.0.0.1:5000
    LOTS=1
    ALLOW_LIVE_TRADING=false
    MANAGE_ALL_NIFTY_MIS_POSITIONS=false

If MANAGE_ALL_NIFTY_MIS_POSITIONS=true, the bot may adopt and manage ANY long
NIFTY MIS option position in the broker position book, including manually placed
positions. Leave it false unless that behaviour is deliberately required.

Install:
    pip install -U openalgo pandas

Run:
    python nifty50_ema5_ema9_itm3_openalgo.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from openalgo import api


# =============================================================================
# CONFIGURATION
# =============================================================================

IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("OPENALGO_API_KEY", "").strip()
API_HOST = os.getenv("HOST_SERVER", "http://127.0.0.1:5000").strip()

STRATEGY_NAME = "NIFTY50_EMA5_EMA9_RISK_HARDENED"
INDEX_SYMBOL = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPTIONS_EXCHANGE = "NFO"
OPTION_PRODUCT = "MIS"
OPTION_OFFSET = "ITM3"

EMA_FAST_PERIOD = 5
EMA_SLOW_PERIOD = 9
CANDLE_TIMEFRAME = "1m"
CANDLE_MINUTES = 1
LOOKBACK_DAYS = 10

# Market-open history bootstrap. Some OpenAlgo/broker history backends return
# only the few candles formed in the current session when queried just after
# 09:15, even when a multi-day date range is requested. If the first response
# is too short, fetch recent calendar days individually and merge them.
BOOTSTRAP_HISTORY_MAX_CALENDAR_DAYS = 14
BOOTSTRAP_HISTORY_TARGET_ROWS = 30

# Daily trading-capital band for testing.
# The strategy may use a capital BASE between Rs.3,00,000 and Rs.5,00,000.
# Default is the maximum Rs.5,00,000. Any environment override is clamped
# into this band so sizing can never assume less than Rs.3L or more than Rs.5L.
#
# IMPORTANT: this is the sizing capital base, not a forced amount that must
# be deployed on every trade. Actual deployment remains limited by the
# per-trade risk budget, option premium, exchange lot size, and max-deployment %.
MIN_TRADING_CAPITAL_RUPEES = 300000.0
MAX_TRADING_CAPITAL_RUPEES = 500000.0
REQUESTED_TRADING_CAPITAL_RUPEES = float(
    os.getenv("TRADING_CAPITAL_RUPEES", str(int(MAX_TRADING_CAPITAL_RUPEES)))
)
TRADING_CAPITAL_RUPEES = min(
    MAX_TRADING_CAPITAL_RUPEES,
    max(MIN_TRADING_CAPITAL_RUPEES, REQUESTED_TRADING_CAPITAL_RUPEES),
)

# The exchange contract lot size itself is never altered; only the NUMBER OF
# LOTS is dynamic so the fixed option-premium stop risk stays near budget.
RISK_PER_TRADE_PERCENT = max(0.1, float(os.getenv("RISK_PER_TRADE_PERCENT", "1.0")))
MAX_CAPITAL_DEPLOYMENT_PERCENT = min(
    100.0,
    max(1.0, float(os.getenv("MAX_CAPITAL_DEPLOYMENT_PERCENT", "25.0"))),
)

# Risk / profit management
STOPLOSS_PERCENT = 5.0
LOSS_WARNING_PERCENT = 5.0  # legacy field retained for state compatibility; adverse-warning exit is disabled
LOSS_ADVERSE_CONFIRM_CANDLES = 2
LOSS_TRAIL_GIVEBACK_PERCENT = 5.0

# Adaptive profit-trail bands.
# Early profits get more breathing room; larger profits tighten, then widen
# again for very large trend moves. The actual floor only ratchets upward.
EARLY_PROTECTION_START_PERCENT = 5.0
EARLY_PROFIT_TRAIL_START_PERCENT = 10.0
EARLY_PROTECTION_FLOOR_PERCENT = -1.0

PROFIT_LOCK_START_PERCENT = 20.0
ADAPTIVE_TRAIL_MID_PERCENT = 50.0
ADAPTIVE_TRAIL_HIGH_PERCENT = 70.0

TRAIL_10_TO_20_GIVEBACK = 7.0
TRAIL_20_TO_50_GIVEBACK = 5.0
TRAIL_50_TO_70_GIVEBACK = 7.0
TRAIL_70_PLUS_GIVEBACK = 10.0

# Profit trail: once +20% is reached, lock at least +20%. Thereafter the
# floor follows the highest observed P&L continuously at 5 percentage points
# below the peak: floor=max(+20%, peak-5). Examples: peak 23 -> floor 20;
# peak 28 -> floor 23; peak 36 -> floor 31. Before +20% is reached, a separate
# universal 5pp trail runs from the best P&L with an absolute floor of -5%.
TARGET_PERCENT = 20.0
TRAIL_ACTIVATION_PERCENT = 20.0
TRAIL_GIVEBACK_PERCENT = 5.0

# Broker-side protective SL-M. Software monitoring remains as a second layer.
BROKER_PROTECTIVE_STOP = os.getenv("BROKER_PROTECTIVE_STOP", "true").lower() == "true"
OPTION_TICK_SIZE = 0.05

# Entry-quality filters for 1-minute NIFTY.
BREAKOUT_LOOKBACK = 5
NOISE_LOOKBACK = 20
ENTRY_SUPPORT_MOVES = 3              # require 3 of last 4 moves in direction
MIN_PROGRESS_NOISE_MULT = 0.75       # displacement from cross vs recent 1m noise
MIN_EMA_GAP_NOISE_MULT = 0.20        # reject nearly-touching EMAs
MAX_EMA_DISTANCE_NOISE_MULT = 3.00   # allow a little more room on fast EMA5/EMA9 moves without chasing extreme spikes
FROZEN_FEED_POINTS = 5
MAX_DATA_AGE_SECONDS = 120

# Daily risk guard. MAX_DAILY_REALIZED_LOSS_RUPEES=0 disables rupee cap.
# Consecutive-loss protection remains available for LIVE mode.
# V7 intentionally disables the per-day trade-count ceiling for extended testing.
MAX_CONSECUTIVE_LOSSES = max(1, int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2")))
ANALYZER_IGNORE_CONSECUTIVE_LOSS_BLOCK = os.getenv("ANALYZER_IGNORE_CONSECUTIVE_LOSS_BLOCK", "true").lower() == "true"
ANALYZER_IGNORE_MAX_TRADES_BLOCK = True  # compatibility flag; V7 trade-count cap is disabled
MAX_TRADES_PER_DAY = 0  # 0 = no per-day trade-count ceiling; count is diagnostics only
MAX_DAILY_REALIZED_LOSS_RUPEES = max(
    0.0, float(os.getenv("MAX_DAILY_REALIZED_LOSS_RUPEES", "0"))
)

CONFIRMATION_MIN_MINUTES = 1
CONFIRMATION_MAX_MINUTES = 3

# Startup existing-regime retry window:
# If the bot starts inside an already-established EMA5/EMA9 regime, do not force
# an entry and do not abandon the regime after one weak candle. Re-check the same
# regime once per completed 1m candle for up to 5 minutes. A real opposite EMA
# crossover cancels the startup opportunity immediately.
STARTUP_EXISTING_RETRY_MIN_MINUTES = 1
STARTUP_EXISTING_RETRY_MAX_MINUTES = 5

# Opposite-regime EXIT confirmation:
# Do not close the current CE/PE on a one-candle/tiny EMA cross.
# Require the opposite EMA regime to persist for 2-3 completed 1m candles.
REVERSAL_CONFIRM_MIN_MINUTES = 2
REVERSAL_CONFIRM_MAX_MINUTES = 3

# Fluctuation filters for 1-minute chart noise.
# Require at least 2 supportive moves among the latest 5 completed 1m candles.
FLUCTUATION_WINDOW_POINTS = 5
FLUCTUATION_MIN_SUPPORT_MOVES = 2

# Established-regime continuation entry.
# Sixteen completed 1-minute closes span 15 minutes: t-15 ... t.
REGIME_CONTINUATION_MINUTES = 15
REGIME_CONTINUATION_POINTS = 16

MARKET_OPEN = dt_time(9, 15)
ENTRY_CUTOFF = dt_time(15, 28)   # No new orders at/after 15:28:00 IST
FORCE_CLOSE = dt_time(15, 29)    # Force exits at/after 15:29:00 IST
MARKET_CLOSE = dt_time(15, 30)

POLL_INTERVAL_SECONDS = 1.0
ORDER_STATUS_POLL_SECONDS = 0.5
ORDER_FILL_TIMEOUT_SECONDS = 20.0
EXIT_VERIFY_TIMEOUT_SECONDS = 8.0
RECONCILE_INTERVAL_SECONDS = 30.0
MAX_QUOTE_FAILURES = 5

# Operational visibility only; these DO NOT change trading logic.
HEARTBEAT_INTERVAL_SECONDS = 30.0
DATA_WARNING_INTERVAL_SECONDS = 30.0

ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
MANAGE_ALL_NIFTY_MIS_POSITIONS = (
    os.getenv("MANAGE_ALL_NIFTY_MIS_POSITIONS", "true").lower() == "true"
)

STATE_FILE = Path(
    os.getenv(
        "NIFTY_EMA_STATE_FILE",
        str(Path(__file__).with_name(".nifty_ema5_ema9_risk_hardened_state.json")),
    )
)

LOCK_FILE = Path(__file__).with_name(".nifty_ema5_ema9_risk_hardened.lock")

NIFTY_OPTION_RE = re.compile(r"^NIFTY\d{2}[A-Z]{3}\d{2}\d+(CE|PE)$", re.IGNORECASE)


# -----------------------------------------------------------------------------
# BIDIRECTIONAL DECISION RULES
# -----------------------------------------------------------------------------
# BULLISH:
#   EMA5 crosses ABOVE EMA9
#   -> observe completed 1m candles for 1 minute
#   -> EMA5 stays above EMA9
#   -> EMA5 rises
#   -> price stays/progresses above crossover level
#   -> latest close is above EMA5
#   -> EMA5/EMA9 gap expands
#   -> at least 3 supportive upward moves among latest 5 completed 1m candles
#   -> square off PE positions
#   -> BUY nearest-expiry ITM3 CE at MARKET
#
# BEARISH:
#   EMA5 crosses BELOW EMA9
#   -> observe completed 1m candles for 1 minute
#   -> EMA5 stays below EMA9
#   -> EMA5 falls
#   -> price stays/progresses below crossover level
#   -> latest close is below EMA5
#   -> EMA5/EMA9 gap expands
#   -> at least 3 supportive downward moves among latest 5 completed 1m candles
#   -> square off CE positions
#   -> BUY nearest-expiry ITM3 PE at MARKET
#
# After one trade is executed for a regime, do not place another trade from the
# same regime. Wait for a NEW fresh EMA5/EMA9 crossover.
# -----------------------------------------------------------------------------

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class ManagedPosition:
    symbol: str
    exchange: str
    option_type: str  # CE / PE
    quantity: int
    entry_price: float
    stop_price: float
    target_price: float
    entry_time: str
    expiry: str = ""
    quote_failures: int = 0
    trailing_active: bool = False
    peak_pnl_percent: float = 0.0
    trailing_floor_percent: float = 0.0
    protective_stop_order_id: str = ""
    loss_warning_active: bool = False
    loss_adverse_count: int = 0
    loss_last_candle: str = ""
    loss_last_sample_ltp: float = 0.0


@dataclass
class PendingSignal:
    direction: str  # BULLISH / BEARISH
    detected_at: str  # ISO timestamp in IST
    crossover_close: float


# =============================================================================
# STRATEGY
# =============================================================================

class NiftyEMAOptionsStrategy:
    def __init__(self) -> None:
        self._lock_handle = None
        self._acquire_single_instance_lock()

        if not API_KEY:
            raise RuntimeError(
                "OPENALGO_API_KEY is not set. Do not hard-code API keys in the script."
            )

        self.client = api(api_key=API_KEY, host=API_HOST)

        self.positions: Dict[str, ManagedPosition] = {}
        self.pending_signal: Optional[PendingSignal] = None
        self.pending_signal_source: Optional[str] = None  # "startup" / "fresh"
        self.pending_reversal: Optional[PendingSignal] = None
        # One-shot reversal-entry bypass: after a confirmed opposite-side reversal
        # closes the current position, allow exactly one new-side entry even if that
        # exit has just pushed consecutive_losses to the configured limit. This
        # bypass never overrides max-trades, daily rupee-loss cap, cutoff, or other
        # safety checks, and is consumed only by a successful reverse entry.
        self.reversal_entry_bypass_direction: Optional[str] = None
        self.last_processed_candle_close: Optional[pd.Timestamp] = None
        self.last_reconcile_monotonic = 0.0
        self.entry_block_reason: Optional[str] = None
        self.expiry_cache_date: Optional[date] = None
        self.expiry_cache_value: Optional[str] = None
        self.running = True
        self.force_close_started = False
        self.analyze_mode = True
        self.analyzer_sl_skip_logged = False

        # Daily strategy risk accounting.
        self.risk_day = self.now_ist().date().isoformat()
        self.daily_realized_pnl = 0.0
        self.daily_trade_count = 0
        self.consecutive_losses = 0
        self.daily_block_reason: Optional[str] = None

        # One-time startup opportunity:
        # if the bot starts while EMA5/EMA9 are already in a regime,
        # observe that EXISTING regime for 1 minute. After that window
        # succeeds or fails, only a fresh EMA crossover can create another trade.
        self.startup_existing_regime_window_used = False

        # One-entry-per-EMA-regime protection.
        self.last_traded_regime: Optional[str] = None


        # Live-data / operational visibility state.
        self.last_heartbeat_monotonic = 0.0
        self.last_data_warning_monotonic = 0.0
        self.last_market_data_ok_at: Optional[datetime] = None
        self.last_market_snapshot: Optional[Dict[str, Any]] = None

        self._load_state()
        self._verify_analyzer_or_live_permission()
        self._reconcile_positions(verbose=True)

    def _acquire_single_instance_lock(self) -> None:
        """
        Prevent two copies of the final controller from trading the same account.
        The OS releases the lock automatically when the process exits/crashes.
        """
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = open(LOCK_FILE, "a+b")

        try:
            self._lock_handle.seek(0)
            if self._lock_handle.read(1) == b"":
                self._lock_handle.seek(0)
                self._lock_handle.write(b"1")
                self._lock_handle.flush()

            self._lock_handle.seek(0)

            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(
                    self._lock_handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except Exception as exc:
            try:
                self._lock_handle.close()
            except Exception:
                pass
            self._lock_handle = None
            raise RuntimeError(
                "ANOTHER NIFTY EMA CONTROLLER INSTANCE IS ALREADY RUNNING. "
                "Stop the other strategy process before starting this one."
            ) from exc

    def _release_single_instance_lock(self) -> None:
        if self._lock_handle is None:
            return
        try:
            self._lock_handle.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(self._lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
        finally:
            try:
                self._lock_handle.close()
            except Exception:
                pass
            self._lock_handle = None

    # -------------------------------------------------------------------------
    # Logging / time helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def now_ist() -> datetime:
        return datetime.now(IST)

    @staticmethod
    def log(message: str) -> None:
        stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp} IST] {message}", flush=True)

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _adaptive_profit_giveback(peak_pnl_percent: float) -> float:
        """Return the active adaptive giveback band for the current peak profit."""
        if peak_pnl_percent >= ADAPTIVE_TRAIL_HIGH_PERCENT:
            return TRAIL_70_PLUS_GIVEBACK
        if peak_pnl_percent >= ADAPTIVE_TRAIL_MID_PERCENT:
            return TRAIL_50_TO_70_GIVEBACK
        if peak_pnl_percent >= PROFIT_LOCK_START_PERCENT:
            return TRAIL_20_TO_50_GIVEBACK
        if peak_pnl_percent >= EARLY_PROFIT_TRAIL_START_PERCENT:
            return TRAIL_10_TO_20_GIVEBACK
        return 0.0

    @classmethod
    def _desired_trailing_floor(cls, peak_pnl_percent: float) -> float:
        """
        Adaptive production-test trailing model:

          peak < +5%       -> fixed -5% hard stop only
          +5% to < +10%    -> fixed -1% protection floor
          +10% to < +20%   -> 7pp giveback
          +20% to < +50%   -> 5pp giveback
          +50% to < +70%   -> 7pp giveback
          +70% and above   -> 10pp giveback

        The +5% to <+10% zone deliberately uses -1%, not exact breakeven.
        This gives NIFTY option premiums some room to fluctuate while
        preventing a meaningful winner from turning into a full -5% loser.

        Above +10%, the floor is true peak-minus-giveback.

        The caller ratchets with max(old_floor, desired_floor), so the
        already-locked floor can NEVER move backward when bands widen.
        """
        if peak_pnl_percent < EARLY_PROTECTION_START_PERCENT:
            return -STOPLOSS_PERCENT

        if peak_pnl_percent < EARLY_PROFIT_TRAIL_START_PERCENT:
            return max(
                -STOPLOSS_PERCENT,
                EARLY_PROTECTION_FLOOR_PERCENT,
            )

        giveback = cls._adaptive_profit_giveback(peak_pnl_percent)
        return max(
            EARLY_PROTECTION_FLOOR_PERCENT,
            peak_pnl_percent - giveback,
        )

    def _data_warning(self, message: str) -> None:
        """Rate-limit repetitive market-data warnings."""
        now_mono = time.monotonic()
        if (
            self.last_data_warning_monotonic == 0.0
            or now_mono - self.last_data_warning_monotonic >= DATA_WARNING_INTERVAL_SECONDS
        ):
            self.log(f"DATA WARNING: {message}")
            self.last_data_warning_monotonic = now_mono

    @staticmethod
    def _ema_relation(ema5: float, ema9: float) -> str:
        if ema5 > ema9:
            return "EMA5>EMA9 (bullish side)"
        if ema5 < ema9:
            return "EMA5<EMA9 (bearish side)"
        return "EMA5=EMA9"

    def _remember_market_snapshot(self, df: pd.DataFrame, now: datetime) -> None:
        if df is None or len(df) == 0:
            return
        row = df.iloc[-1]
        close_time = self._latest_candle_close_time(df)
        self.last_market_data_ok_at = now
        self.last_market_snapshot = {
            "close_time": close_time,
            "close": float(row["close"]),
            "ema5": float(row["ema5"]),
            "ema9": float(row["ema9"]),
        }

    def _log_heartbeat_if_due(self, now: datetime) -> None:
        """Print a quiet health line every 30s so the user knows live data is flowing."""
        now_mono = time.monotonic()
        if (
            self.last_heartbeat_monotonic != 0.0
            and now_mono - self.last_heartbeat_monotonic < HEARTBEAT_INTERVAL_SECONDS
        ):
            return

        self.last_heartbeat_monotonic = now_mono
        pending = self.pending_signal.direction if self.pending_signal else "NONE"
        reversal = self.pending_reversal.direction if self.pending_reversal else "NONE"
        pos_count = len(self.positions)

        if self.last_market_snapshot is None:
            self.log(
                f"HEARTBEAT: waiting for usable NIFTY 1m history | "
                f"pending={pending} | reversal={reversal} | managed_positions={pos_count}"
            )
            return

        snap = self.last_market_snapshot
        data_age = (
            (now - self.last_market_data_ok_at).total_seconds()
            if self.last_market_data_ok_at is not None
            else float("inf")
        )
        status = "OK" if data_age <= max(HEARTBEAT_INTERVAL_SECONDS * 2, 65.0) else "STALE"
        self.log(
            f"HEARTBEAT: market_data={status} | last_closed_1m={snap['close_time'].strftime('%H:%M:%S')} "
            f"| NIFTY={snap['close']:.2f} | EMA5={snap['ema5']:.2f} | EMA9={snap['ema9']:.2f} "
            f"| {self._ema_relation(snap['ema5'], snap['ema9'])} "
            f"| pending={pending} | managed_positions={pos_count}"
        )

    # -------------------------------------------------------------------------
    # Persistent state
    # -------------------------------------------------------------------------

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return

        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for item in raw.get("positions", []):
                pos = ManagedPosition(**item)
                self.positions[pos.symbol] = pos
            saved_regime = raw.get("last_traded_regime")
            if saved_regime in {"BULLISH", "BEARISH"}:
                self.last_traded_regime = saved_regime

            # Restore daily risk counters only for the current trading date.
            if raw.get("risk_day") == self.risk_day:
                self.daily_realized_pnl = float(raw.get("daily_realized_pnl", 0.0))
                self.daily_trade_count = int(raw.get("daily_trade_count", 0))
                self.consecutive_losses = int(raw.get("consecutive_losses", 0))
                self.daily_block_reason = raw.get("daily_block_reason")

            self.log(
                f"Loaded {len(self.positions)} tracked position(s) from {STATE_FILE.name}; "
                f"regime_lock={self.last_traded_regime or 'NONE'}."
            )
        except Exception as exc:
            raise RuntimeError(f"Could not read state file {STATE_FILE}: {exc}") from exc

    def _save_state(self) -> None:
        """
        Persist controller state without allowing a transient Windows file lock
        (for example WinError 5 / antivirus / sync-client interference) to crash
        the trading loop.

        Preferred path:
          write temp -> atomic replace -> retry on PermissionError/OSError

        Fallback path:
          direct overwrite -> retry

        Final emergency path:
          write a timestamped recovery file and continue running with a warning.
        """
        payload = {
            "version": 2,
            "updated_at": self.now_ist().isoformat(),
            "positions": [asdict(p) for p in self.positions.values()],
            "last_traded_regime": self.last_traded_regime,
            "risk_day": self.risk_day,
            "daily_realized_pnl": self.daily_realized_pnl,
            "daily_trade_count": self.daily_trade_count,
            "consecutive_losses": self.consecutive_losses,
            "daily_block_reason": self.daily_block_reason,
        }
        serialized = json.dumps(payload, indent=2)

        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        max_attempts = 6

        # Preferred atomic-save path.
        for attempt in range(1, max_attempts + 1):
            try:
                tmp.write_text(serialized, encoding="utf-8")
                tmp.replace(STATE_FILE)
                return
            except (PermissionError, OSError) as exc:
                if attempt < max_attempts:
                    time.sleep(0.15 * attempt)
                    continue
                atomic_error = exc

        # Graceful direct-write fallback. This is less atomic, but better than
        # terminating the strategy because Windows briefly locked the file.
        for attempt in range(1, 4):
            try:
                STATE_FILE.write_text(serialized, encoding="utf-8")
                self.log(
                    f"STATE SAVE RECOVERED: atomic replace failed ({atomic_error}); "
                    "direct state-file overwrite succeeded."
                )
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                return
            except (PermissionError, OSError) as exc:
                direct_error = exc
                time.sleep(0.20 * attempt)

        # Last-resort recovery snapshot. Do not crash the strategy loop.
        try:
            stamp = self.now_ist().strftime("%Y%m%d_%H%M%S")
            emergency = STATE_FILE.with_name(
                f"{STATE_FILE.stem}.recovery_{stamp}{STATE_FILE.suffix}"
            )
            emergency.write_text(serialized, encoding="utf-8")
            self.log(
                "STATE SAVE WARNING: primary state file could not be updated after retries "
                f"(atomic={atomic_error}; direct={direct_error}). "
                f"Recovery snapshot written to {emergency.name}; strategy continues."
            )
        except Exception as emergency_error:
            self.log(
                "STATE SAVE CRITICAL WARNING: state persistence failed after all retries "
                f"(atomic={atomic_error}; direct={direct_error}; "
                f"recovery={emergency_error}). Strategy continues, but restart recovery "
                "may be incomplete."
            )

    # -------------------------------------------------------------------------
    # OpenAlgo safety / session checks
    # -------------------------------------------------------------------------

    def _verify_analyzer_or_live_permission(self) -> None:
        """Refuse accidental live trading unless explicitly enabled."""
        try:
            response = self.client.analyzerstatus()
            if response.get("status") != "success":
                raise RuntimeError(str(response))
            data = response.get("data", {})
            analyze_mode = bool(data.get("analyze_mode"))
            self.analyze_mode = analyze_mode
            mode = data.get("mode", "unknown")
            self.log(f"OpenAlgo mode: {mode} (analyze_mode={analyze_mode}).")
            if analyze_mode and ANALYZER_IGNORE_CONSECUTIVE_LOSS_BLOCK:
                self.log(
                    "ANALYZER STRESS MODE: consecutive-loss entry blocking is DISABLED; "
                    "losses are still counted/logged."
                )
            if analyze_mode and ANALYZER_IGNORE_MAX_TRADES_BLOCK:
                self.log(
                    "ANALYZER STRESS MODE: maximum-trades-per-day blocking is DISABLED; "
                    "trade count is still tracked/logged for diagnostics."
                )

            if not analyze_mode and not ALLOW_LIVE_TRADING:
                raise RuntimeError(
                    "OpenAlgo is in LIVE mode, but ALLOW_LIVE_TRADING is not true. "
                    "Use Analyzer mode first or explicitly set ALLOW_LIVE_TRADING=true."
                )
        except AttributeError as exc:
            raise RuntimeError(
                "Your installed openalgo Python package does not expose analyzerstatus(). "
                "Upgrade it before using this bot."
            ) from exc
        except RuntimeError:
            raise
        except Exception as exc:
            if not ALLOW_LIVE_TRADING:
                raise RuntimeError(
                    f"Could not verify Analyzer/Live mode ({exc}). For safety, trading is blocked."
                ) from exc
            self.log(
                f"WARNING: Could not verify Analyzer/Live mode ({exc}), but "
                "ALLOW_LIVE_TRADING=true was explicitly set."
            )

    def _is_nfo_session_today(self) -> bool:
        """Use OpenAlgo timings() when available; conservative fallback on failure."""
        today = self.now_ist().date()
        if today.weekday() >= 5:
            return False

        try:
            response = self.client.timings(date=today.isoformat())
            if response.get("status") != "success":
                self.log(f"WARNING: timings() failed: {response}. Falling back to weekday check.")
                return True
            exchanges = {str(item.get("exchange", "")).upper() for item in response.get("data", [])}
            return OPTIONS_EXCHANGE in exchanges
        except Exception as exc:
            self.log(f"WARNING: Could not verify trading calendar ({exc}); using weekday fallback.")
            return True

    def _reset_daily_risk_if_needed(self) -> None:
        today = self.now_ist().date().isoformat()
        if today == self.risk_day:
            return
        self.risk_day = today
        self.daily_realized_pnl = 0.0
        self.daily_trade_count = 0
        self.consecutive_losses = 0
        self.daily_block_reason = None
        self._save_state()

    def _daily_risk_allows_entry(self, allow_reversal_loss_bypass: bool = False) -> bool:
        self._reset_daily_risk_if_needed()

        analyzer_relaxed_losses = (
            self.analyze_mode and ANALYZER_IGNORE_CONSECUTIVE_LOSS_BLOCK
        )
        bypass_consecutive_loss = (
            (allow_reversal_loss_bypass or analyzer_relaxed_losses)
            and self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES
        )

        # A one-shot confirmed reversal may bypass ONLY the consecutive-loss block.
        # It must never bypass the max-trades limit, daily rupee-loss cap, or any
        # unrelated persisted block reason.
        if self.daily_block_reason:
            is_consecutive_loss_block = (
                "consecutive losing exits reached" in self.daily_block_reason
            )
            is_max_trades_block = (
                "maximum " in self.daily_block_reason
                and "entries reached for the day" in self.daily_block_reason
            )

            if analyzer_relaxed_losses and is_consecutive_loss_block:
                self.log(
                    "ANALYZER STRESS MODE: clearing persisted consecutive-loss block; "
                    "loss count remains tracked for diagnostics."
                )
                self.daily_block_reason = None
                self._save_state()
            elif is_max_trades_block and MAX_TRADES_PER_DAY <= 0:
                self.log(
                    "TRADE-COUNT CAP DISABLED: clearing persisted maximum-trades block; "
                    "daily entry count remains tracked for diagnostics only."
                )
                self.daily_block_reason = None
                self._save_state()
            elif not (bypass_consecutive_loss and is_consecutive_loss_block):
                self.log(f"DAILY RISK BLOCK: {self.daily_block_reason}")
                return False
            elif allow_reversal_loss_bypass:
                self.log(
                    "REVERSAL ENTRY BYPASS: consecutive-loss daily block temporarily "
                    "ignored for this one confirmed opposite-side reversal entry."
                )

        # V7: no per-day trade-count ceiling. Count is retained only for diagnostics.
        if MAX_TRADES_PER_DAY > 0 and self.daily_trade_count >= MAX_TRADES_PER_DAY:
            self.daily_block_reason = (
                f"maximum {MAX_TRADES_PER_DAY} entries reached for the day"
            )
            self._save_state()
            self.log(f"DAILY RISK BLOCK: {self.daily_block_reason}")
            return False

        if (
            self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES
            and not bypass_consecutive_loss
            and not analyzer_relaxed_losses
        ):
            self.daily_block_reason = (
                f"{self.consecutive_losses} consecutive losing exits reached"
            )
            self._save_state()
            self.log(f"DAILY RISK BLOCK: {self.daily_block_reason}")
            return False

        if (
            MAX_DAILY_REALIZED_LOSS_RUPEES > 0
            and self.daily_realized_pnl <= -MAX_DAILY_REALIZED_LOSS_RUPEES
        ):
            self.daily_block_reason = (
                f"daily realized loss Rs.{abs(self.daily_realized_pnl):.2f} "
                f"reached cap Rs.{MAX_DAILY_REALIZED_LOSS_RUPEES:.2f}"
            )
            self._save_state()
            self.log(f"DAILY RISK BLOCK: {self.daily_block_reason}")
            return False

        return True

    @staticmethod
    def _recent_noise(df: pd.DataFrame, lookback: int = NOISE_LOOKBACK) -> float:
        closes = df["close"].astype(float).iloc[-(lookback + 1):]
        if len(closes) < 3:
            return 0.0
        diffs = closes.diff().abs().dropna()
        if len(diffs) == 0:
            return 0.0
        # Median is more robust than mean during one large breakout candle.
        return float(diffs.median())

    def _data_quality_ok(self, df: pd.DataFrame, now: datetime) -> Tuple[bool, str]:
        if df is None or len(df) < max(EMA_SLOW_PERIOD + 2, FROZEN_FEED_POINTS):
            return False, "insufficient completed 1m history"

        latest_close = self._latest_candle_close_time(df)
        now_ts = pd.Timestamp(now)
        age = (now_ts - latest_close).total_seconds()

        if latest_close > now_ts + pd.Timedelta(seconds=5):
            return False, f"future candle detected ({latest_close})"

        if age > MAX_DATA_AGE_SECONDS:
            return False, (f"latest completed candle {latest_close.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                           f"is stale by {age:.0f}s versus local now {now_ts.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        last_vals = df["close"].astype(float).iloc[-FROZEN_FEED_POINTS:]
        if len(last_vals) >= FROZEN_FEED_POINTS and last_vals.nunique() == 1:
            return False, (
                f"frozen feed: last {FROZEN_FEED_POINTS} completed 1m closes "
                f"all equal {last_vals.iloc[-1]:.2f}"
            )

        return True, "OK"

    # -------------------------------------------------------------------------
    # Broker position reconciliation
    # -------------------------------------------------------------------------

    @staticmethod
    def _option_type_from_symbol(symbol: str) -> Optional[str]:
        s = symbol.upper()
        if not NIFTY_OPTION_RE.match(s):
            return None
        if s.endswith("CE"):
            return "CE"
        if s.endswith("PE"):
            return "PE"
        return None

    def _positionbook(self) -> list[dict]:
        response = self.client.positionbook()
        if response.get("status") != "success":
            raise RuntimeError(f"positionbook() failed: {response}")
        data = response.get("data", [])
        return data if isinstance(data, list) else []

    def _get_broker_position(self, symbol: str) -> Optional[dict]:
        for item in self._positionbook():
            if (
                str(item.get("symbol", "")).upper() == symbol.upper()
                and str(item.get("exchange", "")).upper() == OPTIONS_EXCHANGE
                and str(item.get("product", "")).upper() == OPTION_PRODUCT
            ):
                return item
        return None

    def _reconcile_positions(self, verbose: bool = False) -> None:
        try:
            book = self._positionbook()
        except Exception as exc:
            self.entry_block_reason = f"Position reconciliation unavailable: {exc}"
            if verbose:
                self.log(f"WARNING: {self.entry_block_reason}")
            return

        broker_long: Dict[str, dict] = {}
        unmanaged: list[str] = []
        unexpected_short: list[str] = []

        for item in book:
            symbol = str(item.get("symbol", "")).upper()
            option_type = self._option_type_from_symbol(symbol)
            if not option_type:
                continue
            if str(item.get("exchange", "")).upper() != OPTIONS_EXCHANGE:
                continue
            if str(item.get("product", "")).upper() != OPTION_PRODUCT:
                continue

            qty = self._as_int(item.get("quantity"))
            if qty > 0:
                broker_long[symbol] = item
            elif qty < 0:
                unexpected_short.append(symbol)

        # Remove locally tracked positions that no longer exist at broker.
        # If our broker-side protective SL-M filled, record that realized result so
        # daily-loss/consecutive-loss guards remain correct.
        for symbol in list(self.positions):
            broker = broker_long.get(symbol.upper())
            if not broker:
                pos = self.positions.get(symbol)
                protective_exit_price = 0.0
                protective_status = ""
                if pos and pos.protective_stop_order_id:
                    try:
                        status_resp = self.client.orderstatus(
                            order_id=pos.protective_stop_order_id,
                            strategy=STRATEGY_NAME,
                        )
                        if status_resp.get("status") == "success":
                            status_data = status_resp.get("data", {})
                            protective_status = str(
                                status_data.get("order_status", "")
                            ).lower()
                            protective_exit_price = self._as_float(
                                status_data.get("average_price")
                            )
                    except Exception as exc:
                        if verbose:
                            self.log(
                                f"Could not inspect protective-stop fill for {symbol}: {exc}"
                            )

                if pos and protective_exit_price > 0:
                    realized = (protective_exit_price - pos.entry_price) * pos.quantity
                    self.daily_realized_pnl += realized
                    if realized < 0:
                        self.consecutive_losses += 1
                    else:
                        self.consecutive_losses = 0
                    self.log(
                        f"BROKER STOP REALIZED: {symbol} @ {protective_exit_price:.2f} "
                        f"Rs.{realized:+.2f} | day=Rs.{self.daily_realized_pnl:+.2f} | "
                        f"consecutive_losses={self.consecutive_losses}."
                    )
                    if (
                        self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES
                        and not (self.analyze_mode and ANALYZER_IGNORE_CONSECUTIVE_LOSS_BLOCK)
                    ):
                        self.daily_block_reason = (
                            f"{self.consecutive_losses} consecutive losing exits reached"
                        )
                    if (
                        MAX_DAILY_REALIZED_LOSS_RUPEES > 0
                        and self.daily_realized_pnl <= -MAX_DAILY_REALIZED_LOSS_RUPEES
                    ):
                        self.daily_block_reason = (
                            f"daily realized loss Rs.{abs(self.daily_realized_pnl):.2f} "
                            f"reached cap Rs.{MAX_DAILY_REALIZED_LOSS_RUPEES:.2f}"
                        )

                if verbose:
                    reason = (
                        f"; protective_order_status={protective_status}"
                        if protective_status
                        else ""
                    )
                    self.log(
                        f"Tracked position {symbol} is no longer open at broker; "
                        f"removing local state{reason}."
                    )
                self.positions.pop(symbol, None)
                self._save_state()
                continue

            pos = self.positions[symbol]
            qty = self._as_int(broker.get("quantity"), pos.quantity)
            avg = self._as_float(broker.get("average_price"), pos.entry_price)
            pos.quantity = qty
            if avg > 0 and abs(avg - pos.entry_price) > 1e-9:
                pos.entry_price = avg
                pos.stop_price = avg * (1.0 - STOPLOSS_PERCENT / 100.0)
                # +20% level is retained as continuous profit-trail activation reference.
                pos.target_price = avg * (1.0 + TARGET_PERCENT / 100.0)

        # Adopt untracked long NIFTY MIS positions only when explicitly enabled.
        tracked_upper = {s.upper() for s in self.positions}
        for symbol, broker in broker_long.items():
            if symbol in tracked_upper:
                continue

            if not MANAGE_ALL_NIFTY_MIS_POSITIONS:
                unmanaged.append(symbol)
                continue

            avg = self._as_float(broker.get("average_price"))
            qty = self._as_int(broker.get("quantity"))
            opt = self._option_type_from_symbol(symbol)
            if avg <= 0 or qty <= 0 or not opt:
                continue

            current_ltp = self._quote_ltp(symbol, OPTIONS_EXCHANGE)
            recovered_pnl = (
                ((current_ltp - avg) / avg) * 100.0
                if current_ltp is not None and avg > 0
                else 0.0
            )
            recovered_trailing = recovered_pnl >= TRAIL_ACTIVATION_PERCENT
            recovered_peak = max(0.0, recovered_pnl)
            recovered_floor = self._desired_trailing_floor(recovered_peak)
            self.positions[symbol] = ManagedPosition(
                symbol=symbol,
                exchange=OPTIONS_EXCHANGE,
                option_type=opt,
                quantity=qty,
                entry_price=avg,
                stop_price=avg * (1.0 - STOPLOSS_PERCENT / 100.0),
                target_price=avg * (1.0 + TARGET_PERCENT / 100.0),
                entry_time=self.now_ist().isoformat(),
                expiry="",
                trailing_active=recovered_trailing,
                peak_pnl_percent=recovered_peak,
                trailing_floor_percent=recovered_floor,
            )
            if verbose:
                note = (
                    f"; recovery profit floor=+{recovered_floor:.2f}%"
                    if recovered_trailing
                    else ""
                )
                self.log(
                    f"Adopted existing broker position {symbol}, qty={qty}, avg={avg:.2f}{note}."
                )

        # Recreate missing protective stops for any long managed position.
        # This also hardens restart recovery when local state survived but a stop
        # order was cancelled externally. If local state was lost, the newly
        # adopted position receives a fresh fixed -5% SL-M.
        # Analyzer sandbox currently rejects resting SL-M orders with its
        # sandbox order-status CHECK constraint. Do not retry those orders in
        # Analyzer mode; software hard-stop monitoring remains active.
        if BROKER_PROTECTIVE_STOP and not self.analyze_mode:
            for pos in list(self.positions.values()):
                if not self._protective_stop_is_working(pos):
                    self._place_protective_stop(pos)

        reasons = []
        if unmanaged:
            reasons.append(
                "Unmanaged long NIFTY MIS option(s): " + ", ".join(sorted(unmanaged))
                + ". Close them or set MANAGE_ALL_NIFTY_MIS_POSITIONS=true deliberately."
            )
        if unexpected_short:
            reasons.append(
                "Unexpected SHORT NIFTY MIS option position(s): "
                + ", ".join(sorted(unexpected_short))
                + ". This long-options strategy will not manage short positions."
            )

        self.entry_block_reason = " | ".join(reasons) if reasons else None
        self._save_state()

        if verbose:
            self.log(f"Broker reconciliation complete: {len(self.positions)} managed position(s).")
            if self.entry_block_reason:
                self.log(f"NEW ENTRIES BLOCKED: {self.entry_block_reason}")

    # -------------------------------------------------------------------------
    # Market data / candles / EMA
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_history_frame(data) -> Optional[pd.DataFrame]:
        """
        Normalize OpenAlgo history responses into a pandas DataFrame.

        Depending on the OpenAlgo/client version, history() may return:
          - a DataFrame,
          - a list of candle dictionaries,
          - a dict of column arrays,
          - or an API envelope such as {"status": "...", "data": [...]}.

        Never let a raw dict reach pd.concat().
        """
        if data is None:
            return None

        if isinstance(data, pd.DataFrame):
            return data.copy()

        # Common API envelope forms.
        if isinstance(data, dict):
            for key in ("data", "candles", "history", "result", "results"):
                if key in data and data[key] is not None:
                    nested = data[key]
                    # Avoid endlessly recursing if a provider returns itself.
                    if nested is not data:
                        frame = NiftyEMAOptionsStrategy._normalize_history_frame(nested)
                        if frame is not None:
                            return frame

            # A plain dict may itself be column-oriented.
            try:
                frame = pd.DataFrame(data)
                if not frame.empty:
                    return frame
            except Exception:
                pass

            # A single candle may be a scalar dict.
            try:
                frame = pd.DataFrame([data])
                if not frame.empty:
                    return frame
            except Exception:
                return None

        # Lists/tuples of candle dicts or row records.
        if isinstance(data, (list, tuple)):
            try:
                frame = pd.DataFrame(data)
                if not frame.empty:
                    return frame
            except Exception:
                return None

        # Last-resort conversion for other table-like objects.
        try:
            frame = pd.DataFrame(data)
            if not frame.empty:
                return frame
        except Exception:
            pass

        return None

    def _history_request(self, start_date: date, end_date: date) -> Optional[pd.DataFrame]:
        """Fetch one history window and always return a normalized DataFrame."""
        try:
            data = self.client.history(
                symbol=INDEX_SYMBOL,
                exchange=INDEX_EXCHANGE,
                interval=CANDLE_TIMEFRAME,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                source="api",
            )
        except TypeError:
            data = self.client.history(
                symbol=INDEX_SYMBOL,
                exchange=INDEX_EXCHANGE,
                interval=CANDLE_TIMEFRAME,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
        except Exception as exc:
            self._data_warning(
                f"history() failed for {start_date.isoformat()} to "
                f"{end_date.isoformat()}: {exc}"
            )
            return None

        frame = self._normalize_history_frame(data)
        if frame is None:
            self._data_warning(
                f"history() response for {start_date.isoformat()} to "
                f"{end_date.isoformat()} could not be normalized "
                f"(type={type(data).__name__})."
            )
            return None

        return frame

    def _bootstrap_history_if_needed(
        self, data: Optional[pd.DataFrame], now: datetime
    ) -> Optional[pd.DataFrame]:
        """
        Build enough 1-minute history at the opening bell.

        A few OpenAlgo data paths return only current-session candles for a
        multi-day request near 09:15. When that happens, walk backwards one
        calendar day at a time, fetch each day separately, and merge the frames.
        Weekends/holidays naturally contribute no rows and are skipped.
        """
        required = max(
            EMA_SLOW_PERIOD + 2,
            REGIME_CONTINUATION_POINTS,
            NOISE_LOOKBACK + 1,
        )
        target = max(required, BOOTSTRAP_HISTORY_TARGET_ROWS)

        frames = []
        initial_rows = 0
        if data is not None:
            data = self._normalize_history_frame(data)
            if data is not None:
                try:
                    initial_rows = len(data)
                    if initial_rows:
                        frames.append(data.copy())
                except Exception:
                    pass

        if initial_rows >= required:
            return data

        self._data_warning(
            f"history() returned only {initial_rows} row(s); need at least "
            f"{required}. Starting market-open prior-session bootstrap."
        )

        accumulated = initial_rows
        today = now.date()
        for days_back in range(1, BOOTSTRAP_HISTORY_MAX_CALENDAR_DAYS + 1):
            day = today - timedelta(days=days_back)
            day_data = self._history_request(day, day)
            day_data = self._normalize_history_frame(day_data)
            if day_data is None:
                continue
            try:
                rows = len(day_data)
            except Exception:
                rows = 0
            if rows <= 0:
                continue

            frames.append(day_data.copy())
            accumulated += rows
            if accumulated >= target:
                break

        if not frames:
            return data

        try:
            merged = pd.concat(frames, axis=0, ignore_index=False, sort=False)
        except Exception as exc:
            self._data_warning(f"market-open history bootstrap merge failed: {exc}")
            return data

        if len(merged) >= required and initial_rows < required:
            self.log(
                f"MARKET-OPEN HISTORY BOOTSTRAP: initial_rows={initial_rows} -> "
                f"merged_rows={len(merged)} using normalized prior-session DataFrames."
            )
        return merged

    def _prepare_closed_history_frame(
        self, data: Optional[pd.DataFrame], now: datetime
    ) -> Optional[pd.DataFrame]:
        """Normalize/filter a history response into validated completed 1m candles."""
        required = max(
            EMA_SLOW_PERIOD + 2,
            REGIME_CONTINUATION_POINTS,
            NOISE_LOOKBACK + 1,
        )
        if data is None or len(data) < required:
            return None

        df = data.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            if "timestamp" in df.columns:
                df.index = pd.to_datetime(df["timestamp"], errors="coerce")
            else:
                return None

        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            idx = idx.tz_localize(IST)
        else:
            idx = idx.tz_convert(IST)
        df.index = idx
        df = df[~df.index.isna()]
        df = df[~df.index.duplicated(keep="last")].sort_index()

        # A row timestamp is the 1-minute candle START. Only use completed candles.
        now_ts = pd.Timestamp(now)
        closed_mask = (df.index + pd.Timedelta(minutes=CANDLE_MINUTES)) <= now_ts
        df = df.loc[closed_mask].copy()
        if len(df) < required:
            return None

        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close"])
        if len(df) < required:
            return None

        df["ema5"] = df["close"].ewm(span=EMA_FAST_PERIOD, adjust=False).mean()
        df["ema9"] = df["close"].ewm(span=EMA_SLOW_PERIOD, adjust=False).mean()
        return df

    def _fetch_closed_candles(self, now: datetime) -> Optional[pd.DataFrame]:
        end_date = now.date()
        start_date = end_date - timedelta(days=LOOKBACK_DAYS)
        required = max(
            EMA_SLOW_PERIOD + 2,
            REGIME_CONTINUATION_POINTS,
            NOISE_LOOKBACK + 1,
        )

        data = self._history_request(start_date, end_date)
        if data is None:
            self._data_warning("history() returned no data for NIFTY NSE_INDEX 1m.")
            return None

        data = self._bootstrap_history_if_needed(data, now)
        if data is None or len(data) < required:
            rows = 0 if data is None else len(data)
            self._data_warning(
                f"history bootstrap still has only {rows} row(s); need at least {required}."
            )
            return None

        df = self._prepare_closed_history_frame(data, now)
        if df is None:
            self._data_warning(
                f"History response did not yield at least {required} valid completed 1m candles."
            )
            return None

        quality_ok, quality_reason = self._data_quality_ok(df, now)
        if not quality_ok:
            # Intermittent OpenAlgo history snapshots have occasionally returned a
            # stale prior-session tail. Retry once immediately before blocking.
            self._data_warning(
                f"market-data quality first check failed: {quality_reason}; "
                "performing one immediate fresh-history retry."
            )
            time.sleep(0.35)
            retry = self._history_request(start_date, end_date)
            retry = self._bootstrap_history_if_needed(retry, now) if retry is not None else None
            retry_df = self._prepare_closed_history_frame(retry, now)
            if retry_df is not None:
                retry_ok, retry_reason = self._data_quality_ok(retry_df, now)
                if retry_ok:
                    self.log(
                        "HISTORY RECOVERY SUCCESS: immediate retry restored a current, "
                        "validated completed-candle series."
                    )
                    df = retry_df
                else:
                    self._data_warning(f"market-data quality block after retry: {retry_reason}")
                    return None
            else:
                self._data_warning("market-data recovery retry did not return usable completed candles.")
                return None

        self._remember_market_snapshot(df, now)
        return df

    @staticmethod
    def _cross_at(df: pd.DataFrame, row_index: int) -> Optional[str]:
        if row_index <= 0 or row_index >= len(df):
            return None
        prev = df.iloc[row_index - 1]
        curr = df.iloc[row_index]

        if prev["ema5"] <= prev["ema9"] and curr["ema5"] > curr["ema9"]:
            return "BULLISH"
        if prev["ema5"] >= prev["ema9"] and curr["ema5"] < curr["ema9"]:
            return "BEARISH"
        return None

    def _latest_candle_close_time(self, df: pd.DataFrame) -> pd.Timestamp:
        return df.index[-1] + pd.Timedelta(minutes=CANDLE_MINUTES)

    def _set_pending_signal(self, direction: str, detected_at: pd.Timestamp, close: float) -> None:
        self.pending_signal = PendingSignal(
            direction=direction,
            detected_at=detected_at.to_pydatetime().astimezone(IST).isoformat(),
            crossover_close=float(close),
        )
        self.pending_signal_source = "fresh"
        self.log(
            f"{direction} EMA5/EMA9 crossover on closed 1m candle at "
            f"{detected_at.strftime('%H:%M:%S')}; confirmation window started."
        )

    def _current_ema_regime(self, df: pd.DataFrame) -> Optional[str]:
        if df is None or len(df) == 0:
            return None
        row = df.iloc[-1]
        ema5 = float(row["ema5"])
        ema9 = float(row["ema9"])
        if ema5 > ema9:
            return "BULLISH"
        if ema5 < ema9:
            return "BEARISH"
        return None

    def _regime_15m_continuation(
        self, df: pd.DataFrame, direction: str
    ) -> Tuple[bool, str]:
        """
        Practical 15-minute established-regime confirmation for 1-minute candles.

        Uses the latest 16 completed 1-minute closes (t-15 ... t).

        It does NOT require every candle to be green/red. Small pullbacks are
        allowed. The broader direction must still be confirmed.
        """
        if df is None or len(df) < REGIME_CONTINUATION_POINTS:
            return False, f"need {REGIME_CONTINUATION_POINTS} completed 1m points"

        recent = df.iloc[-REGIME_CONTINUATION_POINTS:]
        closes = [float(v) for v in recent["close"].tolist()]
        ema5s = [float(v) for v in recent["ema5"].tolist()]
        ema9s = [float(v) for v in recent["ema9"].tolist()]

        transitions = len(closes) - 1
        up_moves = sum(
            1 for i in range(transitions)
            if closes[i + 1] > closes[i]
        )
        down_moves = sum(
            1 for i in range(transitions)
            if closes[i + 1] < closes[i]
        )

        support_needed = max(1, int(transitions * 0.60 + 0.999))
        net_price_move = closes[-1] - closes[0]
        ema5_net_move = ema5s[-1] - ema5s[0]
        latest_spread = ema5s[-1] - ema9s[-1]

        if direction == "BULLISH":
            relationship_ok = ema5s[-1] > ema9s[-1]
            net_price_ok = net_price_move > 0
            ema_slope_ok = ema5_net_move > 0
            majority_ok = up_moves >= support_needed
            price_vs_fast_ok = closes[-1] > ema5s[-1]
            spread_ok = latest_spread > 0
        else:
            relationship_ok = ema5s[-1] < ema9s[-1]
            net_price_ok = net_price_move < 0
            ema_slope_ok = ema5_net_move < 0
            majority_ok = down_moves >= support_needed
            price_vs_fast_ok = closes[-1] < ema5s[-1]
            spread_ok = latest_spread < 0

        ok = (
            relationship_ok
            and net_price_ok
            and ema_slope_ok
            and majority_ok
            and price_vs_fast_ok
            and spread_ok
        )

        details = (
            f"closes={closes[0]:.2f} -> ... -> {closes[-1]:.2f}; "
            f"EMA5={ema5s[0]:.2f} -> {ema5s[-1]:.2f}; "
            f"EMA9_latest={ema9s[-1]:.2f}; "
            f"net_price={net_price_move:+.2f}; "
            f"EMA5_net={ema5_net_move:+.2f}; "
            f"up_moves={up_moves}/{transitions}; "
            f"down_moves={down_moves}/{transitions}; "
            f"support_needed={support_needed}; "
            f"relationship={relationship_ok}; "
            f"price_vs_EMA5={price_vs_fast_ok}; "
            f"confirmed={ok}"
        )
        return ok, details

    def _try_established_regime_entry(
        self, df: pd.DataFrame, now: datetime, source: str
    ) -> bool:
        if now.time() >= ENTRY_CUTOFF:
            return False

        direction = self._current_ema_regime(df)
        if direction is None:
            return False

        if self.last_traded_regime == direction:
            return False

        desired = "CE" if direction == "BULLISH" else "PE"
        if any(pos.option_type == desired for pos in self.positions.values()):
            self.last_traded_regime = direction
            self._save_state()
            return False

        confirms, details = self._regime_15m_continuation(df, direction)
        if not confirms:
            if source == "startup":
                self.log(
                    f"ESTABLISHED-REGIME ENTRY NOT READY: EMA regime={direction}; "
                    f"last {REGIME_CONTINUATION_MINUTES}m trend is not sufficiently "
                    f"{'upward' if direction == 'BULLISH' else 'downward'}. {details}"
                )
            return False

        self.log(
            f"ESTABLISHED-REGIME ENTRY CONFIRMED ({source}): EMA regime={direction}; "
            f"last {REGIME_CONTINUATION_MINUTES}m trend confirmed "
            f"{'upward' if direction == 'BULLISH' else 'downward'}. {details}"
        )
        return self._execute_confirmed_signal(direction)

    def _initialize_live_data(self, df: pd.DataFrame, now: datetime) -> None:
        latest_close = self._latest_candle_close_time(df)
        self.last_processed_candle_close = latest_close

        row = df.iloc[-1]
        ema5 = float(row["ema5"])
        ema9 = float(row["ema9"])
        direction = self._current_ema_regime(df)

        # If saved lock belongs to the opposite regime, market changed while
        # bot was offline, so clear that stale lock.
        if (
            direction
            and self.last_traded_regime
            and self.last_traded_regime != direction
        ):
            self.log(
                f"STARTUP REGIME CHANGE: saved lock={self.last_traded_regime}, "
                f"current regime={direction}; clearing stale regime lock."
            )
            self.last_traded_regime = None
            self._save_state()

        self.log(
            f"LIVE DATA INITIALIZED: last completed 1m candle="
            f"{latest_close.strftime('%H:%M:%S')} "
            f"| NIFTY close={float(row['close']):.2f} "
            f"| EMA5={ema5:.2f} | EMA9={ema9:.2f} "
            f"| {self._ema_relation(ema5, ema9)} "
            f"| regime_lock={self.last_traded_regime or 'NONE'}"
        )

        if direction is not None and self._has_wrong_side_position(direction):
            self._start_reversal_confirmation(
                direction,
                latest_close,
                float(row["close"]),
            )

        if direction is None:
            self.log(
                "EMA5 and EMA9 are equal at startup; waiting for a fresh crossover."
            )
            self.startup_existing_regime_window_used = True
            return

        # If this regime was already traded previously, do not re-enter it on startup.
        if self.last_traded_regime == direction:
            self.log(
                f"Startup EMA regime={direction} was already traded earlier. "
                "Waiting for a NEW fresh EMA5/EMA9 crossover."
            )
            self.startup_existing_regime_window_used = True
            return

        # One-time existing-regime observation window.
        startup_filter_ok, startup_filter_details = self._fluctuation_filter(
            df, direction
        )
        self.log(
            f"STARTUP FLUCTUATION CHECK: {startup_filter_details}"
        )

        self.startup_existing_regime_window_used = True
        self.pending_signal = PendingSignal(
            direction=direction,
            detected_at=latest_close.to_pydatetime().astimezone(IST).isoformat(),
            crossover_close=float(row["close"]),
        )
        self.pending_signal_source = "startup"
        self.log(
            f"STARTUP EXISTING EMA REGIME={direction}. "
            f"Monitoring this existing regime from {STARTUP_EXISTING_RETRY_MIN_MINUTES} "
            f"to {STARTUP_EXISTING_RETRY_MAX_MINUTES} minutes. "
            "A weak candle will be re-checked on the next completed 1m candle; "
            "an opposite EMA5/EMA9 crossover cancels this startup opportunity."
        )

    def _fluctuation_filter(
        self, df: pd.DataFrame, direction: str
    ) -> Tuple[bool, str]:
        """
        Practical anti-whipsaw filter for 1-minute NIFTY.

        We deliberately do NOT require every recent candle to agree or the EMA
        gap to expand on the very last candle. A strong move may pause for one
        minute and should not automatically be rejected.

        Latest 5 completed 1m candles must show:
        - correct EMA5/EMA9 relationship
        - latest price on the correct side of EMA5
        - net price movement in the signal direction
        - net EMA5 movement in the signal direction
        - at least 2 of the 4 close-to-close moves supporting the direction
        - current EMA separation is positive in the signal direction
        """
        if df is None or len(df) < FLUCTUATION_WINDOW_POINTS:
            return False, f"need {FLUCTUATION_WINDOW_POINTS} completed 1m candles"

        recent = df.iloc[-FLUCTUATION_WINDOW_POINTS:]
        closes = [float(v) for v in recent["close"].tolist()]
        ema5s = [float(v) for v in recent["ema5"].tolist()]
        ema9s = [float(v) for v in recent["ema9"].tolist()]

        transitions = len(closes) - 1
        up_moves = sum(
            1 for i in range(transitions)
            if closes[i + 1] > closes[i]
        )
        down_moves = sum(
            1 for i in range(transitions)
            if closes[i + 1] < closes[i]
        )

        net_price = closes[-1] - closes[0]
        ema5_net = ema5s[-1] - ema5s[0]
        signed_gap = ema5s[-1] - ema9s[-1]

        if direction == "BULLISH":
            relation_ok = signed_gap > 0
            price_side_ok = closes[-1] > ema5s[-1]
            net_price_ok = net_price > 0
            ema_slope_ok = ema5_net > 0
            support_ok = up_moves >= 2
            gap_ok = signed_gap > 0
        else:
            relation_ok = signed_gap < 0
            price_side_ok = closes[-1] < ema5s[-1]
            net_price_ok = net_price < 0
            ema_slope_ok = ema5_net < 0
            support_ok = down_moves >= 2
            gap_ok = signed_gap < 0

        ok = (
            relation_ok
            and price_side_ok
            and net_price_ok
            and ema_slope_ok
            and support_ok
            and gap_ok
        )

        details = (
            f"closes={' -> '.join(f'{x:.2f}' for x in closes)} | "
            f"EMA5={ema5s[0]:.2f}->{ema5s[-1]:.2f} | "
            f"EMA9={ema9s[-1]:.2f} | "
            f"net_price={net_price:+.2f} | EMA5_net={ema5_net:+.2f} | "
            f"gap={signed_gap:+.2f} | up_moves={up_moves}/{transitions} | "
            f"down_moves={down_moves}/{transitions} | "
            f"relation={relation_ok} | price_side={price_side_ok} | "
            f"support={support_ok} | confirmed={ok}"
        )
        return ok, details

    def _confirmation_continues(
        self, df: pd.DataFrame, latest_close: pd.Timestamp
    ) -> bool:
        """
        Higher-quality 1m confirmation:
        - current EMA relationship must still be valid
        - EMA5 slope must agree
        - at least 3 of latest 4 close-to-close moves support direction
        - crossover displacement must exceed recent 1m noise
        - EMA5/EMA9 separation must be meaningful vs noise
        - a recent pre-cross 5m high/low must have been broken
        - latest price must be on the correct side of EMA5
        - do not chase if latest price is too stretched away from EMA5
        """
        if not self.pending_signal:
            return False

        detected = pd.Timestamp(self.pending_signal.detected_at)
        if detected.tzinfo is None:
            detected = detected.tz_localize(IST)
        else:
            detected = detected.tz_convert(IST)

        close_times = df.index + pd.Timedelta(minutes=CANDLE_MINUTES)
        mask = (close_times >= detected) & (close_times <= latest_close)
        segment = df.loc[mask].copy()

        if len(segment) < 2:
            self.log(
                f"{self.pending_signal.direction} CONFIRM BLOCKED: "
                "need crossover candle plus 1 completed 1m confirmation candle."
            )
            return False

        direction = self.pending_signal.direction
        closes = segment["close"].astype(float).tolist()
        ema5 = segment["ema5"].astype(float).tolist()
        ema9 = segment["ema9"].astype(float).tolist()

        recent = df.iloc[-5:]
        recent_closes = recent["close"].astype(float).tolist()
        up_moves = sum(
            1 for i in range(len(recent_closes) - 1)
            if recent_closes[i + 1] > recent_closes[i]
        )
        down_moves = sum(
            1 for i in range(len(recent_closes) - 1)
            if recent_closes[i + 1] < recent_closes[i]
        )

        noise = max(self._recent_noise(df), 0.01)
        signed_gap = ema5[-1] - ema9[-1]
        displacement = closes[-1] - self.pending_signal.crossover_close
        ema_distance = closes[-1] - ema5[-1]

        # Pre-cross reference zone for breakout detection.
        first_idx = df.index.get_loc(segment.index[0])
        prior_start = max(0, first_idx - BREAKOUT_LOOKBACK)
        prior = df.iloc[prior_start:first_idx]
        if len(prior) < 2:
            self.log(f"{direction} CONFIRM BLOCKED: insufficient pre-cross breakout history.")
            return False
        prior_closes = prior["close"].astype(float)

        if direction == "BULLISH":
            relationship_ok = signed_gap > 0
            ema_slope_ok = ema5[-1] > ema5[0]
            support_ok = up_moves >= ENTRY_SUPPORT_MOVES
            progress_ok = displacement >= noise * MIN_PROGRESS_NOISE_MULT
            gap_ok = signed_gap >= noise * MIN_EMA_GAP_NOISE_MULT
            breakout_seen = max(closes) > float(prior_closes.max())
            price_side_ok = closes[-1] > ema5[-1]
            not_chasing = 0 <= ema_distance <= noise * MAX_EMA_DISTANCE_NOISE_MULT
        else:
            relationship_ok = signed_gap < 0
            ema_slope_ok = ema5[-1] < ema5[0]
            support_ok = down_moves >= ENTRY_SUPPORT_MOVES
            progress_ok = (-displacement) >= noise * MIN_PROGRESS_NOISE_MULT
            gap_ok = (-signed_gap) >= noise * MIN_EMA_GAP_NOISE_MULT
            breakout_seen = min(closes) < float(prior_closes.min())
            price_side_ok = closes[-1] < ema5[-1]
            not_chasing = 0 <= (-ema_distance) <= noise * MAX_EMA_DISTANCE_NOISE_MULT

        passed = all(
            [
                relationship_ok,
                ema_slope_ok,
                support_ok,
                progress_ok,
                gap_ok,
                breakout_seen,
                price_side_ok,
                not_chasing,
            ]
        )

        self.log(
            f"{direction} QUALITY CONFIRM: "
            f"relationship={relationship_ok} | EMA5_slope={ema_slope_ok} | "
            f"support={support_ok} ({up_moves} up/{down_moves} down) | "
            f"progress={progress_ok} ({displacement:+.2f}, noise={noise:.2f}) | "
            f"gap_ok={gap_ok} (gap={signed_gap:+.2f}) | "
            f"breakout={breakout_seen} | price_side={price_side_ok} | "
            f"not_chasing={not_chasing} | PASSED={passed}"
        )
        return passed

    def _evaluate_pending_confirmation(self, df: pd.DataFrame, latest_close: pd.Timestamp) -> None:
        if not self.pending_signal:
            return

        detected = pd.Timestamp(self.pending_signal.detected_at)
        if detected.tzinfo is None:
            detected = detected.tz_localize(IST)
        else:
            detected = detected.tz_convert(IST)

        elapsed = (latest_close - detected).total_seconds() / 60.0
        direction = self.pending_signal.direction
        source = self.pending_signal_source or "fresh"

        # Safety first: if the EMA regime itself has reversed, the old signal is invalid.
        current_regime = self._current_ema_regime(df)
        if current_regime != direction:
            self.log(
                f"{direction} pending signal cancelled: EMA5/EMA9 regime changed to "
                f"{current_regime or 'NEUTRAL'}."
            )
            self.pending_signal = None
            self.pending_signal_source = None
            return

        if source == "startup":
            min_wait = STARTUP_EXISTING_RETRY_MIN_MINUTES
            max_wait = STARTUP_EXISTING_RETRY_MAX_MINUTES
        else:
            min_wait = CONFIRMATION_MIN_MINUTES
            max_wait = CONFIRMATION_MAX_MINUTES

        if elapsed < min_wait:
            self.log(
                f"{direction} confirmation waiting: {elapsed:.0f}/{min_wait} minute(s) completed "
                f"| source={source}."
            )
            return

        # A quality pass is required for both fresh-cross and startup-existing entries.
        if self._confirmation_continues(df, latest_close):
            # ARBITRATION RULE:
            # If an opposite option is already open, entry confirmation is NOT allowed
            # to close it. Only the dedicated 2-3 minute reversal controller may do so.
            #
            # Keep this pending entry alive while the reversal watch is active. Once
            # reversal confirmation closes the wrong-side option, it arms the one-shot
            # reversal_entry_bypass_direction and this same candle may then enter the
            # new side. If reversal confirmation fails/cancels, cancel this entry signal
            # rather than using the entry engine to force-close the existing position.
            wrong_side_open = self._has_wrong_side_position(direction)
            reversal_ready = self.reversal_entry_bypass_direction == direction

            if wrong_side_open and not reversal_ready:
                reversal_watch_same_direction = (
                    self.pending_reversal is not None
                    and self.pending_reversal.direction == direction
                )

                if reversal_watch_same_direction:
                    self.log(
                        f"{direction} ENTRY READY but DEFERRED: quality filters passed "
                        f"after {elapsed:.0f} minute(s), but an opposite position is still open. "
                        f"REVERSAL PRIORITY is active; waiting for the dedicated "
                        f"{REVERSAL_CONFIRM_MIN_MINUTES}-{REVERSAL_CONFIRM_MAX_MINUTES}m "
                        "reversal confirmation before any exit/reverse entry."
                    )
                    return

                self.log(
                    f"{direction} ENTRY CANCELLED: quality filters passed, but the existing "
                    "opposite position was NOT closed by the dedicated reversal controller. "
                    "Entry logic is not permitted to force-close it."
                )
                self.pending_signal = None
                self.pending_signal_source = None
                return

            self.log(
                f"{direction} CONTINUOUS TREND CONFIRMED after {elapsed:.0f} minute(s) "
                f"| source={source}. "
                + ("Preparing ITM3 CE trade." if direction == "BULLISH" else "Preparing ITM3 PE trade.")
            )
            self.pending_signal = None
            self.pending_signal_source = None
            self._execute_confirmed_signal(direction)
            return

        # Fresh crossover: keep the signal alive for a short bounded retry window.
        # A single stretched/weak candle should not permanently discard an otherwise
        # valid EMA5/EMA9 regime. Re-check each completed 1m candle while the EMA
        # relationship remains intact, but never chase beyond the configured filters.
        if source != "startup":
            if elapsed >= max_wait:
                self.log(
                    f"{direction} fresh-cross regime did not pass quality filters within "
                    f"{max_wait} minute(s); signal cancelled. Waiting for a fresh "
                    "EMA5/EMA9 crossover."
                )
                self.pending_signal = None
                self.pending_signal_source = None
                return

            self.log(
                f"{direction} fresh-cross regime still valid but quality filters did not "
                f"pass at {elapsed:.0f} minute(s); will re-check next completed 1m candle "
                f"until {max_wait} minutes. "
                f"No-chase limit={MAX_EMA_DISTANCE_NOISE_MULT:.1f}x recent noise."
            )
            return

        # Startup existing regime: keep it alive for a bounded retry window.
        if elapsed >= max_wait:
            self.log(
                f"{direction} existing startup regime did not pass quality filters within "
                f"{max_wait} minutes; startup opportunity cancelled. Waiting for a fresh "
                "EMA5/EMA9 crossover."
            )
            self.pending_signal = None
            self.pending_signal_source = None
            return

        self.log(
            f"{direction} existing startup regime still valid but quality filters did not "
            f"pass at {elapsed:.0f} minute(s); will re-check next completed 1m candle "
            f"until {max_wait} minutes."
        )

    def _process_candle_signal(self, df: pd.DataFrame, now: datetime) -> None:
        latest_close = self._latest_candle_close_time(df)

        if self.last_processed_candle_close is None:
            self._initialize_live_data(df, now)
            return

        if latest_close <= self.last_processed_candle_close:
            return

        self.last_processed_candle_close = latest_close

        row = df.iloc[-1]
        ema5 = float(row["ema5"])
        ema9 = float(row["ema9"])
        self.log(
            f"1M CANDLE CLOSED: {latest_close.strftime('%H:%M:%S')} "
            f"| NIFTY close={float(row['close']):.2f} | EMA5={ema5:.2f} | EMA9={ema9:.2f} "
            f"| {self._ema_relation(ema5, ema9)} "
            f"| pending={(self.pending_signal.direction if self.pending_signal else 'NONE')}"
        )

        direction = self._cross_at(df, len(df) - 1)

        if direction:
            # Any genuinely new crossover supersedes an unused prior bypass.
            if self.reversal_entry_bypass_direction and self.reversal_entry_bypass_direction != direction:
                self.reversal_entry_bypass_direction = None

            # Fresh crossover starts TWO clocks:
            # 1) 2-3m reversal confirmation before closing the currently-held
            #    wrong-side option.
            # 2) 1-3m bounded quality-confirmation retry before entering the new-side option.
            self.last_traded_regime = None
            self._save_state()
            self.log(
                f"FRESH {direction} EMA5/EMA9 CROSS: previous regime lock cleared."
            )

            self._start_reversal_confirmation(
                direction,
                latest_close,
                float(df.iloc[-1]["close"]),
            )

            self._set_pending_signal(
                direction,
                latest_close,
                float(df.iloc[-1]["close"]),
            )

        # First handle any short 2-3m reversal-exit confirmation.
        self._evaluate_pending_reversal(df, latest_close)

        # New-side entry uses the bounded 1-3m fresh-cross confirmation/retry window.
        self._evaluate_pending_confirmation(df, latest_close)

        # No repeated established-regime entries here.
        # After the one-time startup existing-regime window, all future entries
        # require a genuinely fresh EMA5/EMA9 crossover.

    # -------------------------------------------------------------------------
    # Expiry / option symbol resolution
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_expiry(raw: str) -> Optional[date]:
        raw = str(raw).strip().upper()
        for fmt in ("%d-%b-%y", "%d%b%y", "%d-%b-%Y", "%d%b%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    def _nearest_expiry(self, force_refresh: bool = False) -> str:
        """
        Return the nearest current/future NIFTY option expiry.

        Past expiries are explicitly rejected. The daily cache is used only
        when the cached expiry still parses to a date >= today.
        """
        today = self.now_ist().date()

        if (
            not force_refresh
            and self.expiry_cache_date == today
            and self.expiry_cache_value
        ):
            cached_date = self._parse_expiry(self.expiry_cache_value)
            if cached_date and cached_date >= today:
                return self.expiry_cache_value

            self.log(
                f"EXPIRY CACHE INVALIDATED: cached={self.expiry_cache_value} "
                f"is before today={today.isoformat()}."
            )
            self.expiry_cache_value = None
            self.expiry_cache_date = None

        response = self.client.expiry(
            symbol=INDEX_SYMBOL,
            exchange=OPTIONS_EXCHANGE,
            instrumenttype="options",
        )
        if not isinstance(response, dict) or response.get("status") != "success":
            raise RuntimeError(f"expiry() failed: {response}")

        raw_expiries = response.get("data", [])
        if not isinstance(raw_expiries, (list, tuple)):
            raise RuntimeError(f"expiry() returned unexpected data: {response}")

        candidates: list[Tuple[date, str]] = []
        expired: list[str] = []
        unparsed: list[str] = []

        for raw in raw_expiries:
            parsed = self._parse_expiry(raw)
            if parsed is None:
                unparsed.append(str(raw))
                continue
            if parsed < today:
                expired.append(str(raw))
                continue
            candidates.append((parsed, str(raw)))

        self.log(
            f"EXPIRY ROLLOVER CHECK: today={today.isoformat()} | "
            f"returned={list(raw_expiries)} | expired_skipped={expired} | "
            f"unparsed_skipped={unparsed}."
        )

        if not candidates:
            raise RuntimeError(
                f"No current/future NIFTY option expiry returned for "
                f"{today.isoformat()}: {response}"
            )

        candidates.sort(key=lambda x: x[0])
        expiry_date = candidates[0][0]

        if expiry_date < today:
            raise RuntimeError(
                f"Expiry rollover safety failure: selected expiry "
                f"{expiry_date.isoformat()} is before today {today.isoformat()}."
            )

        expiry_for_api = expiry_date.strftime("%d%b%y").upper()

        self.expiry_cache_date = today
        self.expiry_cache_value = expiry_for_api
        self.log(
            f"EXPIRY ROLLOVER VERIFIED: selected nearest valid NIFTY expiry "
            f"{expiry_for_api} ({expiry_date.isoformat()})."
        )
        return expiry_for_api

    def _resolve_itm3(self, direction: str) -> dict:
        option_type = "CE" if direction == "BULLISH" else "PE"

        # First use the normal nearest-valid expiry. If optionsymbol fails,
        # force one fresh expiry() lookup in case the master contract rolled
        # over after the previous cached response.
        expiry = self._nearest_expiry()

        response = self.client.optionsymbol(
            underlying=INDEX_SYMBOL,
            exchange=INDEX_EXCHANGE,
            expiry_date=expiry,
            offset=OPTION_OFFSET,
            option_type=option_type,
        )

        if not isinstance(response, dict) or response.get("status") != "success":
            self.log(
                f"OPTION EXPIRY RESOLVE RETRY: optionsymbol failed for "
                f"expiry={expiry}; forcing fresh expiry rollover lookup."
            )
            expiry = self._nearest_expiry(force_refresh=True)
            response = self.client.optionsymbol(
                underlying=INDEX_SYMBOL,
                exchange=INDEX_EXCHANGE,
                expiry_date=expiry,
                offset=OPTION_OFFSET,
                option_type=option_type,
            )

        if not isinstance(response, dict) or response.get("status") != "success":
            raise RuntimeError(
                f"optionsymbol() failed after expiry rollover refresh: {response}"
            )

        symbol = str(response.get("symbol", "")).upper()
        exchange = str(response.get("exchange", OPTIONS_EXCHANGE)).upper()
        lot_size = self._as_int(response.get("lotsize"))

        if not symbol or exchange != OPTIONS_EXCHANGE or lot_size <= 0:
            raise RuntimeError(f"Invalid optionsymbol() response: {response}")

        # Size the trade from the actual option premium. The contract lot size
        # is exchange-defined; only the number of lots is dynamic.
        option_ltp = self._quote_ltp(symbol, exchange)
        if option_ltp is None or option_ltp <= 0:
            raise RuntimeError(f"Cannot size {symbol}: option LTP unavailable")

        cost_per_lot = option_ltp * lot_size
        risk_per_lot = cost_per_lot * (STOPLOSS_PERCENT / 100.0)
        risk_budget = TRADING_CAPITAL_RUPEES * (RISK_PER_TRADE_PERCENT / 100.0)
        capital_budget = TRADING_CAPITAL_RUPEES * (MAX_CAPITAL_DEPLOYMENT_PERCENT / 100.0)

        lots_by_risk = int(risk_budget // risk_per_lot) if risk_per_lot > 0 else 0
        lots_by_capital = int(capital_budget // cost_per_lot) if cost_per_lot > 0 else 0
        dynamic_lots = min(lots_by_risk, lots_by_capital)

        if dynamic_lots < 1:
            raise RuntimeError(
                f"Capital sizing blocks {symbol}: LTP={option_ltp:.2f}, lot_size={lot_size}, "
                f"cost/lot=Rs.{cost_per_lot:.2f}, risk/lot=Rs.{risk_per_lot:.2f}, "
                f"risk_budget=Rs.{risk_budget:.2f}, capital_budget=Rs.{capital_budget:.2f}."
            )

        quantity = lot_size * dynamic_lots
        estimated_deployment = option_ltp * quantity
        estimated_stop_risk = estimated_deployment * (STOPLOSS_PERCENT / 100.0)

        self.log(
            f"CAPITAL SIZING: effective_capital=Rs.{TRADING_CAPITAL_RUPEES:,.0f} "
            f"(allowed band Rs.{MIN_TRADING_CAPITAL_RUPEES:,.0f}-"
            f"Rs.{MAX_TRADING_CAPITAL_RUPEES:,.0f}) | "
            f"risk/trade={RISK_PER_TRADE_PERCENT:.2f}% "
            f"(Rs.{risk_budget:,.0f}) | "
            f"max_deployment_budget=Rs.{capital_budget:,.0f} | "
            f"option={symbol} LTP={option_ltp:.2f} | exchange_lot={lot_size} | "
            f"dynamic_lots={dynamic_lots} | qty={quantity} | "
            f"estimated_deployment=Rs.{estimated_deployment:,.0f} | "
            f"estimated_5pct_stop_risk=Rs.{estimated_stop_risk:,.0f}."
        )

        return {
            "symbol": symbol,
            "exchange": exchange,
            "lot_size": lot_size,
            "lots": dynamic_lots,
            "quantity": quantity,
            "sizing_ltp": option_ltp,
            "estimated_deployment": estimated_deployment,
            "estimated_stop_risk": estimated_stop_risk,
            "option_type": option_type,
            "expiry": expiry,
            "underlying_ltp": self._as_float(response.get("underlying_ltp")),
        }

    # -------------------------------------------------------------------------
    # Orders
    # -------------------------------------------------------------------------

    def _wait_for_fill(self, order_id: str, timeout: float) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        last_response: Optional[dict] = None

        while time.monotonic() < deadline:
            try:
                response = self.client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
                last_response = response
                if response.get("status") == "success":
                    data = response.get("data", {})
                    status = str(data.get("order_status", "")).lower()
                    if status == "complete":
                        return data
                    if status in {"rejected", "cancelled", "canceled"}:
                        self.log(f"Order {order_id} ended with status={status}: {response}")
                        return None
            except Exception as exc:
                self.log(f"orderstatus() transient error for {order_id}: {exc}")
            time.sleep(ORDER_STATUS_POLL_SECONDS)

        self.log(f"Order {order_id} fill status timed out. Last response: {last_response}")
        return None

    @staticmethod
    def _round_to_option_tick(price: float) -> float:
        if price <= 0:
            return 0.0
        return round(round(price / OPTION_TICK_SIZE) * OPTION_TICK_SIZE, 2)

    def _place_protective_stop(self, pos: ManagedPosition) -> bool:
        """Place broker-side SELL SL-M at the fixed -5% trigger in LIVE mode.

        OpenAlgo Analyzer/sandbox currently rejects resting SL-M orders because
        the sandbox database does not accept the returned ``trigger pending``
        status. In Analyzer mode we therefore deliberately skip the broker stop
        and keep the software -5% stop active.
        """
        if not BROKER_PROTECTIVE_STOP:
            return True
        if self.analyze_mode:
            if not self.analyzer_sl_skip_logged:
                self.log(
                    "ANALYZER MODE: broker-side SL-M skipped because the OpenAlgo "
                    "sandbox rejects resting trigger-pending stop orders. "
                    "Software -5% hard-stop monitoring remains active."
                )
                self.analyzer_sl_skip_logged = True
            return True
        if pos.protective_stop_order_id:
            return True

        trigger = self._round_to_option_tick(pos.stop_price)
        if trigger <= 0:
            return False
        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=pos.symbol,
                exchange=pos.exchange,
                action="SELL",
                quantity=pos.quantity,
                price_type="SL-M",
                product=OPTION_PRODUCT,
                trigger_price=trigger,
            )
        except Exception as exc:
            self.log(f"CRITICAL: protective SL-M exception for {pos.symbol}: {exc}")
            return False

        if response.get("status") != "success" or not response.get("orderid"):
            self.log(f"CRITICAL: protective SL-M rejected for {pos.symbol}: {response}")
            return False

        pos.protective_stop_order_id = str(response["orderid"])
        self._save_state()
        self.log(
            f"BROKER PROTECTIVE STOP ACTIVE: {pos.symbol} SELL SL-M qty={pos.quantity} "
            f"trigger={trigger:.2f} (-{STOPLOSS_PERCENT:.0f}%), "
            f"order_id={pos.protective_stop_order_id}."
        )
        return True

    def _protective_stop_is_working(self, pos: ManagedPosition) -> bool:
        if not pos.protective_stop_order_id:
            return False
        try:
            response = self.client.orderstatus(
                order_id=pos.protective_stop_order_id, strategy=STRATEGY_NAME
            )
            if response.get("status") != "success":
                return True  # Do not duplicate a stop merely because status lookup failed.
            data = response.get("data", {})
            status = str(data.get("order_status", "")).strip().lower()
            terminal = {"complete", "completed", "cancelled", "canceled", "rejected"}
            if status in terminal:
                self.log(
                    f"Protective stop {pos.protective_stop_order_id} for {pos.symbol} "
                    f"is {status}; it will be recreated if the long position remains open."
                )
                pos.protective_stop_order_id = ""
                self._save_state()
                return False
            return True
        except Exception as exc:
            self.log(
                f"WARNING: could not verify protective stop for {pos.symbol}: {exc}. "
                "Keeping the saved stop id to avoid accidental duplicate SELL stops."
            )
            return True

    def _protective_stop_verified_strict(self, pos: ManagedPosition) -> bool:
        """Strict post-entry LIVE verification; lookup failures are NOT treated as success."""
        if self.analyze_mode or not BROKER_PROTECTIVE_STOP:
            return True
        if not pos.protective_stop_order_id:
            return False

        working = {"trigger pending", "trigger_pending", "pending", "open", "put order req received", "validation pending"}
        for attempt in range(1, 4):
            try:
                response = self.client.orderstatus(
                    order_id=pos.protective_stop_order_id,
                    strategy=STRATEGY_NAME,
                )
                if response.get("status") == "success":
                    data = response.get("data", {}) or {}
                    status = str(data.get("order_status", "")).strip().lower()
                    if status in working or "trigger" in status or "pending" in status or "open" in status:
                        self.log(
                            f"LIVE SL VERIFIED: {pos.symbol} protective order "
                            f"{pos.protective_stop_order_id} status={status or 'working'}."
                        )
                        return True
                    if status in {"complete", "completed"}:
                        # The stop may already have filled during verification.
                        self.log(
                            f"LIVE SL NOTICE: {pos.symbol} protective stop completed during "
                            "post-entry verification; broker reconciliation will confirm flat state."
                        )
                        self._reconcile_positions(verbose=False)
                        return pos.symbol not in self.positions
                    if status in {"rejected", "cancelled", "canceled"}:
                        self.log(
                            f"LIVE SL VERIFY FAIL: {pos.symbol} protective order status={status}."
                        )
                        return False
            except Exception as exc:
                self.log(
                    f"LIVE SL VERIFY RETRY {attempt}/3 for {pos.symbol}: {exc}"
                )
            time.sleep(0.40 * attempt)
        return False

    def _enforce_live_protection_after_entry(self, pos: ManagedPosition) -> bool:
        """
        LIVE safety contract: a newly opened position must have a verified broker
        SL-M. If not, flatten immediately and block further entries for manual review.
        """
        if self.analyze_mode or not BROKER_PROTECTIVE_STOP:
            return True

        for attempt in range(1, 3):
            if not pos.protective_stop_order_id:
                self.log(
                    f"LIVE SL PLACE attempt {attempt}/2 for {pos.symbol}."
                )
                self._place_protective_stop(pos)
            if self._protective_stop_verified_strict(pos):
                return True

            # Clear an invalid/rejected id before one controlled retry.
            pos.protective_stop_order_id = ""
            self._save_state()

        reason = "LIVE SAFETY FAIL - protective SL-M could not be verified"
        self.log(
            f"CRITICAL: {pos.symbol} has no verified broker-side protective stop. "
            "Flattening immediately; new entries will be blocked for manual review."
        )
        self._place_exit(pos.symbol, reason)
        self.entry_block_reason = (
            f"{pos.symbol}: protective SL-M could not be verified after entry; "
            "manual broker/orderbook review required"
        )
        return False

    def _cancel_protective_stop(self, pos: ManagedPosition) -> None:
        order_id = pos.protective_stop_order_id
        if not order_id:
            return
        try:
            response = self.client.cancelorder(order_id=order_id, strategy=STRATEGY_NAME)
            if response.get("status") == "success":
                self.log(f"Protective stop cancelled for {pos.symbol}: order_id={order_id}.")
            else:
                self.log(
                    f"Protective stop cancel returned {response} for {pos.symbol}; "
                    "re-checking broker quantity before any manual exit."
                )
        except Exception as exc:
            self.log(
                f"Protective stop cancel exception for {pos.symbol}: {exc}; "
                "re-checking broker quantity before any manual exit."
            )
        finally:
            pos.protective_stop_order_id = ""
            self._save_state()

    def _place_entry(self, contract: dict) -> bool:
        symbol = contract["symbol"]
        quantity = int(contract["quantity"])

        entry_lots = int(contract.get("lots", max(1, quantity // max(1, int(contract.get("lot_size", 1))))))
        self.log(
            f"ENTRY: BUY {symbol} qty={quantity} ({entry_lots} lot(s) x {contract['lot_size']}) at MARKET; "
            f"expiry={contract['expiry']}, offset={OPTION_OFFSET}."
        )

        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=symbol,
                exchange=contract["exchange"],
                action="BUY",
                quantity=quantity,
                price_type="MARKET",
                product=OPTION_PRODUCT,
            )
        except Exception as exc:
            self.log(f"Entry order exception: {exc}")
            return False

        if response.get("status") != "success" or not response.get("orderid"):
            self.log(f"Entry order rejected/failed: {response}")
            return False

        order_id = str(response["orderid"])
        fill = self._wait_for_fill(order_id, ORDER_FILL_TIMEOUT_SECONDS)

        entry_price = 0.0
        filled_qty = quantity
        if fill:
            entry_price = self._as_float(fill.get("average_price"))
            filled_qty = self._as_int(fill.get("quantity"), quantity)

        # If status polling timed out, reconcile once to avoid losing a late fill.
        if entry_price <= 0:
            try:
                broker = self._get_broker_position(symbol)
                if broker and self._as_int(broker.get("quantity")) > 0:
                    entry_price = self._as_float(broker.get("average_price"))
                    filled_qty = self._as_int(broker.get("quantity"), quantity)
                    self.log(
                        f"Recovered entry from broker positionbook: {symbol}, "
                        f"qty={filled_qty}, avg={entry_price:.2f}."
                    )
            except Exception as exc:
                self.log(f"Could not reconcile timed-out entry order: {exc}")

        if entry_price <= 0 or filled_qty <= 0:
            self.entry_block_reason = (
                f"Entry order {order_id} could not be verified. Manual broker check required before new entries."
            )
            self.log(f"CRITICAL: {self.entry_block_reason}")
            return False

        position = ManagedPosition(
            symbol=symbol,
            exchange=contract["exchange"],
            option_type=contract["option_type"],
            quantity=filled_qty,
            entry_price=entry_price,
            stop_price=entry_price * (1.0 - STOPLOSS_PERCENT / 100.0),
            # +20% is the profit-trail activation reference.
            target_price=entry_price * (1.0 + TARGET_PERCENT / 100.0),
            entry_time=self.now_ist().isoformat(),
            expiry=contract["expiry"],
            trailing_active=False,
            peak_pnl_percent=0.0,
            trailing_floor_percent=-STOPLOSS_PERCENT,
        )
        self.positions[symbol] = position
        self.daily_trade_count += 1
        self._save_state()

        self.log(
            f"FILLED: {symbol} avg={entry_price:.2f}, qty={filled_qty}, "
            f"SL={position.stop_price:.2f} (-{STOPLOSS_PERCENT:.0f}%), "
            f"protection=<+5% hard-stop only; +5-<10% floor=-1%; "
            f"+10-<20% trail=7pp; +20-<50% trail=5pp; "
            f"+50-<70% trail=7pp; +70% trail=10pp | "
            f"daily_entries={self.daily_trade_count} (trade-count cap disabled)."
        )

        # Analyzer intentionally skips resting broker SL-M. LIVE requires a
        # verified broker-side stop and will flatten immediately if verification fails.
        if not self._place_protective_stop(position):
            if self.analyze_mode:
                self.log(
                    f"ANALYZER: broker stop unavailable for {symbol}; software -5% "
                    "hard-stop monitoring remains active."
                )
            else:
                if not self._enforce_live_protection_after_entry(position):
                    return False
        elif not self._enforce_live_protection_after_entry(position):
            return False
        return True

    def _place_exit(self, symbol: str, reason: str) -> bool:
        pos = self.positions.get(symbol)
        if not pos:
            return True

        try:
            broker = self._get_broker_position(symbol)
        except Exception as exc:
            self.log(f"Cannot verify broker quantity before exit of {symbol}: {exc}")
            return False

        if not broker or self._as_int(broker.get("quantity")) <= 0:
            self.log(f"{symbol} is already closed at broker; clearing local state.")
            self.positions.pop(symbol, None)
            self._save_state()
            return True

        quantity = self._as_int(broker.get("quantity"))

        # Prevent a resting protective SELL stop from firing after the manual
        # market exit and accidentally creating a short position.
        self._cancel_protective_stop(pos)
        try:
            broker = self._get_broker_position(symbol)
            quantity = self._as_int(broker.get("quantity")) if broker else 0
        except Exception as exc:
            self.log(f"Cannot re-check broker quantity after stop cancellation for {symbol}: {exc}")
            return False
        if quantity <= 0:
            self.log(f"{symbol} became flat while cancelling protective stop; clearing local state.")
            self.positions.pop(symbol, None)
            self._save_state()
            return True

        self.log(f"EXIT: SELL {symbol} qty={quantity} at MARKET. Reason: {reason}.")

        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=symbol,
                exchange=pos.exchange,
                action="SELL",
                quantity=quantity,
                price_type="MARKET",
                product=OPTION_PRODUCT,
            )
        except Exception as exc:
            self.log(f"Exit order exception for {symbol}: {exc}")
            return False

        if response.get("status") != "success" or not response.get("orderid"):
            self.log(f"Exit order failed for {symbol}: {response}")
            return False

        order_id = str(response["orderid"])
        exit_fill = self._wait_for_fill(order_id, ORDER_FILL_TIMEOUT_SECONDS)
        exit_price = self._as_float(exit_fill.get("average_price")) if exit_fill else 0.0

        deadline = time.monotonic() + EXIT_VERIFY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                broker = self._get_broker_position(symbol)
                remaining = self._as_int(broker.get("quantity")) if broker else 0
                if remaining <= 0:
                    closed = self.positions.pop(symbol, None)

                    if closed and exit_price > 0:
                        realized = (exit_price - closed.entry_price) * closed.quantity
                        self.daily_realized_pnl += realized
                        if realized < 0:
                            self.consecutive_losses += 1
                        else:
                            self.consecutive_losses = 0

                        self.log(
                            f"REALIZED: {symbol} Rs.{realized:+.2f} | "
                            f"day=Rs.{self.daily_realized_pnl:+.2f} | "
                            f"consecutive_losses={self.consecutive_losses}."
                        )

                        if (
                            self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES
                            and not (self.analyze_mode and ANALYZER_IGNORE_CONSECUTIVE_LOSS_BLOCK)
                        ):
                            self.daily_block_reason = (
                                f"{self.consecutive_losses} consecutive losing exits reached"
                            )

                        if (
                            MAX_DAILY_REALIZED_LOSS_RUPEES > 0
                            and self.daily_realized_pnl <= -MAX_DAILY_REALIZED_LOSS_RUPEES
                        ):
                            self.daily_block_reason = (
                                f"daily realized loss Rs.{abs(self.daily_realized_pnl):.2f} "
                                f"reached cap Rs.{MAX_DAILY_REALIZED_LOSS_RUPEES:.2f}"
                            )

                    self._save_state()
                    price_note = f" @ {exit_price:.2f}" if exit_price > 0 else ""
                    self.log(f"EXIT VERIFIED: {symbol} is flat at broker{price_note}.")
                    return True
                pos.quantity = remaining
                self._save_state()
            except Exception as exc:
                self.log(f"Exit verification transient error for {symbol}: {exc}")
            time.sleep(0.5)

        self.log(
            f"CRITICAL: Exit for {symbol} could not be verified. Position remains tracked; "
            "new opposite entry will be blocked."
        )
        return False

    def _square_off_option_type(self, option_type: str, reason: str) -> bool:
        symbols = [
            symbol for symbol, pos in self.positions.items() if pos.option_type.upper() == option_type.upper()
        ]
        ok = True
        for symbol in symbols:
            if not self._place_exit(symbol, reason):
                ok = False
        return ok

    def _exit_wrong_side_on_regime_change(
        self, direction: str, reason: str
    ) -> bool:
        """
        Risk-control rule:
        - BULLISH EMA regime => CE is desired, PE is wrong-side.
        - BEARISH EMA regime => PE is desired, CE is wrong-side.

        Exit wrong-side positions immediately on a fresh EMA crossover.
        Do NOT wait 1 minute to close a position that is now opposite
        to the NIFTY EMA regime. New entry still waits for confirmation.
        """
        self._reconcile_positions(verbose=False)

        wrong_side = "PE" if direction == "BULLISH" else "CE"
        symbols = [
            symbol
            for symbol, pos in self.positions.items()
            if pos.option_type.upper() == wrong_side
        ]

        if not symbols:
            return True

        self.log(
            f"REGIME REVERSAL RISK EXIT: {direction} detected; "
            f"closing {wrong_side} position(s) immediately before confirmation."
        )

        ok = True
        for symbol in symbols:
            if not self._place_exit(symbol, reason):
                ok = False

        return ok

    def _has_wrong_side_position(self, direction: str) -> bool:
        wrong_side = "PE" if direction == "BULLISH" else "CE"
        return any(
            pos.option_type.upper() == wrong_side
            for pos in self.positions.values()
        )

    def _start_reversal_confirmation(
        self, direction: str, detected_at: pd.Timestamp, close: float
    ) -> None:
        """
        Start a short 2-3 minute confirmation window before closing the
        currently-held option that is opposite to the new EMA regime.
        """
        self._reconcile_positions(verbose=False)

        if not self._has_wrong_side_position(direction):
            self.pending_reversal = None
            return

        self.pending_reversal = PendingSignal(
            direction=direction,
            detected_at=detected_at.to_pydatetime().astimezone(IST).isoformat(),
            crossover_close=float(close),
        )
        self.log(
            f"REVERSAL WATCH STARTED: {direction}; holding current position for "
            f"{REVERSAL_CONFIRM_MIN_MINUTES}-{REVERSAL_CONFIRM_MAX_MINUTES}m "
            "before reversal exit confirmation."
        )

    def _reversal_confirmed(
        self, df: pd.DataFrame, latest_close: pd.Timestamp
    ) -> bool:
        """
        Confirm that an opposite EMA crossover is real enough to close the
        currently-held option.

        Conditions:
        - EMA5/EMA9 relationship still supports the new direction
        - EMA5 has moved in that direction since crossover
        - latest close is on the correct side of EMA5
        - price has moved beyond the crossover close in the new direction
        - recent completed 1m closes predominantly support the new direction
        """
        if not self.pending_reversal:
            return False

        detected = pd.Timestamp(self.pending_reversal.detected_at)
        if detected.tzinfo is None:
            detected = detected.tz_localize(IST)
        else:
            detected = detected.tz_convert(IST)

        close_times = df.index + pd.Timedelta(minutes=CANDLE_MINUTES)
        segment = df.loc[
            (close_times >= detected) & (close_times <= latest_close)
        ].copy()

        if len(segment) < 3:
            return False

        closes = segment["close"].astype(float).tolist()
        ema5 = segment["ema5"].astype(float).tolist()
        ema9 = segment["ema9"].astype(float).tolist()

        transitions = len(closes) - 1
        up_moves = sum(
            1 for i in range(transitions)
            if closes[i + 1] > closes[i]
        )
        down_moves = sum(
            1 for i in range(transitions)
            if closes[i + 1] < closes[i]
        )

        # At 2 minutes there are usually 2 transitions: require both.
        # At 3 minutes there are usually 3 transitions: require at least 2.
        support_needed = 2 if transitions >= 2 else 1

        direction = self.pending_reversal.direction

        if direction == "BULLISH":
            relationship_ok = ema5[-1] > ema9[-1]
            ema_slope_ok = ema5[-1] > ema5[0]
            price_side_ok = closes[-1] > ema5[-1]
            progressed = closes[-1] > self.pending_reversal.crossover_close
            support_ok = up_moves >= support_needed
        else:
            relationship_ok = ema5[-1] < ema9[-1]
            ema_slope_ok = ema5[-1] < ema5[0]
            price_side_ok = closes[-1] < ema5[-1]
            progressed = closes[-1] < self.pending_reversal.crossover_close
            support_ok = down_moves >= support_needed

        passed = (
            relationship_ok
            and ema_slope_ok
            and price_side_ok
            and progressed
            and support_ok
        )

        self.log(
            f"REVERSAL {direction} CHECK: "
            f"relationship={relationship_ok} | "
            f"EMA5_slope={ema_slope_ok} | "
            f"price_side={price_side_ok} | "
            f"progressed={progressed} | "
            f"up_moves={up_moves}/{transitions} | "
            f"down_moves={down_moves}/{transitions} | "
            f"support_needed={support_needed} | PASSED={passed}"
        )
        return passed

    def _evaluate_pending_reversal(
        self, df: pd.DataFrame, latest_close: pd.Timestamp
    ) -> None:
        if not self.pending_reversal:
            return

        detected = pd.Timestamp(self.pending_reversal.detected_at)
        if detected.tzinfo is None:
            detected = detected.tz_localize(IST)
        else:
            detected = detected.tz_convert(IST)

        elapsed = (latest_close - detected).total_seconds() / 60.0
        direction = self.pending_reversal.direction

        # If EMA regime has already flipped back, cancel the reversal watch.
        current_direction = self._current_ema_regime(df)
        if current_direction != direction:
            self.log(
                f"REVERSAL WATCH CANCELLED: expected {direction}, "
                f"current EMA regime={current_direction or 'NONE'}."
            )
            self.pending_reversal = None
            return

        if elapsed < REVERSAL_CONFIRM_MIN_MINUTES:
            self.log(
                f"REVERSAL {direction} waiting: "
                f"{elapsed:.0f}/{REVERSAL_CONFIRM_MIN_MINUTES} minutes completed."
            )
            return

        if elapsed > REVERSAL_CONFIRM_MAX_MINUTES:
            self.log(
                f"REVERSAL {direction} not confirmed within "
                f"{REVERSAL_CONFIRM_MAX_MINUTES} minutes; current position retained."
            )
            self.pending_reversal = None
            return

        if self._reversal_confirmed(df, latest_close):
            self.log(
                f"REVERSAL {direction} CONFIRMED after {elapsed:.0f} minute(s). "
                "Closing wrong-side option now."
            )
            reversal_exit_ok = self._exit_wrong_side_on_regime_change(
                direction,
                f"{direction} EMA5/EMA9 reversal confirmed after "
                f"{elapsed:.0f}m",
            )
            if reversal_exit_ok:
                self.reversal_entry_bypass_direction = direction
                self.log(
                    f"REVERSAL ENTRY BYPASS ARMED: one immediate {direction} reverse "
                    "entry may bypass only the consecutive-loss guard if all normal "
                    "entry-quality checks pass."
                )
            self.pending_reversal = None
            return

        if elapsed >= REVERSAL_CONFIRM_MAX_MINUTES:
            self.log(
                f"REVERSAL {direction} failed the "
                f"{REVERSAL_CONFIRM_MAX_MINUTES}-minute confirmation; "
                "current position retained."
            )
            self.pending_reversal = None
        else:
            self.log(
                f"REVERSAL {direction} not confirmed at "
                f"{elapsed:.0f} minute(s); re-checking at "
                f"{REVERSAL_CONFIRM_MAX_MINUTES} minutes."
            )

    def _execute_confirmed_signal(self, direction: str) -> bool:
        now = self.now_ist()
        reversal_bypass = self.reversal_entry_bypass_direction == direction
        if not self._daily_risk_allows_entry(
            allow_reversal_loss_bypass=reversal_bypass
        ):
            return False

        if now.time() >= ENTRY_CUTOFF:
            self.log("Confirmed signal ignored because entry cutoff 15:28 IST has been reached.")
            return False

        # Refresh broker state immediately before trading.
        self._reconcile_positions(verbose=False)
        self.log(
            f"ORDER CHECK: direction={direction} | "
            f"regime_lock={self.last_traded_regime or 'NONE'} | "
            f"managed_positions={len(self.positions)}"
        )
        if self.entry_block_reason:
            self.log(f"Confirmed signal NOT traded: {self.entry_block_reason}")
            return False

        desired = "CE" if direction == "BULLISH" else "PE"
        opposite = "PE" if direction == "BULLISH" else "CE"

        # Reconcile immediately before every entry. The controller adopts all
        # existing LONG NIFTY MIS options, including manual positions.
        self._reconcile_positions(verbose=False)

        # One-position-at-a-time controller.
        if any(pos.option_type == desired for pos in self.positions.values()):
            self.log(
                f"{desired} position already exists at broker; duplicate entry skipped."
            )
            return False

        # HARD ARBITRATION SAFETY:
        # Entry logic may NEVER liquidate an existing opposite-side position by
        # itself. A reverse entry is allowed only after the dedicated reversal
        # controller has already closed the wrong-side position and armed the
        # reversal bypass for this same direction.
        opposite_still_open = any(
            pos.option_type == opposite for pos in self.positions.values()
        )
        if opposite_still_open and not reversal_bypass:
            self.log(
                f"REVERSAL PRIORITY BLOCK: {opposite} position is still open. "
                f"{direction} entry engine will NOT close it. Waiting for dedicated "
                f"{REVERSAL_CONFIRM_MIN_MINUTES}-{REVERSAL_CONFIRM_MAX_MINUTES}m "
                "reversal confirmation."
            )
            return False

        # Normally the reversal controller has already flattened the opposite side.
        # Keep this verification call only as a cleanup safeguard for a broker/state
        # race after a CONFIRMED reversal. It is never reached for an unconfirmed one.
        if opposite_still_open and reversal_bypass:
            if not self._square_off_option_type(
                opposite,
                f"{direction} CONFIRMED reversal cleanup",
            ):
                self.log(
                    f"New {desired} entry aborted because confirmed-reversal cleanup "
                    f"could not verify {opposite} flat."
                )
                return False

        if any(pos.option_type == opposite for pos in self.positions.values()):
            self.log(f"New {desired} entry aborted: an opposite {opposite} position still exists.")
            return False

        self.log(
            "BULLISH CONTINUATION -> close PE and BUY ITM3 CE"
            if direction == "BULLISH"
            else "BEARISH CONTINUATION -> close CE and BUY ITM3 PE"
        )

        try:
            contract = self._resolve_itm3(direction)
        except Exception as exc:
            self.log(f"Could not resolve ITM3 contract: {exc}")
            return False

        if self._place_entry(contract):
            if reversal_bypass:
                self.log(
                    f"REVERSAL ENTRY BYPASS CONSUMED: {direction} reverse entry placed. "
                    "Normal daily risk guard is active again for subsequent entries."
                )
                self.reversal_entry_bypass_direction = None
            self.last_traded_regime = direction
            self._save_state()
            self.log(
                f"REGIME LOCK SET: {direction}. No repeated entry in this EMA regime "
                "until a fresh EMA5/EMA9 crossover establishes a new regime."
            )
            return True
        return False

    # -------------------------------------------------------------------------
    # Risk management
    # -------------------------------------------------------------------------

    def _quote_ltp(self, symbol: str, exchange: str) -> Optional[float]:
        try:
            response = self.client.quotes(symbol=symbol, exchange=exchange)
            if response.get("status") != "success":
                return None
            ltp = self._as_float(response.get("data", {}).get("ltp"))
            return ltp if ltp > 0 else None
        except Exception:
            return None

    def _loss_adverse_confirmation(self, pos: ManagedPosition, ltp: float, pnl_pct: float) -> bool:
        """
        Evaluate the -5% early-loss warning only once per completed NIFTY 1m candle.

        A confirmation requires BOTH:
        1) option premium is still declining versus the prior completed-candle sample; and
        2) NIFTY EMA5/EMA9 + price side are adverse to the held option.

        CE adverse = EMA5 < EMA9 and NIFTY close < EMA5.
        PE adverse = EMA5 > EMA9 and NIFTY close > EMA5.

        Legacy adverse-warning helper retained only for backward state compatibility.
        It is no longer called by the active position-management path.
        """
        if pnl_pct > -LOSS_WARNING_PERCENT:
            if pos.loss_warning_active or pos.loss_adverse_count:
                self.log(
                    f"LOSS WARNING RESET: {pos.symbol} recovered above "
                    f"-{LOSS_WARNING_PERCENT:.0f}% (PnL={pnl_pct:+.2f}%)."
                )
            pos.loss_warning_active = False
            pos.loss_adverse_count = 0
            pos.loss_last_candle = ""
            pos.loss_last_sample_ltp = 0.0
            return False

        snap = self.last_market_snapshot
        if not snap:
            return False

        close_time = snap.get("close_time")
        if close_time is None:
            return False
        candle_key = close_time.isoformat()

        # Count at most once per completed 1-minute candle.
        if pos.loss_last_candle == candle_key:
            return False

        close = float(snap["close"])
        ema5 = float(snap["ema5"])
        ema9 = float(snap["ema9"])

        if not pos.loss_warning_active:
            pos.loss_warning_active = True
            pos.loss_adverse_count = 0
            self.log(
                f"LOSS WARNING ACTIVATED: {pos.symbol} PnL={pnl_pct:+.2f}% <= "
                f"-{LOSS_WARNING_PERCENT:.0f}%. Need {LOSS_ADVERSE_CONFIRM_CANDLES} "
                "consecutive adverse completed 1m confirmations for early exit; "
                f"absolute hard stop remains -{STOPLOSS_PERCENT:.0f}%."
            )

        premium_falling = (
            pos.loss_last_sample_ltp > 0 and ltp < pos.loss_last_sample_ltp
        )

        if pos.option_type == "CE":
            adverse_regime = ema5 < ema9
            adverse_price_side = close < ema5
            adverse_label = "bearish"
        else:
            adverse_regime = ema5 > ema9
            adverse_price_side = close > ema5
            adverse_label = "bullish"

        # The first sample after crossing -5% establishes the reference premium.
        # It does not count as a falling-premium confirmation by itself.
        if pos.loss_last_sample_ltp <= 0:
            pos.loss_adverse_count = 0
            self.log(
                f"LOSS WARNING SAMPLE: {pos.symbol} candle={close_time.strftime('%H:%M:%S')} "
                f"LTP={ltp:.2f}, PnL={pnl_pct:+.2f}%; reference sample set. "
                f"NIFTY adverse_{adverse_label}_regime={adverse_regime}, "
                f"price_side={adverse_price_side}."
            )
        else:
            adverse = premium_falling and adverse_regime and adverse_price_side
            if adverse:
                pos.loss_adverse_count += 1
            else:
                pos.loss_adverse_count = 0

            self.log(
                f"LOSS WARNING CHECK: {pos.symbol} candle={close_time.strftime('%H:%M:%S')} "
                f"PnL={pnl_pct:+.2f}% | premium_falling={premium_falling} "
                f"({pos.loss_last_sample_ltp:.2f}->{ltp:.2f}) | "
                f"adverse_{adverse_label}_regime={adverse_regime} | "
                f"price_side={adverse_price_side} | "
                f"confirmations={pos.loss_adverse_count}/{LOSS_ADVERSE_CONFIRM_CANDLES}."
            )

        pos.loss_last_candle = candle_key
        pos.loss_last_sample_ltp = ltp
        self._save_state()

        return pos.loss_adverse_count >= LOSS_ADVERSE_CONFIRM_CANDLES

    def _monitor_positions(self) -> None:
        for symbol in list(self.positions.keys()):
            pos = self.positions.get(symbol)
            if not pos:
                continue

            ltp = self._quote_ltp(symbol, pos.exchange)
            if ltp is None:
                pos.quote_failures += 1
                self._save_state()
                self.log(
                    f"WARNING: quote unavailable for {symbol} "
                    f"({pos.quote_failures}/{MAX_QUOTE_FAILURES})."
                )
                if pos.quote_failures >= MAX_QUOTE_FAILURES:
                    self.log(
                        f"FAIL-SAFE: {symbol} market data is stale/unavailable; "
                        "attempting market exit."
                    )
                    self._place_exit(symbol, "MARKET DATA FAIL-SAFE")
                continue

            if pos.quote_failures:
                pos.quote_failures = 0

            pnl_pct = ((ltp - pos.entry_price) / pos.entry_price) * 100.0

            if pnl_pct > pos.peak_pnl_percent:
                pos.peak_pnl_percent = pnl_pct

            # Adaptive protection:
            #   peak < +5%        -> fixed -5% hard stop only
            #   +5% to < +10%     -> fixed -1% protection floor
            #   +10% to < +20%    -> 7pp giveback
            #   +20% to < +50%    -> 5pp giveback
            #   +50% to < +70%    -> 7pp giveback
            #   +70% and above    -> 10pp giveback
            #
            # CRITICAL: desired_floor may mathematically fall when a wider band
            # starts, but the real floor below uses max(old_floor, desired_floor).
            # Therefore already-locked profit is NEVER released.
            desired_floor = self._desired_trailing_floor(pos.peak_pnl_percent)

            if pos.peak_pnl_percent >= PROFIT_LOCK_START_PERCENT:
                if not pos.trailing_active:
                    pos.trailing_active = True
                    self.log(
                        f"PROFIT TRAIL ACTIVATED: {symbol} peak reached "
                        f"{pos.peak_pnl_percent:+.2f}%; true 5pp trail zone is active."
                    )

            if desired_floor > pos.trailing_floor_percent:
                previous_floor = pos.trailing_floor_percent
                pos.trailing_floor_percent = desired_floor
                giveback = (
                    self._adaptive_profit_giveback(pos.peak_pnl_percent)
                    if pos.peak_pnl_percent >= EARLY_PROFIT_TRAIL_START_PERCENT
                    else 0.0
                )
                if (
                    EARLY_PROTECTION_START_PERCENT
                    <= pos.peak_pnl_percent
                    < EARLY_PROFIT_TRAIL_START_PERCENT
                ):
                    self.log(
                        f"EARLY PROFIT PROTECTION: {symbol} "
                        f"peak={pos.peak_pnl_percent:+.2f}% | "
                        f"floor {previous_floor:+.2f}% -> "
                        f"{pos.trailing_floor_percent:+.2f}%."
                    )
                else:
                    label = "PROFIT FLOOR RATCHET" if pos.trailing_active else "TRAIL RATCHET"
                    self.log(
                        f"{label}: {symbol} peak={pos.peak_pnl_percent:+.2f}% | "
                        f"floor {previous_floor:+.2f}% -> {pos.trailing_floor_percent:+.2f}% | "
                        f"active_giveback={giveback:.0f}pp."
                    )

            self._save_state()

            active_giveback = (
                self._adaptive_profit_giveback(pos.peak_pnl_percent)
                if pos.peak_pnl_percent >= EARLY_PROFIT_TRAIL_START_PERCENT
                else 0.0
            )
            trail_note = (
                f", peak={pos.peak_pnl_percent:+.2f}%, "
                f"trail_floor={pos.trailing_floor_percent:+.2f}%, "
                f"giveback={active_giveback:.0f}pp, "
                f"early_protection=+{EARLY_PROTECTION_START_PERCENT:.0f}%->"
                f"{EARLY_PROTECTION_FLOOR_PERCENT:.0f}%, "
                f"5pp_zone_starts=+{PROFIT_LOCK_START_PERCENT:.0f}%"
            )

            self.log(
                f"POSITION {symbol}: LTP={ltp:.2f}, entry={pos.entry_price:.2f}, "
                f"PnL={pnl_pct:+.2f}%, SL={pos.stop_price:.2f}{trail_note}."
            )

            # Hard loss protection always has priority.
            if ltp <= pos.stop_price:
                self._place_exit(
                    symbol,
                    f"{STOPLOSS_PERCENT:.0f}% HARD STOP-LOSS (LTP {ltp:.2f})",
                )
                continue

            # Adaptive protection:
            # -5% hard stop below +5% peak; -1% floor from +5% to <+10%;
            # true adaptive trailing above +10%.
            # The fixed hard stop above remains the absolute maximum-loss backstop.
            if pnl_pct <= pos.trailing_floor_percent:
                trail_kind = "PROFIT TRAIL EXIT" if pos.trailing_active else "EARLY/ADAPTIVE PROTECTION EXIT"
                self._place_exit(
                    symbol,
                    f"{trail_kind}: peak={pos.peak_pnl_percent:.2f}%, "
                    f"current={pnl_pct:.2f}%, "
                    f"floor={pos.trailing_floor_percent:.2f}%",
                )
                continue

    def _force_close_all(self) -> None:
        if not self.force_close_started:
            self.force_close_started = True
            self.pending_signal = None
            self.pending_reversal = None
            self.log("15:29 IST FORCE-CLOSE initiated. Cancelling strategy pending orders and exiting positions.")
            try:
                response = self.client.cancelallorder(strategy=STRATEGY_NAME)
                self.log(f"cancelallorder(): {response}")
            except Exception as exc:
                self.log(f"WARNING: cancelallorder() failed: {exc}")

        # Keep retrying unverified exits until market close.
        for symbol in list(self.positions.keys()):
            self._place_exit(symbol, "15:29 IST FORCE CLOSE")

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def _print_configuration(self) -> None:
        self.log("=" * 72)
        if REQUESTED_TRADING_CAPITAL_RUPEES != TRADING_CAPITAL_RUPEES:
            self.log(
                f"CAPITAL BAND CLAMP: requested=Rs.{REQUESTED_TRADING_CAPITAL_RUPEES:,.0f} -> "
                f"effective=Rs.{TRADING_CAPITAL_RUPEES:,.0f}; allowed range is "
                f"Rs.{MIN_TRADING_CAPITAL_RUPEES:,.0f}-Rs.{MAX_TRADING_CAPITAL_RUPEES:,.0f}."
            )
        else:
            self.log(
                f"CAPITAL BAND: effective=Rs.{TRADING_CAPITAL_RUPEES:,.0f}; "
                f"allowed range Rs.{MIN_TRADING_CAPITAL_RUPEES:,.0f}-"
                f"Rs.{MAX_TRADING_CAPITAL_RUPEES:,.0f}."
            )
        self.log("NIFTY50 EMA5/EMA9 ITM3 OPTIONS STRATEGY")
        self.log(
            f"Signal=NIFTY spot closed 1m candles | EMA{EMA_FAST_PERIOD}/EMA{EMA_SLOW_PERIOD} | "
            f"fresh-cross confirmation/retry={CONFIRMATION_MIN_MINUTES}-{CONFIRMATION_MAX_MINUTES}m"
        )
        self.log(
            f"Established-regime entry=current EMA relationship + continuous last "
            f"{REGIME_CONTINUATION_MINUTES}m direction; one entry per EMA regime"
        )
        self.log(
            f"Contract=nearest expiry {OPTION_OFFSET} CE/PE | "
            f"capital_band=Rs.{MIN_TRADING_CAPITAL_RUPEES:,.0f}-"
            f"Rs.{MAX_TRADING_CAPITAL_RUPEES:,.0f} | "
            f"effective_capital=Rs.{TRADING_CAPITAL_RUPEES:,.0f} | "
            f"dynamic lots by {RISK_PER_TRADE_PERCENT:.2f}% risk/trade and "
            f"{MAX_CAPITAL_DEPLOYMENT_PERCENT:.0f}% max capital deployment | "
            f"hard SL={STOPLOSS_PERCENT}% | continuous loss/trail giveback={LOSS_TRAIL_GIVEBACK_PERCENT}pp "
            f"with {LOSS_ADVERSE_CONFIRM_CANDLES} adverse 1m confirmations | "
            f"broker SL-M configured={BROKER_PROTECTIVE_STOP} "
            f"(Analyzer skips broker SL-M; LIVE attempts it) | profit trail activates="
            f"+{PROFIT_LOCK_START_PERCENT}% | adaptive protection: "
            f"<5=hard-stop only, "
            f"5-<10={EARLY_PROTECTION_FLOOR_PERCENT:.0f}% floor, "
            f"10-<20={TRAIL_10_TO_20_GIVEBACK:.0f}pp, "
            f"20-<50={TRAIL_20_TO_50_GIVEBACK:.0f}pp, "
            f"50-<70={TRAIL_50_TO_70_GIVEBACK:.0f}pp, "
            f"70+={TRAIL_70_PLUS_GIVEBACK:.0f}pp | "
            f"floor never moves backward"
        )
        self.log(
            f"Entry filters: 3/4 directional support + recent breakout + "
            f"noise-adjusted progress/gap + no-chase EMA5 distance ({MAX_EMA_DISTANCE_NOISE_MULT:.1f}x noise)"
        )
        self.log(
            f"Market-open bootstrap: enabled | target_rows={BOOTSTRAP_HISTORY_TARGET_ROWS} | "
            f"max_calendar_days_back={BOOTSTRAP_HISTORY_MAX_CALENDAR_DAYS}"
        )
        self.log(
            f"Daily guard: max_entries_live={MAX_TRADES_PER_DAY} | "
            f"analyzer_ignores_max_entries={ANALYZER_IGNORE_MAX_TRADES_BLOCK} | "
            f"max_consecutive_losses_live={MAX_CONSECUTIVE_LOSSES} | "
            f"analyzer_ignores_consecutive_loss_block={ANALYZER_IGNORE_CONSECUTIVE_LOSS_BLOCK} | "
            f"rupee_loss_cap={MAX_DAILY_REALIZED_LOSS_RUPEES or 'disabled'}"
        )
        self.log("New-entry cutoff=15:28:00 IST | force-close=15:29:00 IST")
        self.log(
            f"Single controller | manage all existing NIFTY MIS options={MANAGE_ALL_NIFTY_MIS_POSITIONS} | "
            f"Explicit live permission={ALLOW_LIVE_TRADING}"
        )
        self.log(
            f"Opposite crossover exit confirmation="
            f"{REVERSAL_CONFIRM_MIN_MINUTES}-{REVERSAL_CONFIRM_MAX_MINUTES}m; "
            f"new-side entry confirmation/retry="
            f"{CONFIRMATION_MIN_MINUTES}-{CONFIRMATION_MAX_MINUTES}m"
        )
        self.log("=" * 72)

    def run(self) -> None:
        self._print_configuration()

        if not self._is_nfo_session_today():
            self.log("NFO is not scheduled as an open session today. Strategy will not trade.")
            return

        try:
            while self.running:
                now = self.now_ist()
                current = now.time()

                if current < MARKET_OPEN:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # At 15:29, exit first; do not process any further signals.
                if current >= FORCE_CLOSE:
                    self._force_close_all()
                    if current >= MARKET_CLOSE:
                        if self.positions:
                            self.log(
                                "CRITICAL: Market close reached with locally tracked position(s) still present: "
                                + ", ".join(self.positions.keys())
                            )
                        else:
                            self.log("Market close reached; all strategy-managed positions are flat.")
                        break
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # Risk management runs every second while market is open.
                if self.positions:
                    self._monitor_positions()

                # Periodic broker reconciliation protects against external/manual changes.
                if time.monotonic() - self.last_reconcile_monotonic >= RECONCILE_INTERVAL_SECONDS:
                    self._reconcile_positions(verbose=False)
                    self.last_reconcile_monotonic = time.monotonic()

                # At/after 15:28 no fresh entries; discard pending confirmations.
                if current >= ENTRY_CUTOFF:
                    if self.pending_signal:
                        self.log("15:28 IST entry cutoff reached; pending signal cancelled.")
                        self.pending_signal = None
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # Fetch every second, but process only when a NEW completed 1m candle appears.
                df = self._fetch_closed_candles(now)
                if df is not None:
                    self._process_candle_signal(df, now)

                # Visibility only: proves the one-second loop and live market-data path are healthy.
                self._log_heartbeat_if_due(now)

                time.sleep(POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            self.log("Keyboard interrupt received. Bot stopped; positions are NOT auto-closed by Ctrl+C.")
        finally:
            self.running = False
            self._save_state()
            self._release_single_instance_lock()


# =============================================================================
# MAIN
# =============================================================================
    def __del__(self):
        try:
            self._release_single_instance_lock()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        bot = NiftyEMAOptionsStrategy()
        bot.run()
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
