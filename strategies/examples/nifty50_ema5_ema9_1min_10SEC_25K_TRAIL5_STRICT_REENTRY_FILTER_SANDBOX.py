#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NIFTY50 EMA5/EMA9 - 1 Minute - ITM3
Existing-regime immediate entry + fresh-cross 10-second confirmation + persistent 1-minute retry | /history authoritative + local failover EXIT-ONLY
OpenAlgo Analyzer/Sandbox only

Rules
-----
1. Signal source: NIFTY spot index (NSE_INDEX), completed 1-minute candles.
2. EMA5 / EMA9 only.
3. Existing regime / no position:
      EMA5 > EMA9 -> BUY nearest-expiry ITM3 CE immediately.
      EMA5 < EMA9 -> BUY nearest-expiry ITM3 PE immediately.
   Do not wait for a fresh crossover when the script starts or when flat.
4. Fresh crossover while running:
      EMA5 crosses above EMA9 -> start a 10-second confirmation.
      If EMA5 remains above EMA9 and NIFTY spot is still moving upward after
      10 seconds, square off all PE and BUY ITM3 CE immediately.
      EMA5 crosses below EMA9 -> start a 10-second confirmation.
      If EMA5 remains below EMA9 and NIFTY spot is still moving downward after
      10 seconds, square off all CE and BUY ITM3 PE immediately.
5. If the crossover fails before confirmation, cancel it and follow the current
   established EMA regime when flat.
6. Nearest current/future NIFTY expiry is selected dynamically.
7. Option contract: nearest-expiry ITM3 from spot via optionsymbol(offset="ITM3").
8. Use Rs.25,000 as sizing capital and buy the maximum whole number of
   exchange lots affordable at the option LTP.
9. Risk management: hard loss exit at -5% from entry. Profit trailing activates at +5%; after activation the exit threshold is the higher of (a) 5% below the highest option LTP or (b) +5% above entry. After any risk exit, same-direction re-entry is locked. The EMA must first move to the opposite regime, then produce a new crossover back into the stopped direction, and that crossover must pass the strict RSI/momentum/EMA-separation/slope/no-chase filter.
10. No new entry at/after 15:28 IST.
11. Square off all strategy-managed positions at/after 15:29 IST.
12. Crossover detection compares the stored EMA relationship from the last
    processed completed candle against the newly completed candle.
13. /history is authoritative during normal operation. Local sampled data is
    used only when /history fails, times out, returns insufficient data, or
    omits the newly completed candle.
14. During LOCAL FAILOVER, a locally detected opposite EMA crossover may be
    confirmed by the same 10-second live NIFTY continuation check, but the
    failover path is EXIT-ONLY: square off all wrong-side NIFTY MIS options
    and DO NOT place a new CE/PE entry until /history recovers.
15. When /history recovers, authority immediately returns to /history and
    normal entry/switching logic resumes.
16. If a 10-second continuation check fails, the bot retries the same
    direction on each newly completed 1-minute candle while that EMA
    regime remains valid, instead of waiting for another fresh cross.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from openalgo import api


# =============================================================================
# CONFIG
# =============================================================================

IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("OPENALGO_API_KEY", "").strip()
API_HOST = os.getenv("HOST_SERVER", "http://127.0.0.1:5000").strip()

STRATEGY_NAME = "NIFTY50_EMA5_EMA9_1MIN_10SEC_25K_TRAIL5_STRICT_REENTRY_FILTER_SANDBOX"

INDEX_SYMBOL = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPTIONS_EXCHANGE = "NFO"
OPTION_PRODUCT = "MIS"
OPTION_OFFSET = "ITM3"

EMA_FAST = 5
EMA_SLOW = 9
CANDLE_TIMEFRAME = "1m"
CANDLE_MINUTES = 1
LOOKBACK_DAYS = 5

TRADING_CAPITAL_RUPEES = 25000.0

ENTRY_CUTOFF = dt_time(15, 28)
FORCE_CLOSE = dt_time(15, 29)
MARKET_CLOSE = dt_time(15, 30)

POLL_SECONDS = 1.0
FRESH_CROSS_CONFIRM_SECONDS = 10.0

HARD_LOSS_PCT = 0.05
PROFIT_TRAIL_ACTIVATE_PCT = 0.05
PROFIT_TRAIL_DRAWDOWN_PCT = 0.05

# Strict filter used ONLY for same-direction re-entry after a risk exit.
STRICT_RSI_PERIOD = 14
STRICT_BULL_RSI_MIN = 55.0
STRICT_BULL_RSI_MAX = 72.0
STRICT_BEAR_RSI_MIN = 28.0
STRICT_BEAR_RSI_MAX = 45.0
STRICT_MOMENTUM_BARS = 3
STRICT_MIN_MOMENTUM_PCT = 0.00025       # 0.025%
STRICT_MIN_EMA_GAP_PCT = 0.00008        # 0.008%
STRICT_EMA_SLOPE_BARS = 2
STRICT_MAX_CHASE_PCT = 0.00150          # 0.15%


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Position:
    direction: str          # BULLISH / BEARISH
    option_type: str        # CE / PE
    symbol: str
    exchange: str
    quantity: int
    entry_price: float
    expiry: str
    peak_price: float = 0.0
    profit_trail_active: bool = False


@dataclass
class PendingCross:
    direction: str
    detected_candle: pd.Timestamp
    detected_ema5: float
    detected_ema9: float
    detected_spot: float
    armed_monotonic: float



# =============================================================================
# STRATEGY
# =============================================================================

class NiftyImmediateEMA:
    def __init__(self) -> None:
        if not API_KEY:
            raise RuntimeError(
                "OPENALGO_API_KEY is not set for this OpenAlgo instance."
            )

        self.client = api(api_key=API_KEY, host=API_HOST)
        self.position: Optional[Position] = None
        self.last_processed_candle: Optional[pd.Timestamp] = None
        self.expiry_cache_date: Optional[date] = None
        self.expiry_cache_value: Optional[str] = None
        self.pending_cross: Optional[PendingCross] = None

        # Resilient local completed-candle/EMA state.
        # Seeded from /history once, then advanced candle-by-candle locally.
        self.local_df: Optional[pd.DataFrame] = None
        self.local_ema5: Optional[float] = None
        self.local_ema9: Optional[float] = None
        self.last_processed_relation: Optional[str] = None
        self.local_minute_start: Optional[pd.Timestamp] = None
        self.local_minute_last_spot: Optional[float] = None
        self.history_fallback_count: int = 0
        self.local_failover_active: bool = False
        self.failover_entry_blocked: bool = False
        self.retry_cross_direction: Optional[str] = None
        self.risk_exit_lock_direction: Optional[str] = None
        self.risk_exit_lock_reason: Optional[str] = None
        self.risk_reentry_armed: bool = False

        self._verify_analyzer()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def now_ist() -> datetime:
        return datetime.now(IST)

    @staticmethod
    def log(message: str) -> None:
        stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp} IST] {message}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(
                line.encode("ascii", errors="replace").decode("ascii"),
                flush=True,
            )

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

    def _verify_analyzer(self) -> None:
        response = self.client.analyzerstatus()
        if response.get("status") != "success":
            raise RuntimeError(f"analyzerstatus() failed: {response}")

        data = response.get("data", {})
        if not bool(data.get("analyze_mode")):
            raise RuntimeError(
                "OpenAlgo Analyzer mode is OFF. This sandbox script will not run."
            )

        self.log("SAFETY VERIFIED: OpenAlgo Analyzer Mode is ON.")

    # -------------------------------------------------------------------------
    # History / EMA
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_history(data: Any) -> Optional[pd.DataFrame]:
        if data is None:
            return None

        if isinstance(data, pd.DataFrame):
            return data.copy()

        if isinstance(data, dict):
            for key in ("data", "candles", "history", "result", "results"):
                if key in data and data[key] is not None:
                    nested = NiftyImmediateEMA._normalize_history(data[key])
                    if nested is not None:
                        return nested

            try:
                df = pd.DataFrame(data)
                if not df.empty:
                    return df
            except Exception:
                pass

            try:
                df = pd.DataFrame([data])
                if not df.empty:
                    return df
            except Exception:
                return None

        if isinstance(data, (list, tuple)):
            try:
                df = pd.DataFrame(data)
                if not df.empty:
                    return df
            except Exception:
                return None

        return None

    def _fetch_closed_history(self) -> pd.DataFrame:
        now = self.now_ist()
        start_date = now.date() - timedelta(days=LOOKBACK_DAYS)
        end_date = now.date()

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

        df = self._normalize_history(data)
        if df is None or len(df) < EMA_SLOW + 2:
            raise RuntimeError("Insufficient NIFTY 1-minute history.")

        if not isinstance(df.index, pd.DatetimeIndex):
            if "timestamp" not in df.columns:
                raise RuntimeError("History response has no timestamp.")
            df.index = pd.to_datetime(df["timestamp"], errors="coerce")

        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            idx = idx.tz_localize(IST)
        else:
            idx = idx.tz_convert(IST)

        df.index = idx
        df = df[~df.index.isna()]
        df = df[~df.index.duplicated(keep="last")].sort_index()

        now_ts = pd.Timestamp(now)
        closed_mask = (
            df.index + pd.Timedelta(minutes=CANDLE_MINUTES)
        ) <= now_ts
        df = df.loc[closed_mask].copy()

        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close"])

        if len(df) < EMA_SLOW + 2:
            raise RuntimeError("Insufficient completed NIFTY 1-minute candles.")

        df["ema5"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        df["ema9"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

        return df

    @staticmethod
    def _current_regime(df: pd.DataFrame) -> Optional[str]:
        if df is None or len(df) < 1:
            return None

        row = df.iloc[-1]
        ema5 = float(row["ema5"])
        ema9 = float(row["ema9"])

        if ema5 > ema9:
            return "BULLISH"
        if ema5 < ema9:
            return "BEARISH"
        return None

    @staticmethod
    def _relation_from_values(ema5: float, ema9: float) -> Optional[str]:
        if ema5 > ema9:
            return "BULLISH"
        if ema5 < ema9:
            return "BEARISH"
        return None

    def _fresh_cross_from_stored_relation(
        self, ema5: float, ema9: float
    ) -> Optional[str]:
        """
        Compare the stored relationship from the LAST PROCESSED completed
        candle against the newly completed candle.

        This avoids depending on df.iloc[-2] from a new /history response.
        """
        current = self._relation_from_values(ema5, ema9)
        previous = self.last_processed_relation

        if previous == "BEARISH" and current == "BULLISH":
            return "BULLISH"
        if previous == "BULLISH" and current == "BEARISH":
            return "BEARISH"
        return None


    # -------------------------------------------------------------------------
    # Local 1-minute candle / EMA continuity
    # -------------------------------------------------------------------------

    def _seed_local_ema_state(self, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            raise RuntimeError("Cannot seed local EMA state from empty history.")

        self.local_df = df.tail(120).copy()
        row = self.local_df.iloc[-1]
        self.local_ema5 = float(row["ema5"])
        self.local_ema9 = float(row["ema9"])
        self.last_processed_candle = self.local_df.index[-1]
        self.last_processed_relation = self._relation_from_values(
            self.local_ema5, self.local_ema9
        )

        self.log(
            f"LOCAL EMA SEEDED: candle={self.last_processed_candle.strftime('%H:%M')} "
            f"EMA5={self.local_ema5:.2f} EMA9={self.local_ema9:.2f} "
            f"relation={self.last_processed_relation or 'NEUTRAL'}"
        )

    def _preferred_completed_close(
        self, completed_minute: pd.Timestamp, local_close: float
    ) -> Tuple[float, str]:
        """
        Prefer /history for the just-completed candle. If /history fails, is late,
        or omits the candle, use the locally sampled last NIFTY spot of that minute.
        """
        try:
            hdf = self._fetch_closed_history()
            if completed_minute in hdf.index:
                close = float(hdf.loc[completed_minute]["close"])
                self.history_fallback_count = 0
                return close, "history"

            self.history_fallback_count += 1
            self.log(
                f"HISTORY LATE/MISSING: {completed_minute.strftime('%H:%M')} "
                f"not returned; using local close={local_close:.2f}."
            )
            return local_close, "local"
        except Exception as exc:
            self.history_fallback_count += 1
            self.log(
                f"HISTORY FALLBACK #{self.history_fallback_count}: {exc} | "
                f"using local close for {completed_minute.strftime('%H:%M')}="
                f"{local_close:.2f}."
            )
            return local_close, "local"

    def _advance_local_ema(
        self, completed_minute: pd.Timestamp, close: float, source: str
    ) -> pd.DataFrame:
        if self.local_ema5 is None or self.local_ema9 is None:
            raise RuntimeError("Local EMA state is not seeded.")

        a5 = 2.0 / (EMA_FAST + 1.0)
        a9 = 2.0 / (EMA_SLOW + 1.0)

        ema5 = a5 * close + (1.0 - a5) * self.local_ema5
        ema9 = a9 * close + (1.0 - a9) * self.local_ema9

        row = pd.DataFrame(
            {"close": [close], "ema5": [ema5], "ema9": [ema9]},
            index=pd.DatetimeIndex([completed_minute]),
        )

        if self.local_df is None or self.local_df.empty:
            self.local_df = row
        else:
            self.local_df = self.local_df[
                self.local_df.index != completed_minute
            ]
            self.local_df = pd.concat(
                [self.local_df, row]
            ).sort_index().tail(120)

        self.local_ema5 = ema5
        self.local_ema9 = ema9

        self.log(
            f"LOCAL EMA UPDATE {completed_minute.strftime('%H:%M')} "
            f"source={source} close={close:.2f} "
            f"EMA5={ema5:.2f} EMA9={ema9:.2f}"
        )
        return self.local_df

    def _poll_local_completed_candle(self) -> Optional[pd.DataFrame]:
        """
        /history is PRIMARY and AUTHORITATIVE.

        Local NIFTY sampling runs only as a standby backup. At each completed
        minute the script first requests /history. Local data becomes active
        only if /history errors, times out, returns insufficient data, or omits
        the required completed candle. As soon as /history supplies the next
        required candle, authority returns to /history automatically.
        """
        spot = self._quote_ltp(INDEX_SYMBOL, INDEX_EXCHANGE)
        now = pd.Timestamp(self.now_ist())
        minute_start = now.floor("min")

        if self.local_minute_start is None:
            self.local_minute_start = minute_start
            self.local_minute_last_spot = spot
            return None

        if minute_start == self.local_minute_start:
            # Standby sample only. It does not drive EMA while /history is healthy.
            self.local_minute_last_spot = spot
            return None

        completed_minute = self.local_minute_start
        local_close = float(
            self.local_minute_last_spot
            if self.local_minute_last_spot is not None
            else spot
        )

        # Begin standby sampling for the new minute immediately.
        self.local_minute_start = minute_start
        self.local_minute_last_spot = spot

        if (
            self.last_processed_candle is not None
            and completed_minute <= self.last_processed_candle
        ):
            return None

        # AUTHORITATIVE PATH: /history first.
        try:
            hdf = self._fetch_closed_history()
            if completed_minute not in hdf.index:
                raise RuntimeError(
                    f"/history omitted completed candle "
                    f"{completed_minute.strftime('%H:%M')}"
                )

            row = hdf.loc[completed_minute]
            close = float(row["close"])
            ema5 = float(row["ema5"])
            ema9 = float(row["ema9"])

            if self.local_failover_active:
                self.log(
                    f"HISTORY RECOVERED: {completed_minute.strftime('%H:%M')} "
                    "available. Returning authority to /history."
                )

            self.local_failover_active = False
            self.failover_entry_blocked = False
            self.history_fallback_count = 0

            row_df = pd.DataFrame(
                {"close": [close], "ema5": [ema5], "ema9": [ema9]},
                index=pd.DatetimeIndex([completed_minute]),
            )

            if self.local_df is None or self.local_df.empty:
                self.local_df = row_df
            else:
                self.local_df = self.local_df[
                    self.local_df.index != completed_minute
                ]
                self.local_df = pd.concat(
                    [self.local_df, row_df]
                ).sort_index().tail(120)

            self.local_ema5 = ema5
            self.local_ema9 = ema9

            self.log(
                f"EMA UPDATE {completed_minute.strftime('%H:%M')} "
                f"source=history-authoritative close={close:.2f} "
                f"EMA5={ema5:.2f} EMA9={ema9:.2f}"
            )
            return self.local_df

        except Exception as exc:
            # FAILOVER PATH: local data is used only here.
            self.history_fallback_count += 1
            self.local_failover_active = True
            self.failover_entry_blocked = True

            if self.local_ema5 is None or self.local_ema9 is None:
                raise RuntimeError(
                    "History failed and local EMA state is not seeded."
                ) from exc

            a5 = 2.0 / (EMA_FAST + 1.0)
            a9 = 2.0 / (EMA_SLOW + 1.0)

            ema5 = a5 * local_close + (1.0 - a5) * self.local_ema5
            ema9 = a9 * local_close + (1.0 - a9) * self.local_ema9

            row_df = pd.DataFrame(
                {"close": [local_close], "ema5": [ema5], "ema9": [ema9]},
                index=pd.DatetimeIndex([completed_minute]),
            )

            if self.local_df is None or self.local_df.empty:
                self.local_df = row_df
            else:
                self.local_df = self.local_df[
                    self.local_df.index != completed_minute
                ]
                self.local_df = pd.concat(
                    [self.local_df, row_df]
                ).sort_index().tail(120)

            self.local_ema5 = ema5
            self.local_ema9 = ema9

            self.log(
                f"HISTORY FAILOVER #{self.history_fallback_count}: "
                f"{type(exc).__name__}: {exc} | "
                f"LOCAL BACKUP ACTIVE for {completed_minute.strftime('%H:%M')} "
                f"close={local_close:.2f} EMA5={ema5:.2f} EMA9={ema9:.2f}"
            )
            return self.local_df


    # -------------------------------------------------------------------------
    # Expiry / option symbol
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_expiry(raw: str) -> Optional[date]:
        raw = str(raw).strip().upper()

        for fmt in ("%d-%b-%y", "%d%b%y", "%d-%b-%Y", "%d%b%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                pass

        return None

    def _nearest_expiry(self, force_refresh: bool = False) -> str:
        today = self.now_ist().date()

        if (
            not force_refresh
            and self.expiry_cache_date == today
            and self.expiry_cache_value
        ):
            parsed = self._parse_expiry(self.expiry_cache_value)
            if parsed and parsed >= today:
                return self.expiry_cache_value

        response = self.client.expiry(
            symbol=INDEX_SYMBOL,
            exchange=OPTIONS_EXCHANGE,
            instrumenttype="options",
        )

        if response.get("status") != "success":
            raise RuntimeError(f"expiry() failed: {response}")

        candidates: List[Tuple[date, str]] = []

        for raw in response.get("data", []):
            parsed = self._parse_expiry(raw)
            if parsed is not None and parsed >= today:
                candidates.append((parsed, str(raw)))

        if not candidates:
            raise RuntimeError("No current/future NIFTY option expiry available.")

        candidates.sort(key=lambda x: x[0])
        expiry_date = candidates[0][0]
        expiry_for_api = expiry_date.strftime("%d%b%y").upper()

        self.expiry_cache_date = today
        self.expiry_cache_value = expiry_for_api

        self.log(
            f"EXPIRY: nearest NIFTY expiry={expiry_for_api}"
        )

        return expiry_for_api

    def _quote_ltp(self, symbol: str, exchange: str) -> float:
        response = self.client.quotes(
            symbol=symbol,
            exchange=exchange,
        )

        if response.get("status") != "success":
            raise RuntimeError(f"quotes() failed for {symbol}: {response}")

        data = response.get("data", {})
        ltp = self._as_float(data.get("ltp"))

        if ltp <= 0:
            raise RuntimeError(f"Invalid LTP for {symbol}: {response}")

        return ltp

    def _resolve_itm3(self, direction: str) -> Dict[str, Any]:
        option_type = "CE" if direction == "BULLISH" else "PE"
        expiry = self._nearest_expiry()

        response = self.client.optionsymbol(
            underlying=INDEX_SYMBOL,
            exchange=INDEX_EXCHANGE,
            expiry_date=expiry,
            offset=OPTION_OFFSET,
            option_type=option_type,
        )

        if response.get("status") != "success":
            expiry = self._nearest_expiry(force_refresh=True)

            response = self.client.optionsymbol(
                underlying=INDEX_SYMBOL,
                exchange=INDEX_EXCHANGE,
                expiry_date=expiry,
                offset=OPTION_OFFSET,
                option_type=option_type,
            )

        if response.get("status") != "success":
            raise RuntimeError(f"optionsymbol() failed: {response}")

        symbol = str(response.get("symbol", "")).upper()
        exchange = str(
            response.get("exchange", OPTIONS_EXCHANGE)
        ).upper()
        lot_size = self._as_int(response.get("lotsize"))

        if not symbol or exchange != OPTIONS_EXCHANGE or lot_size <= 0:
            raise RuntimeError(f"Invalid optionsymbol() response: {response}")

        option_ltp = self._quote_ltp(symbol, exchange)

        cost_per_lot = option_ltp * lot_size
        lots = int(TRADING_CAPITAL_RUPEES // cost_per_lot)

        if lots < 1:
            raise RuntimeError(
                f"Rs.5L insufficient for one lot of {symbol}: "
                f"LTP={option_ltp:.2f}, lot={lot_size}"
            )

        quantity = lots * lot_size

        self.log(
            f"OPTION RESOLVED: {direction} {symbol} "
            f"expiry={expiry} ITM3 LTP={option_ltp:.2f} "
            f"lot={lot_size} lots={lots} qty={quantity}"
        )

        return {
            "symbol": symbol,
            "exchange": exchange,
            "option_type": option_type,
            "expiry": expiry,
            "lot_size": lot_size,
            "lots": lots,
            "quantity": quantity,
            "reference_ltp": option_ltp,
        }

    # -------------------------------------------------------------------------
    # EMA regime / crossover confirmation
    # -------------------------------------------------------------------------

    def _same_side_position_open(self, direction: str) -> bool:
        if self.position is None:
            return False
        desired = "CE" if direction == "BULLISH" else "PE"
        return self.position.option_type == desired

    def _start_pending_cross(
        self, direction: str, df: pd.DataFrame
    ) -> None:
        row = df.iloc[-1]
        spot = self._quote_ltp(INDEX_SYMBOL, INDEX_EXCHANGE)

        self.pending_cross = PendingCross(
            direction=direction,
            detected_candle=df.index[-1],
            detected_ema5=float(row["ema5"]),
            detected_ema9=float(row["ema9"]),
            detected_spot=float(spot),
            armed_monotonic=time.monotonic(),
        )

        self.log(
            f"{direction} FRESH EMA5/EMA9 CROSS detected on "
            f"{df.index[-1].strftime('%H:%M')} completed candle | "
            f"spot={spot:.2f}. Waiting {FRESH_CROSS_CONFIRM_SECONDS:.0f}s "
            "for continuation confirmation."
        )

    def _evaluate_pending_cross_5sec(self) -> None:
        p = self.pending_cross
        if p is None:
            return

        elapsed = time.monotonic() - p.armed_monotonic
        if elapsed < FRESH_CROSS_CONFIRM_SECONDS:
            return

        if self.local_ema5 is None or self.local_ema9 is None:
            return

        current_regime = self._relation_from_values(
            self.local_ema5, self.local_ema9
        )

        try:
            spot = self._quote_ltp(INDEX_SYMBOL, INDEX_EXCHANGE)
        except Exception as exc:
            self.log(
                f"{p.direction} 10SEC CONFIRM quote unavailable: {exc}. "
                "Keeping confirmation pending."
            )
            return

        if current_regime != p.direction:
            self.log(
                f"{p.direction} 10SEC CROSS CANCELLED: EMA relation changed "
                f"to {current_regime or 'NEUTRAL'}."
            )
            self.pending_cross = None
            return

        if p.direction == "BULLISH":
            continuation_ok = spot > p.detected_spot
        else:
            continuation_ok = spot < p.detected_spot

        self.log(
            f"{p.direction} 10SEC CONFIRM: "
            f"start_spot={p.detected_spot:.2f} current_spot={spot:.2f} | "
            f"EMA5={self.local_ema5:.2f} EMA9={self.local_ema9:.2f} | "
            f"relation_ok={current_regime == p.direction} | "
            f"continuation_ok={continuation_ok} | "
            f"history_failover={self.local_failover_active}"
        )

        if not continuation_ok:
            self.log(
                f"{p.direction} 10SEC CROSS NOT CONFIRMED: "
                "NIFTY did not continue in crossover direction. "
                "Will retry on the next completed 1-minute candle if the "
                "same EMA regime still remains valid."
            )
            self.retry_cross_direction = p.direction
            self.pending_cross = None
            return

        direction = p.direction
        self.pending_cross = None
        self.retry_cross_direction = None

        if self.local_failover_active or self.failover_entry_blocked:
            wrong_type = "PE" if direction == "BULLISH" else "CE"
            self.log(
                f"{direction} LOCAL FAILOVER CROSS CONFIRMED after "
                f"{FRESH_CROSS_CONFIRM_SECONDS:.0f}s. EXIT-ONLY MODE: "
                f"close ALL {wrong_type}; DO NOT open a new position until "
                "/history recovers."
            )
            try:
                self._square_off_broker_positions_by_type(
                    wrong_type,
                    f"LOCAL_FAILOVER_{direction}_10SEC_EXIT_ONLY",
                )
            except Exception as exc:
                self.log(f"LOCAL FAILOVER PROTECTIVE EXIT ERROR: {exc}")
            return

        self.log(
            f"{direction} CONTINUATION CONFIRMED after "
            f"{FRESH_CROSS_CONFIRM_SECONDS:.0f}s. Acting normally."
        )
        self._act_on_direction(
            direction, "FRESH_CROSS_10SEC_CONFIRMED"
        )

    def _follow_existing_regime_if_flat(self, df: pd.DataFrame) -> None:
        """
        If there is no managed position and no pending fresh crossover,
        immediately follow the current EMA5/EMA9 relationship.
        """
        if self.position is not None or self.pending_cross is not None:
            return

        if self.local_failover_active or self.failover_entry_blocked:
            self.log(
                "FAILOVER ENTRY BLOCK: remain flat until /history recovers."
            )
            return

        direction = self._current_regime(df)
        if direction is None:
            return

        if (
            self.risk_exit_lock_direction is not None
            and direction == self.risk_exit_lock_direction
        ):
            self.log(
                f"EXISTING REGIME {direction}: strict risk re-entry lock active "
                f"after {self.risk_exit_lock_reason}; waiting for a fresh "
                "EMA crossover plus strict filter."
            )
            return

        if self.now_ist().time() >= ENTRY_CUTOFF:
            return

        self.log(
            f"EXISTING EMA REGIME WHILE FLAT: {direction}. "
            "Entering immediately; no fresh crossover required."
        )
        self._act_on_direction(direction, "EXISTING_REGIME_IMMEDIATE")

    # -------------------------------------------------------------------------
    # Position / orders
    # -------------------------------------------------------------------------

    @staticmethod
    def _option_type_from_symbol(symbol: str) -> Optional[str]:
        s = str(symbol).upper().strip()
        if not s.startswith("NIFTY"):
            return None
        if s.endswith("CE"):
            return "CE"
        if s.endswith("PE"):
            return "PE"
        return None

    def _positionbook(self) -> List[Dict[str, Any]]:
        response = self.client.positionbook()
        if response.get("status") != "success":
            raise RuntimeError(f"positionbook() failed: {response}")
        data = response.get("data", [])
        return data if isinstance(data, list) else []

    def _broker_long_nifty_mis_positions(
        self, option_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for item in self._positionbook():
            symbol = str(item.get("symbol", "")).upper().strip()
            opt = self._option_type_from_symbol(symbol)
            if opt is None:
                continue
            if option_type and opt != option_type:
                continue
            if str(item.get("exchange", "")).upper() != OPTIONS_EXCHANGE:
                continue
            if str(item.get("product", "")).upper() != OPTION_PRODUCT:
                continue

            qty = self._as_int(item.get("quantity"))
            if qty > 0:
                out.append(item)
            elif qty < 0:
                self.log(
                    f"WARNING: short NIFTY MIS option position detected and NOT auto-sold: "
                    f"{symbol} qty={qty}. This strategy manages long-option positions only."
                )
        return out

    def _square_off_broker_positions_by_type(
        self, option_type: str, reason: str
    ) -> int:
        positions = self._broker_long_nifty_mis_positions(option_type)
        if not positions:
            self.log(
                f"BROKER-WIDE EXIT CHECK: no long NIFTY {option_type} MIS positions to close."
            )
            return 0

        closed_count = 0
        for item in positions:
            symbol = str(item.get("symbol", "")).upper().strip()
            exchange = str(item.get("exchange", OPTIONS_EXCHANGE)).upper()
            qty = self._as_int(item.get("quantity"))
            if qty <= 0:
                continue

            self.log(
                f"BROKER-WIDE EXIT: SELL {symbol} qty={qty} at MARKET | reason={reason}"
            )
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=symbol,
                exchange=exchange,
                action="SELL",
                quantity=qty,
                price_type="MARKET",
                product=OPTION_PRODUCT,
            )
            if response.get("status") != "success":
                raise RuntimeError(
                    f"Broker-wide SELL failed for {symbol}: {response}"
                )

            self.log(f"BROKER-WIDE EXIT ORDER SENT: {symbol} -> {response}")
            closed_count += 1

            # Clear the local position only if it is this exact symbol.
            if self.position is not None and self.position.symbol.upper() == symbol:
                self.position = None

        return closed_count

    def _square_off_all_broker_nifty_mis_longs(self, reason: str) -> int:
        positions = self._broker_long_nifty_mis_positions()
        if not positions:
            self.log("BROKER-WIDE FORCE EXIT: no long NIFTY MIS option positions open.")
            self.position = None
            return 0

        closed_count = 0
        for item in positions:
            symbol = str(item.get("symbol", "")).upper().strip()
            exchange = str(item.get("exchange", OPTIONS_EXCHANGE)).upper()
            qty = self._as_int(item.get("quantity"))
            if qty <= 0:
                continue

            self.log(
                f"BROKER-WIDE FORCE EXIT: SELL {symbol} qty={qty} at MARKET | reason={reason}"
            )
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=symbol,
                exchange=exchange,
                action="SELL",
                quantity=qty,
                price_type="MARKET",
                product=OPTION_PRODUCT,
            )
            if response.get("status") != "success":
                raise RuntimeError(
                    f"Force-square-off SELL failed for {symbol}: {response}"
                )
            self.log(f"BROKER-WIDE FORCE EXIT ORDER SENT: {symbol} -> {response}")
            closed_count += 1

        self.position = None
        return closed_count

    def _broker_has_same_side_long(self, direction: str) -> bool:
        desired_type = "CE" if direction == "BULLISH" else "PE"
        positions = self._broker_long_nifty_mis_positions(desired_type)
        if positions:
            details = ", ".join(
                f"{str(x.get('symbol','')).upper()} qty={self._as_int(x.get('quantity'))}"
                for x in positions
            )
            self.log(
                f"SAME-SIDE POSITION EXISTS: {details}. No duplicate {desired_type} entry."
            )
            return True
        return False

    def _place_entry(self, direction: str) -> None:
        contract = self._resolve_itm3(direction)

        self.log(
            f"ENTRY: BUY {contract['symbol']} qty={contract['quantity']} "
            f"({contract['lots']} lots) at MARKET"
        )

        response = self.client.placeorder(
            strategy=STRATEGY_NAME,
            symbol=contract["symbol"],
            exchange=contract["exchange"],
            action="BUY",
            quantity=contract["quantity"],
            price_type="MARKET",
            product=OPTION_PRODUCT,
        )

        if response.get("status") != "success":
            raise RuntimeError(f"BUY failed: {response}")

        # Analyzer may not always provide a fill instantly.
        time.sleep(0.5)

        try:
            entry_price = self._quote_ltp(
                contract["symbol"],
                contract["exchange"],
            )
        except Exception:
            entry_price = float(contract["reference_ltp"])

        self.position = Position(
            direction=direction,
            option_type=contract["option_type"],
            symbol=contract["symbol"],
            exchange=contract["exchange"],
            quantity=int(contract["quantity"]),
            entry_price=entry_price,
            expiry=contract["expiry"],
            peak_price=entry_price,
            profit_trail_active=False,
        )

        self.log(
            f"ENTRY OPENED: {self.position.symbol} "
            f"qty={self.position.quantity} ref_entry={self.position.entry_price:.2f}"
        )

    def _monitor_position_trailing(self) -> None:
        """
        5% loss protection + profit trailing.

        Loss:
            Exit when option LTP falls 5% below entry.

        Profit trail:
            Activate once option LTP reaches +5% from entry.
            Then track the highest LTP and exit after a 5% decline from that peak.
        """
        pos = self.position
        if pos is None:
            return

        try:
            ltp = self._quote_ltp(pos.symbol, pos.exchange)
        except Exception as exc:
            self.log(f"TRAIL MONITOR QUOTE ERROR {pos.symbol}: {exc}")
            return

        if pos.entry_price <= 0:
            return

        if pos.peak_price <= 0:
            pos.peak_price = pos.entry_price

        if ltp > pos.peak_price:
            pos.peak_price = ltp

        pnl_pct = ((ltp - pos.entry_price) / pos.entry_price) * 100.0

        hard_loss_price = pos.entry_price * (1.0 - HARD_LOSS_PCT)
        if ltp <= hard_loss_price:
            self.log(
                f"HARD LOSS EXIT: {pos.symbol} entry={pos.entry_price:.2f} "
                f"ltp={ltp:.2f} pnl={pnl_pct:.2f}%"
            )
            self._exit_position("HARD_LOSS_5PCT")
            return

        activate_price = pos.entry_price * (1.0 + PROFIT_TRAIL_ACTIVATE_PCT)
        if not pos.profit_trail_active and ltp >= activate_price:
            pos.profit_trail_active = True
            pos.peak_price = max(pos.peak_price, ltp)
            self.log(
                f"PROFIT TRAIL ACTIVATED: {pos.symbol} "
                f"entry={pos.entry_price:.2f} ltp={ltp:.2f} "
                f"profit={pnl_pct:.2f}% peak={pos.peak_price:.2f}"
            )

        if pos.profit_trail_active:
            raw_trail_exit_price = pos.peak_price * (
                1.0 - PROFIT_TRAIL_DRAWDOWN_PCT
            )
            profit_lock_price = pos.entry_price * (
                1.0 + PROFIT_TRAIL_ACTIVATE_PCT
            )
            trail_exit_price = max(
                raw_trail_exit_price,
                profit_lock_price,
            )

            self.log(
                f"PROFIT TRAIL: {pos.symbol} ltp={ltp:.2f} "
                f"peak={pos.peak_price:.2f} "
                f"raw_trail={raw_trail_exit_price:.2f} "
                f"profit_floor={profit_lock_price:.2f} "
                f"effective_exit={trail_exit_price:.2f} "
                f"pnl={pnl_pct:.2f}%"
            )

            if ltp <= trail_exit_price:
                self.log(
                    f"PROFIT TRAIL EXIT: {pos.symbol} "
                    f"entry={pos.entry_price:.2f} peak={pos.peak_price:.2f} "
                    f"ltp={ltp:.2f}"
                )
                self._exit_position("PROFIT_TRAIL_5PCT_FROM_PEAK")
                return

    def _exit_position(self, reason: str) -> None:
        if self.position is None:
            return

        pos = self.position

        self.log(
            f"EXIT: SELL {pos.symbol} qty={pos.quantity} "
            f"reason={reason}"
        )

        response = self.client.placeorder(
            strategy=STRATEGY_NAME,
            symbol=pos.symbol,
            exchange=pos.exchange,
            action="SELL",
            quantity=pos.quantity,
            price_type="MARKET",
            product=OPTION_PRODUCT,
        )

        if response.get("status") != "success":
            raise RuntimeError(f"SELL failed: {response}")

        self.log(f"EXIT ORDER SENT: {response}")

        if reason.startswith("HARD_LOSS_") or reason.startswith("PROFIT_TRAIL_"):
            self.risk_exit_lock_direction = pos.direction
            self.risk_exit_lock_reason = reason
            self.risk_reentry_armed = False
            self.log(
                f"RISK RE-ENTRY LOCK SET: direction={pos.direction} "
                f"reason={reason}. Same-side entry is blocked until the "
                "EMA regime first changes to the opposite side."
            )

        self.position = None

    def _strict_reentry_filter(self, direction: str) -> bool:
        """
        Strict confirmation used ONLY for same-direction re-entry after
        HARD_LOSS / PROFIT_TRAIL exits.

        Uses completed NIFTY 1-minute candles already maintained by the strategy:
          - EMA5/EMA9 relation
          - RSI(14)
          - 3-candle directional momentum
          - minimum EMA separation
          - EMA5 directional slope
          - no-chase distance from EMA5

        Fail-closed: if enough clean history is not available, re-entry is rejected.
        """
        df = self.local_df
        if df is None or len(df) < max(STRICT_RSI_PERIOD + 3, 20):
            self.log("STRICT REENTRY FILTER: insufficient completed-candle history.")
            return False

        work = df.copy()
        work["close"] = pd.to_numeric(work["close"], errors="coerce")
        work["ema5"] = pd.to_numeric(work["ema5"], errors="coerce")
        work["ema9"] = pd.to_numeric(work["ema9"], errors="coerce")
        work = work.dropna(subset=["close", "ema5", "ema9"])

        if len(work) < max(STRICT_RSI_PERIOD + 3, 20):
            self.log("STRICT REENTRY FILTER: insufficient clean indicator history.")
            return False

        close = work["close"]
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta.clip(upper=0.0))

        avg_gain = gain.ewm(
            alpha=1.0 / STRICT_RSI_PERIOD,
            adjust=False,
            min_periods=STRICT_RSI_PERIOD,
        ).mean()
        avg_loss = loss.ewm(
            alpha=1.0 / STRICT_RSI_PERIOD,
            adjust=False,
            min_periods=STRICT_RSI_PERIOD,
        ).mean()

        rs = avg_gain / avg_loss.replace(0.0, float("nan"))
        rsi = 100.0 - (100.0 / (1.0 + rs))

        # Handle all-up sequences where avg_loss is zero.
        if pd.isna(rsi.iloc[-1]) and avg_gain.iloc[-1] > 0 and avg_loss.iloc[-1] == 0:
            current_rsi = 100.0
        else:
            current_rsi = float(rsi.iloc[-1])

        last_close = float(close.iloc[-1])
        ema5 = float(work["ema5"].iloc[-1])
        ema9 = float(work["ema9"].iloc[-1])

        momentum_base = float(close.iloc[-1 - STRICT_MOMENTUM_BARS])
        momentum_pct = (
            (last_close - momentum_base) / momentum_base
            if momentum_base > 0
            else 0.0
        )

        ema_gap_pct = abs(ema5 - ema9) / last_close if last_close > 0 else 0.0

        ema5_old = float(work["ema5"].iloc[-1 - STRICT_EMA_SLOPE_BARS])
        ema5_slope = ema5 - ema5_old

        chase_pct = abs(last_close - ema5) / last_close if last_close > 0 else 999.0

        if direction == "BULLISH":
            checks = {
                "relation": ema5 > ema9,
                "rsi": STRICT_BULL_RSI_MIN <= current_rsi <= STRICT_BULL_RSI_MAX,
                "momentum": momentum_pct >= STRICT_MIN_MOMENTUM_PCT,
                "ema_gap": ema_gap_pct >= STRICT_MIN_EMA_GAP_PCT,
                "ema_slope": ema5_slope > 0,
                "no_chase": chase_pct <= STRICT_MAX_CHASE_PCT,
            }
        else:
            checks = {
                "relation": ema5 < ema9,
                "rsi": STRICT_BEAR_RSI_MIN <= current_rsi <= STRICT_BEAR_RSI_MAX,
                "momentum": momentum_pct <= -STRICT_MIN_MOMENTUM_PCT,
                "ema_gap": ema_gap_pct >= STRICT_MIN_EMA_GAP_PCT,
                "ema_slope": ema5_slope < 0,
                "no_chase": chase_pct <= STRICT_MAX_CHASE_PCT,
            }

        passed = all(checks.values())

        self.log(
            f"STRICT REENTRY {direction}: passed={passed} | "
            f"RSI14={current_rsi:.2f} | momentum3={momentum_pct*100:.3f}% | "
            f"EMA_gap={ema_gap_pct*100:.4f}% | EMA5_slope={ema5_slope:.2f} | "
            f"chase={chase_pct*100:.3f}% | "
            + " ".join(f"{k}={v}" for k, v in checks.items())
        )

        return passed

    def _act_on_direction(self, direction: str, source: str) -> None:
        now = self.now_ist()

        if (
            self.risk_exit_lock_direction is not None
            and direction == self.risk_exit_lock_direction
        ):
            # Same-side re-entry after a risk exit is deliberately stricter:
            # 1) EMA must first have moved to the opposite regime (arms re-entry).
            # 2) A NEW fresh crossover back to this direction is required.
            # 3) The strict RSI/momentum/EMA/no-chase filter must pass.
            is_fresh_cross_source = "FRESH_CROSS" in source

            if not self.risk_reentry_armed:
                self.log(
                    f"{source} {direction}: RISK RE-ENTRY LOCK active after "
                    f"{self.risk_exit_lock_reason}. EMA has not yet completed "
                    "an opposite-regime reset."
                )
                return

            if not is_fresh_cross_source:
                self.log(
                    f"{source} {direction}: risk re-entry requires a NEW confirmed "
                    "EMA crossover; existing-regime entry is blocked."
                )
                return

            if not self._strict_reentry_filter(direction):
                self.log(
                    f"{source} {direction}: NEW EMA crossover occurred, but "
                    "STRICT REENTRY FILTER failed. Remaining flat for this side."
                )
                return

            self.log(
                f"{source} {direction}: NEW EMA crossover + STRICT FILTER PASSED. "
                "Risk re-entry lock released."
            )
            self.risk_exit_lock_direction = None
            self.risk_exit_lock_reason = None
            self.risk_reentry_armed = False

        if self.local_failover_active or self.failover_entry_blocked:
            self.log(
                f"{source} {direction}: LOCAL FAILOVER ENTRY BLOCK active. "
                "No new CE/PE entry until /history recovers."
            )
            return
        if now.time() >= ENTRY_CUTOFF:
            self.log(
                f"{source} {direction} detected, but entry cutoff has been reached."
            )
            return

        desired_type = "CE" if direction == "BULLISH" else "PE"
        wrong_type = "PE" if direction == "BULLISH" else "CE"

        # CROSSOVER CONTROL:
        # 1) Close ALL long NIFTY MIS positions on the wrong side from broker book.
        # 2) Re-read broker book.
        # 3) If same-side position already exists, do not duplicate it.
        # 4) Otherwise place the new ITM3 entry.
        self.log(
            f"{source} {direction}: broker-wide switch requested | "
            f"close ALL {wrong_type}, then ensure {desired_type}."
        )

        closed = self._square_off_broker_positions_by_type(
            wrong_type,
            f"{source}_{direction}_WRONG_SIDE_EXIT",
        )
        if closed:
            # Give Analyzer/broker positionbook a brief moment to reflect exits.
            time.sleep(0.5)

        if self._broker_has_same_side_long(direction):
            # If the same side exists at broker, avoid duplicate buying.
            if self.position is not None and self.position.option_type != desired_type:
                self.position = None
            return

        self._place_entry(direction)

    def _process_new_local_candle(
        self, df: pd.DataFrame
    ) -> None:
        latest_candle = df.index[-1]
        row = df.iloc[-1]
        ema5 = float(row["ema5"])
        ema9 = float(row["ema9"])
        close = float(row["close"])

        signal = self._fresh_cross_from_stored_relation(
            ema5, ema9
        )
        current_relation = self._relation_from_values(
            ema5, ema9
        )

        self.log(
            f"1MIN CLOSED {latest_candle.strftime('%H:%M')} "
            f"NIFTY={close:.2f} EMA5={ema5:.2f} EMA9={ema9:.2f} "
            f"prior_relation={self.last_processed_relation or 'NEUTRAL'} "
            f"current_relation={current_relation or 'NEUTRAL'} "
            f"cross={signal} "
            f"retry={self.retry_cross_direction or 'NONE'}"
        )

        # Compare previous -> current first, then store the new relationship.
        self.last_processed_candle = latest_candle
        self.last_processed_relation = current_relation

        # After a risk exit, reaching the opposite EMA regime only ARMS a
        # possible future re-entry. The lock is NOT cleared here. Re-entry into
        # the stopped side still requires a NEW crossover back plus strict filter.
        if self.risk_exit_lock_direction is not None:
            opposite = (
                "BEARISH"
                if self.risk_exit_lock_direction == "BULLISH"
                else "BULLISH"
            )
            if current_relation == opposite and not self.risk_reentry_armed:
                self.risk_reentry_armed = True
                self.log(
                    f"RISK RE-ENTRY ARMED: blocked direction="
                    f"{self.risk_exit_lock_direction}; EMA completed opposite "
                    f"regime={current_relation}. Waiting for NEW crossover back "
                    "plus strict filter."
                )

        # A brand-new cross replaces any old retry state.
        if signal == "BULLISH":
            self.retry_cross_direction = None
            self._start_pending_cross("BULLISH", df)
            return

        if signal == "BEARISH":
            self.retry_cross_direction = None
            self._start_pending_cross("BEARISH", df)
            return

        # If a prior 10-second confirmation failed, do not wait for another
        # fresh EMA crossover. On each new completed 1-minute candle, retry
        # the same 10-second confirmation while that EMA regime still persists.
        if self.retry_cross_direction is not None:
            if current_relation == self.retry_cross_direction:
                if self.pending_cross is None:
                    self.log(
                        f"{self.retry_cross_direction} RETRY: EMA regime still valid "
                        f"on {latest_candle.strftime('%H:%M')} completed candle. "
                        f"Re-arming {FRESH_CROSS_CONFIRM_SECONDS:.0f}s continuation check."
                    )
                    self._start_pending_cross(
                        self.retry_cross_direction,
                        df,
                    )
                return

            self.log(
                f"{self.retry_cross_direction} RETRY CANCELLED: EMA regime changed "
                f"to {current_relation or 'NEUTRAL'}."
            )
            self.retry_cross_direction = None

        # Existing-regime behavior when flat remains unchanged.
        self._follow_existing_regime_if_flat(df)


    # -------------------------------------------------------------------------
    # Main
    # -------------------------------------------------------------------------

    def run(self) -> None:
        self.log("=" * 88)
        self.log(
            "EXIT RULE: EMA crossover switching + -5% hard loss + profit trail from +5%. "
            "Risk exits lock same-side re-entry until EMA flips opposite; "
            "15:29 force-square-off closes all long NIFTY MIS options."
        )
        self.log(
            "NIFTY50 EMA5/EMA9 1MIN | 10SEC FRESH-CROSS CONFIRM | "
            "HISTORY PRIMARY | LOCAL FAILOVER EXIT-ONLY | BROKER-WIDE WRONG-SIDE EXIT | 25K SANDBOX"
        )
        self.log(
            "DATA FIX: stored prior EMA relation + local 1m EMA continuity "
            "protect against temporary /history failures."
        )
        self.log("=" * 88)

        # Startup: seed EMA state from history once.
        df = self._fetch_closed_history()
        self._seed_local_ema_state(df)

        latest = self.local_df.iloc[-1]
        startup_regime = self._current_regime(self.local_df)

        self.log(
            f"STARTUP: NIFTY={float(latest['close']):.2f} "
            f"EMA5={float(latest['ema5']):.2f} "
            f"EMA9={float(latest['ema9']):.2f} "
            f"regime={startup_regime}"
        )

        # Existing-regime immediate-entry behavior remains unchanged.
        if (
            self.now_ist().time() < ENTRY_CUTOFF
            and startup_regime in {"BULLISH", "BEARISH"}
        ):
            self.log(
                f"STARTUP EXISTING REGIME {startup_regime}: "
                "entering immediately; no fresh crossover required."
            )
            self._act_on_direction(
                startup_regime, "STARTUP_EXISTING_REGIME"
            )

        while True:
            now = self.now_ist()

            try:
                self._monitor_position_trailing()
            except Exception as exc:
                self.log(f"TRAIL MONITOR ERROR: {exc}")

            if now.time() >= FORCE_CLOSE:
                try:
                    self._square_off_all_broker_nifty_mis_longs(
                        "15_29_FORCE_SQUARE_OFF"
                    )
                except Exception as exc:
                    self.log(
                        f"15:29 BROKER-WIDE FORCE EXIT ERROR: {exc}"
                    )

                self.log(
                    "15:29 IST reached. Broker-wide long NIFTY MIS option "
                    "square-off attempted. No new orders."
                )
                break

            # 10-second fresh-cross confirmation is checked every loop.
            if self.pending_cross is not None:
                try:
                    self._evaluate_pending_cross_5sec()
                except Exception as exc:
                    self.log(f"10SEC CROSS CONFIRM ERROR: {exc}")

            # Maintain local completed-candle/EMA state independently of history.
            try:
                local_df = self._poll_local_completed_candle()
                if local_df is not None:
                    self._process_new_local_candle(local_df)
            except Exception as exc:
                # Do NOT clear/reset EMA or prior-relation state.
                self.log(f"LOCAL DATA LOOP ERROR: {exc}")

            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        NiftyImmediateEMA().run()
    except KeyboardInterrupt:
        print("Stopped by user.", flush=True)
    except Exception as exc:
        print(
            f"FATAL ERROR: {type(exc).__name__}: {exc}",
            flush=True,
        )
        raise
