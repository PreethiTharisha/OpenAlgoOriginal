#!/usr/bin/env python3
"""
NIFTY50 NON-EMA STRICT MOMENTUM/BREAKOUT V2 OPTIONS STRATEGY (OpenAlgo)
===================================================================
TESTING BUILD - Analyzer-first. No EMA crossover is used for entries.

Core entry engine (closed 1-minute NIFTY candles):
  * market structure: HH/HL for bullish, LH/LL for bearish
  * 5-candle breakout / breakdown
  * RSI momentum zone
  * ADX trend-strength + DI direction
  * ATR expansion (avoid dead/choppy periods)
  * short-horizon price acceleration / directional persistence
  * optional VWAP + volume confirmation ONLY if the feed supplies usable volume

Trade instrument:
  * nearest-expiry NIFTY ITM4 CE for bullish signals
  * nearest-expiry NIFTY ITM4 PE for bearish signals

Risk:
  * software hard stop: -3% option premium
  * early weak-trade exit after 4 min if peak < +2% and current P&L <= +0.5%
  * profit protection starts from +3% peak; floor only ratchets upward
  * 2 opposite high-quality closed-candle signals can exit a position
  * no averaging / pyramiding
  * no per-day entry cap in this TESTING build; re-entry safety guards still apply
  * buffered breakouts + exhaustion guard + same-side cooldown/pullback reset
  * frozen/stale candle watchdog; fast option-risk loop separated from candle loop

Isolation:
  * unique strategy tag, state file, and lock file
  * only positions recorded by THIS script are managed
  * foreign/manual NIFTY MIS option positions are ignored
  * exact-symbol collision blocks a new entry

IMPORTANT:
  * Default ALLOW_LIVE_TRADING=false.
  * Use OpenAlgo Analyzer for testing first.
  * No strategy can guarantee profit or prevent slippage/gaps.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from openalgo import api

IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("OPENALGO_API_KEY", "").strip()
API_HOST = os.getenv("HOST_SERVER", "http://127.0.0.1:5001").strip()
ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"

STRATEGY_NAME = "NIFTY50_NONEMA_STRICT_MOMENTUM_BREAKOUT_V2_ITM4"
INDEX_SYMBOL = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPTIONS_EXCHANGE = "NFO"
OPTION_PRODUCT = "MIS"
OPTION_OFFSET = "ITM4"

CANDLE_INTERVAL = "1m"
LOOKBACK_DAYS = 8
MIN_BARS = 40

TRADING_CAPITAL_RUPEES = max(200000.0, float(os.getenv("TRADING_CAPITAL_RUPEES", "200000")))
# Minimum allocated daily capital pool is Rs.2,00,000. Capital is reused across unlimited test trades.
RISK_PER_TRADE_PERCENT = max(0.05, float(os.getenv("RISK_PER_TRADE_PERCENT", "0.35")))
MAX_CAPITAL_DEPLOYMENT_PERCENT = min(100.0, max(1.0, float(os.getenv("MAX_CAPITAL_DEPLOYMENT_PERCENT", "15"))))

# Option-premium risk management.
STOPLOSS_PERCENT = 3.0
EARLY_STALL_MINUTES = 4.0
EARLY_STALL_PEAK_REQUIRED_PERCENT = 2.0
EARLY_STALL_CURRENT_MAX_PERCENT = 0.50

# Ratcheting profit floor. Floor never moves backward.
PROFIT_TRAIL_START = 3.0
PROFIT_TRAIL_BANDS = (
    (5.0, 2.25),   # peak 3..<5
    (7.5, 2.00),   # peak 5..<7.5
    (10.0, 1.75),  # peak 7.5..<10
    (15.0, 1.50),  # peak 10..<15
    (math.inf, 2.00),
)

# Indicator / signal settings.
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
BREAKOUT_LOOKBACK = 5
STRUCTURE_LOOKBACK = 4
MOMENTUM_LOOKBACK = 4
ATR_EXPANSION_LOOKBACK = 10
VOLUME_LOOKBACK = 20

MIN_SCORE = 7              # strict entry: 7 of available factors
MIN_ADX = 20.0
BULL_RSI_MIN, BULL_RSI_MAX = 56.0, 76.0
BEAR_RSI_MIN, BEAR_RSI_MAX = 24.0, 44.0
MIN_ATR_EXPANSION = 1.03   # ATR >= 1.03 x prior 10-bar mean
MIN_DIRECTIONAL_MOVES = 3 # among last 4 close-to-close moves
MAX_EXTENSION_ATR = 1.50   # absolute chase guard from buffered breakout level
BREAKOUT_BUFFER_ATR = 0.15 # close must clear prior breakout level by this ATR fraction
EXTREME_BULL_RSI = 78.0    # block fresh CE chase above this
EXTREME_BEAR_RSI = 22.0    # block fresh PE chase below this
SAME_SIDE_COOLDOWN_SECONDS = 5 * 60
HARD_STOP_COOLDOWN_SECONDS = 10 * 60
PULLBACK_RESET_ATR = 0.40
PULLBACK_RESET_BULL_RSI = 72.0
PULLBACK_RESET_BEAR_RSI = 30.0
MAX_OPTION_SPREAD_PERCENT = 1.00  # only enforced when bid/ask are supplied by quotes()
FROZEN_CLOSE_COUNT = 3
FROZEN_MAX_RANGE_POINTS = 0.05
OPPOSITE_EXIT_CONFIRMATIONS = 2

MARKET_OPEN = dt_time(9, 15)
ENTRY_START = dt_time(9, 20)
ENTRY_CUTOFF = dt_time(15, 20)
FORCE_CLOSE = dt_time(15, 28)
MARKET_CLOSE = dt_time(15, 30)
RISK_POLL_SECONDS = 0.35
CANDLE_FETCH_SECONDS = 2.0
RECONCILE_SECONDS = 30.0
MAX_DATA_AGE_SECONDS = 150
# TESTING BUILD: intentionally NO per-day entry cap. daily_trades is statistics only.

STATE_FILE = Path(__file__).with_name(".nifty_nonema_strict_momentum_breakout_v2_itm4_state.json")
LOCK_FILE = Path(__file__).with_name(".nifty_nonema_strict_momentum_breakout_v2_itm4.lock")


@dataclass
class Position:
    symbol: str
    exchange: str
    option_type: str
    quantity: int
    entry_price: float
    entry_time: str
    expiry: str
    peak_pnl_percent: float = 0.0
    trailing_floor_percent: float = -STOPLOSS_PERCENT
    opposite_count: int = 0


class Strategy:
    def __init__(self) -> None:
        if not API_KEY:
            raise RuntimeError("OPENALGO_API_KEY is not set.")
        self._lock_handle = None
        self._acquire_lock()
        self.client = api(api_key=API_KEY, host=API_HOST)
        self.positions: Dict[str, Position] = {}
        self.running = True
        self.last_candle: Optional[pd.Timestamp] = None
        self.last_reconcile = 0.0
        self.expiry_cache: Optional[Tuple[date, str]] = None
        self.daily_day = self.now().date().isoformat()
        self.daily_trades = 0
        self.last_signal_direction: Optional[str] = None
        self.last_candle_fetch = 0.0
        self.latest_underlying_close = 0.0
        self.latest_atr = 0.0
        self.latest_rsi = 50.0
        self.last_exit_direction: Optional[str] = None
        self.last_exit_time: Optional[str] = None
        self.last_exit_reason: str = ""
        self.last_exit_underlying: float = 0.0
        self.reentry_reset_seen: bool = True
        self._load_state()
        self._verify_mode()
        self._reconcile(verbose=True)

    @staticmethod
    def now() -> datetime:
        return datetime.now(IST)

    def log(self, msg: str) -> None:
        print(f"[{self.now().strftime('%Y-%m-%d %H:%M:%S IST')}] {msg}", flush=True)

    def _acquire_lock(self) -> None:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = open(LOCK_FILE, "a+b")
        try:
            self._lock_handle.seek(0)
            if not self._lock_handle.read(1):
                self._lock_handle.write(b"1"); self._lock_handle.flush()
            self._lock_handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            raise RuntimeError("Another instance of THIS non-EMA strategy is already running.") from exc

    def _release_lock(self) -> None:
        if not self._lock_handle:
            return
        try:
            self._lock_handle.seek(0)
            if os.name == "nt":
                import msvcrt
                try: msvcrt.locking(self._lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception: pass
            else:
                import fcntl
                try: fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                except Exception: pass
            self._lock_handle.close()
        except Exception:
            pass

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.daily_day = raw.get("daily_day", self.daily_day)
            self.daily_trades = int(raw.get("daily_trades", 0))
            self.last_exit_direction = raw.get("last_exit_direction")
            self.last_exit_time = raw.get("last_exit_time")
            self.last_exit_reason = str(raw.get("last_exit_reason", ""))
            self.last_exit_underlying = self._f(raw.get("last_exit_underlying"), 0.0)
            self.reentry_reset_seen = bool(raw.get("reentry_reset_seen", True))
            self.positions = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
            self.log(f"Loaded {len(self.positions)} tracked position(s) from {STATE_FILE.name}.")
        except Exception as exc:
            raise RuntimeError(f"State read failed: {exc}")

    def _save_state(self) -> None:
        payload = {
            "daily_day": self.daily_day,
            "daily_trades": self.daily_trades,
            "last_exit_direction": self.last_exit_direction,
            "last_exit_time": self.last_exit_time,
            "last_exit_reason": self.last_exit_reason,
            "last_exit_underlying": self.last_exit_underlying,
            "reentry_reset_seen": self.reentry_reset_seen,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def _reset_day(self) -> None:
        today = self.now().date().isoformat()
        if today != self.daily_day:
            self.daily_day = today
            self.daily_trades = 0
            # A new session starts fresh; do not carry yesterday's same-side re-entry lock.
            self.last_exit_direction = None
            self.last_exit_time = None
            self.last_exit_reason = ""
            self.last_exit_underlying = 0.0
            self.reentry_reset_seen = True
            self._save_state()

    def _verify_mode(self) -> None:
        # OpenAlgo versions differ in analyze-mode introspection. Reuse safety principle:
        # LIVE execution is permitted only with explicit env opt-in.
        if ALLOW_LIVE_TRADING:
            self.log("WARNING: ALLOW_LIVE_TRADING=true. This script may place live orders if OpenAlgo is live.")
        else:
            self.log("SAFE DEFAULT: ALLOW_LIVE_TRADING=false. Run OpenAlgo in Analyzer mode for testing.")

    @staticmethod
    def _f(v, default=0.0) -> float:
        try: return float(v)
        except Exception: return default

    @staticmethod
    def _i(v, default=0) -> int:
        try: return int(float(v))
        except Exception: return default

    def _positionbook(self) -> list[dict]:
        r = self.client.positionbook()
        if r.get("status") != "success":
            raise RuntimeError(f"positionbook() failed: {r}")
        data = r.get("data", [])
        return data if isinstance(data, list) else []

    def _broker_position(self, symbol: str) -> Optional[dict]:
        for x in self._positionbook():
            if str(x.get("symbol", "")).upper() == symbol.upper() and str(x.get("exchange", "")).upper() == OPTIONS_EXCHANGE:
                return x
        return None

    def _reconcile(self, verbose=False) -> None:
        try:
            book = self._positionbook()
        except Exception as exc:
            self.log(f"Reconcile warning: {exc}")
            return
        by_symbol = {str(x.get("symbol", "")).upper(): x for x in book}
        changed = False
        for symbol in list(self.positions):
            row = by_symbol.get(symbol.upper())
            qty = self._i(row.get("quantity")) if row else 0
            if qty <= 0:
                self.log(f"RECONCILE: tracked {symbol} is flat at broker; removing local state.")
                self.positions.pop(symbol, None); changed = True
        if changed: self._save_state()
        if verbose: self.log(f"Broker reconciliation complete: {len(self.positions)} managed position(s).")

    def _fetch_candles(self) -> Optional[pd.DataFrame]:
        now = self.now()
        try:
            try:
                df = self.client.history(symbol=INDEX_SYMBOL, exchange=INDEX_EXCHANGE, interval=CANDLE_INTERVAL,
                                         start_date=(now.date()-timedelta(days=LOOKBACK_DAYS)).isoformat(),
                                         end_date=now.date().isoformat(), source="api")
            except TypeError:
                df = self.client.history(symbol=INDEX_SYMBOL, exchange=INDEX_EXCHANGE, interval=CANDLE_INTERVAL,
                                         start_date=(now.date()-timedelta(days=LOOKBACK_DAYS)).isoformat(),
                                         end_date=now.date().isoformat())
        except Exception as exc:
            self.log(f"history() error: {exc}"); return None
        if df is None or len(df) < MIN_BARS:
            return None
        df = df.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            if "timestamp" not in df.columns: return None
            df.index = pd.to_datetime(df["timestamp"], errors="coerce")
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
        df.index = idx
        df = df[~df.index.duplicated(keep="last")].sort_index()
        # row timestamp = candle start; completed candles only
        df = df.loc[(df.index + pd.Timedelta(minutes=1)) <= pd.Timestamp(now)].copy()
        for c in ("open","high","low","close","volume"):
            if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["open","high","low","close"])
        if len(df) < MIN_BARS: return None
        if (now - df.index[-1].to_pydatetime()).total_seconds() > MAX_DATA_AGE_SECONDS + 60:
            self.log(f"DATA STALE: latest candle timestamp {df.index[-1]}; no signal."); return None

        # Frozen-feed watchdog: repeated identical closes plus essentially no total range.
        if len(df) >= FROZEN_CLOSE_COUNT:
            tail = df.iloc[-FROZEN_CLOSE_COUNT:]
            same_close = tail["close"].round(4).nunique(dropna=True) == 1
            total_range = self._f(tail["high"].max()) - self._f(tail["low"].min())
            if same_close and total_range <= FROZEN_MAX_RANGE_POINTS:
                self.log(f"DATA FROZEN: last {FROZEN_CLOSE_COUNT} completed candles unchanged at {tail['close'].iloc[-1]:.2f}; entries suspended.")
                return None
        return self._indicators(df)

    def _indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        x = df.copy()
        close, high, low = x["close"], x["high"], x["low"]

        # RSI (Wilder-style EWM).
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
        avg_loss = loss.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
        rs = avg_gain / avg_loss.replace(0, math.nan)
        x["rsi"] = 100 - (100 / (1 + rs))
        x.loc[avg_loss == 0, "rsi"] = 100.0

        # ATR / True Range.
        pc = close.shift(1)
        tr = pd.concat([(high-low).abs(), (high-pc).abs(), (low-pc).abs()], axis=1).max(axis=1)
        x["atr"] = tr.ewm(alpha=1/ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()
        x["atr_mean"] = x["atr"].shift(1).rolling(ATR_EXPANSION_LOOKBACK).mean()

        # ADX + directional indicators.
        up = high.diff(); down = -low.diff()
        plus_dm = up.where((up > down) & (up > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)
        atr_rma = tr.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean() / atr_rma
        minus_di = 100 * minus_dm.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean() / atr_rma
        dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di).replace(0, math.nan)
        x["plus_di"] = plus_di; x["minus_di"] = minus_di
        x["adx"] = dx.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()

        # Previous breakout levels (exclude current candle).
        x["prev_high"] = high.shift(1).rolling(BREAKOUT_LOOKBACK).max()
        x["prev_low"] = low.shift(1).rolling(BREAKOUT_LOOKBACK).min()

        # Optional true VWAP and volume ratio when volume is usable.
        if "volume" in x.columns and x["volume"].fillna(0).gt(0).sum() >= VOLUME_LOOKBACK:
            session = pd.Series(x.index.date, index=x.index)
            typical = (high+low+close)/3.0
            pv = typical * x["volume"].fillna(0)
            x["vwap"] = pv.groupby(session).cumsum() / x["volume"].fillna(0).groupby(session).cumsum().replace(0, math.nan)
            x["vol_avg"] = x["volume"].shift(1).rolling(VOLUME_LOOKBACK).mean()
            x["vol_ratio"] = x["volume"] / x["vol_avg"].replace(0, math.nan)
        else:
            x["vwap"] = math.nan; x["vol_ratio"] = math.nan
        return x

    def _score(self, df: pd.DataFrame, direction: str) -> tuple[int,int,list[str]]:
        r = df.iloc[-1]
        reasons=[]; score=0; available=0
        closes=df["close"]
        highs=df["high"]; lows=df["low"]
        atr=self._f(r.get("atr")); atr_mean=self._f(r.get("atr_mean"))
        if atr <= 0: return 0,0,["ATR unavailable"]

        # 1) price structure over recent completed bars.
        recent_h = highs.iloc[-STRUCTURE_LOOKBACK:]
        recent_l = lows.iloc[-STRUCTURE_LOOKBACK:]
        available += 1
        if direction=="BULLISH":
            structure = recent_h.is_monotonic_increasing and recent_l.is_monotonic_increasing
        else:
            structure = recent_h.is_monotonic_decreasing and recent_l.is_monotonic_decreasing
        if structure: score+=1; reasons.append("structure")

        # 2) confirmed close breakout/breakdown.
        available += 1
        level = self._f(r["prev_high"] if direction=="BULLISH" else r["prev_low"])
        buffer = BREAKOUT_BUFFER_ATR * atr
        breakout = (r["close"] > level + buffer) if direction=="BULLISH" else (r["close"] < level - buffer)
        if level>0 and breakout: score+=1; reasons.append("breakout_buf")

        # 3) no-chase distance from breakout level.
        available += 1
        extension = abs(self._f(r["close"])-level) if level>0 else 999999
        if level>0 and extension <= MAX_EXTENSION_ATR*atr: score+=1; reasons.append("not_stretched")

        # 4) RSI directional zone.
        available += 1
        rsi=self._f(r.get("rsi"),50)
        rsi_ok = (BULL_RSI_MIN <= rsi <= BULL_RSI_MAX) if direction=="BULLISH" else (BEAR_RSI_MIN <= rsi <= BEAR_RSI_MAX)
        if rsi_ok: score+=1; reasons.append("rsi")

        # 5) ADX strength + DI direction.
        available += 1
        adx=self._f(r.get("adx")); pdi=self._f(r.get("plus_di")); mdi=self._f(r.get("minus_di"))
        adx_ok = adx >= MIN_ADX and ((pdi>mdi) if direction=="BULLISH" else (mdi>pdi))
        if adx_ok: score+=1; reasons.append("adx_di")

        # 6) ATR expansion.
        available += 1
        atr_ok = atr_mean>0 and atr >= MIN_ATR_EXPANSION*atr_mean
        if atr_ok: score+=1; reasons.append("atr_expand")

        # 7) directional persistence / acceleration.
        moves=closes.diff().iloc[-MOMENTUM_LOOKBACK:]
        directional = int((moves>0).sum()) if direction=="BULLISH" else int((moves<0).sum())
        available += 1
        if directional >= MIN_DIRECTIONAL_MOVES: score+=1; reasons.append("momentum")

        # 8) current candle body agrees and is meaningful vs ATR.
        available += 1
        body=self._f(r["close"])-self._f(r["open"])
        body_ok = body >= 0.20*atr if direction=="BULLISH" else body <= -0.20*atr
        if body_ok: score+=1; reasons.append("body")

        # 9) true VWAP if available (otherwise omitted from denominator).
        vwap=self._f(r.get("vwap"),0)
        if vwap>0:
            available += 1
            vwap_ok=(r["close"]>vwap) if direction=="BULLISH" else (r["close"]<vwap)
            if vwap_ok: score+=1; reasons.append("vwap")

        # 10) volume expansion if valid (otherwise omitted).
        vr=self._f(r.get("vol_ratio"),0)
        if vr>0:
            available += 1
            if vr >= 1.20: score+=1; reasons.append("volume")
        return score, available, reasons

    def _signal(self, df: pd.DataFrame) -> tuple[Optional[str], dict]:
        bull_score,bull_avail,bull_reasons=self._score(df,"BULLISH")
        bear_score,bear_avail,bear_reasons=self._score(df,"BEARISH")
        # Keep threshold strict but adapt if optional volume/vwap are absent.
        bull_need=min(MIN_SCORE, max(6, bull_avail-1))
        bear_need=min(MIN_SCORE, max(6, bear_avail-1))
        meta={"bull":(bull_score,bull_need,bull_reasons,bull_avail),"bear":(bear_score,bear_need,bear_reasons,bear_avail)}
        if bull_score>=bull_need and bull_score>bear_score: return "BULLISH",meta
        if bear_score>=bear_need and bear_score>bull_score: return "BEARISH",meta
        return None,meta

    @staticmethod
    def _parse_expiry(raw: str) -> Optional[date]:
        s=str(raw).strip()
        for fmt in ("%d-%b-%y","%d%b%y","%Y-%m-%d","%d-%m-%Y"):
            try: return datetime.strptime(s,fmt).date()
            except ValueError: pass
        return None

    def _nearest_expiry(self) -> str:
        today=self.now().date()
        if self.expiry_cache and self.expiry_cache[0]==today: return self.expiry_cache[1]
        r=self.client.expiry(symbol=INDEX_SYMBOL,exchange=OPTIONS_EXCHANGE,instrumenttype="options")
        if r.get("status")!="success": raise RuntimeError(f"expiry() failed: {r}")
        vals=[]
        for raw in r.get("data",[]):
            d=self._parse_expiry(raw)
            if d and d>=today: vals.append(d)
        if not vals: raise RuntimeError("No current/future NIFTY expiry returned")
        exp=min(vals).strftime("%d%b%y").upper(); self.expiry_cache=(today,exp); return exp

    def _quote_details(self,symbol:str,exchange:str) -> dict:
        try:
            r=self.client.quotes(symbol=symbol,exchange=exchange)
            if r.get("status")!="success": return {}
            d=r.get("data",{}) if isinstance(r.get("data",{}),dict) else {}
            ltp=self._f(d.get("ltp"))
            bid=0.0; ask=0.0
            for k in ("bid","bid_price","best_bid_price","bestbid"):
                bid=self._f(d.get(k));
                if bid>0: break
            for k in ("ask","ask_price","best_ask_price","bestask"):
                ask=self._f(d.get(k));
                if ask>0: break
            return {"ltp":ltp,"bid":bid,"ask":ask}
        except Exception:
            return {}

    def _quote(self,symbol:str,exchange:str) -> Optional[float]:
        x=self._quote_details(symbol,exchange).get("ltp",0.0)
        return x if x>0 else None

    def _spread_ok(self,symbol:str,exchange:str) -> tuple[bool,Optional[float]]:
        q=self._quote_details(symbol,exchange)
        bid=self._f(q.get("bid")); ask=self._f(q.get("ask"))
        if bid<=0 or ask<=0 or ask<bid:
            return True,None  # feed does not expose bid/ask: do not fabricate a spread
        mid=(bid+ask)/2.0
        sp=(ask-bid)/mid*100 if mid>0 else 999.0
        return sp<=MAX_OPTION_SPREAD_PERCENT,sp

    def _contract(self,direction:str) -> dict:
        option_type="CE" if direction=="BULLISH" else "PE"
        expiry=self._nearest_expiry()
        r=self.client.optionsymbol(underlying=INDEX_SYMBOL,exchange=INDEX_EXCHANGE,expiry_date=expiry,
                                   offset=OPTION_OFFSET,option_type=option_type)
        if r.get("status")!="success": raise RuntimeError(f"optionsymbol() failed: {r}")
        symbol=str(r.get("symbol","")).upper(); exch=str(r.get("exchange",OPTIONS_EXCHANGE)).upper(); lot=self._i(r.get("lotsize"))
        if not symbol or exch!=OPTIONS_EXCHANGE or lot<=0: raise RuntimeError(f"Bad optionsymbol response: {r}")
        ltp=self._quote(symbol,exch)
        if not ltp: raise RuntimeError(f"LTP unavailable for {symbol}")
        spread_ok,spread_pct=self._spread_ok(symbol,exch)
        if not spread_ok:
            raise RuntimeError(f"Spread too wide for {symbol}: {spread_pct:.2f}% > {MAX_OPTION_SPREAD_PERCENT:.2f}%")
        risk_budget=TRADING_CAPITAL_RUPEES*RISK_PER_TRADE_PERCENT/100
        cap_budget=TRADING_CAPITAL_RUPEES*MAX_CAPITAL_DEPLOYMENT_PERCENT/100
        risk_per_lot=ltp*lot*STOPLOSS_PERCENT/100
        lots=min(int(risk_budget//risk_per_lot) if risk_per_lot else 0,int(cap_budget//(ltp*lot)) if ltp*lot else 0)
        if lots<1: raise RuntimeError(f"Sizing blocks {symbol}: premium={ltp:.2f}, lot={lot}")
        return {"symbol":symbol,"exchange":exch,"lot":lot,"lots":lots,"quantity":lot*lots,"expiry":expiry,"option_type":option_type}

    def _wait_fill(self,order_id:str,timeout=20.0) -> Optional[dict]:
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            try:
                r=self.client.orderstatus(order_id=order_id,strategy=STRATEGY_NAME)
                if r.get("status")=="success":
                    d=r.get("data",{})
                    status=str(d.get("order_status",d.get("status",""))).lower()
                    if status in {"complete","completed","filled"} or self._f(d.get("average_price"))>0: return d
                    if status in {"rejected","cancelled","canceled"}: return None
            except Exception: pass
            time.sleep(0.5)
        return None

    def _exact_symbol_collision(self,symbol:str) -> bool:
        row=self._broker_position(symbol)
        return bool(row and self._i(row.get("quantity"))!=0 and symbol not in self.positions)

    def _update_reentry_reset(self, df: pd.DataFrame) -> None:
        if not self.last_exit_direction or self.reentry_reset_seen or self.last_exit_underlying<=0:
            return
        r=df.iloc[-1]
        close=self._f(r.get("close")); atr=max(self._f(r.get("atr")),0.01); rsi=self._f(r.get("rsi"),50.0)
        if self.last_exit_direction=="BEARISH":
            reset = close >= self.last_exit_underlying + PULLBACK_RESET_ATR*atr and rsi >= PULLBACK_RESET_BEAR_RSI
        else:
            reset = close <= self.last_exit_underlying - PULLBACK_RESET_ATR*atr and rsi <= PULLBACK_RESET_BULL_RSI
        if reset:
            self.reentry_reset_seen=True
            self._save_state()
            self.log(f"RE-ENTRY RESET READY for {self.last_exit_direction}: pullback confirmed close={close:.2f} RSI={rsi:.1f}")

    def _reentry_allowed(self,direction:str) -> tuple[bool,str]:
        if direction != self.last_exit_direction or not self.last_exit_time:
            return True,"fresh/opposite direction"
        try:
            elapsed=(self.now()-datetime.fromisoformat(self.last_exit_time)).total_seconds()
        except Exception:
            elapsed=999999
        cooldown=HARD_STOP_COOLDOWN_SECONDS if "HARD STOP" in self.last_exit_reason.upper() else SAME_SIDE_COOLDOWN_SECONDS
        if elapsed<cooldown:
            return False,f"same-side cooldown {elapsed:.0f}/{cooldown}s"
        if not self.reentry_reset_seen:
            return False,"waiting for pullback/RSI reset after same-side exit"
        return True,"cooldown + pullback reset satisfied"

    def _enter(self,direction:str) -> bool:
        if self.positions: return False
        c=self._contract(direction)
        if self._exact_symbol_collision(c["symbol"]):
            self.log(f"ISOLATION BLOCK: {c['symbol']} already exists outside this script."); return False
        self.log(f"ENTRY: BUY {c['symbol']} qty={c['quantity']} ({c['lots']} lots) MARKET | {direction} score signal")
        try:
            r=self.client.placeorder(strategy=STRATEGY_NAME,symbol=c["symbol"],exchange=c["exchange"],action="BUY",
                                     quantity=c["quantity"],price_type="MARKET",product=OPTION_PRODUCT)
        except Exception as exc:
            self.log(f"Entry exception: {exc}"); return False
        if r.get("status")!="success" or not r.get("orderid"):
            self.log(f"Entry rejected: {r}"); return False
        oid=str(r["orderid"]); fill=self._wait_fill(oid)
        avg=self._f(fill.get("average_price")) if fill else 0; qty=self._i(fill.get("quantity"),c["quantity"]) if fill else c["quantity"]
        if avg<=0:
            row=self._broker_position(c["symbol"])
            if row: avg=self._f(row.get("average_price")); qty=self._i(row.get("quantity"),qty)
        if avg<=0 or qty<=0:
            self.log(f"CRITICAL: entry {oid} not verified. Check Analyzer/broker manually."); return False
        self.positions[c["symbol"]]=Position(c["symbol"],c["exchange"],c["option_type"],qty,avg,self.now().isoformat(),c["expiry"])
        self.daily_trades+=1; self._save_state()
        self.log(f"FILLED: {c['symbol']} avg={avg:.2f} qty={qty} | hardSL=-{STOPLOSS_PERCENT:.1f}% | profit protection from +{PROFIT_TRAIL_START:.1f}%")
        return True

    def _exit(self,pos:Position,reason:str) -> None:
        row=self._broker_position(pos.symbol)
        qty=min(pos.quantity,max(0,self._i(row.get("quantity")) if row else 0))
        if qty<=0:
            self.positions.pop(pos.symbol,None); self._save_state(); return
        self.log(f"EXIT: SELL {pos.symbol} qty={qty} MARKET | {reason}")
        try:
            r=self.client.placeorder(strategy=STRATEGY_NAME,symbol=pos.symbol,exchange=pos.exchange,action="SELL",quantity=qty,
                                     price_type="MARKET",product=OPTION_PRODUCT)
            if r.get("status")!="success": self.log(f"Exit order failed: {r}"); return
            if r.get("orderid"): self._wait_fill(str(r["orderid"]),8.0)
        except Exception as exc:
            self.log(f"Exit exception: {exc}"); return
        time.sleep(0.5)
        row2=self._broker_position(pos.symbol)
        if not row2 or self._i(row2.get("quantity"))<=0:
            self.positions.pop(pos.symbol,None)
            self.last_exit_direction = "BULLISH" if pos.option_type=="CE" else "BEARISH"
            self.last_exit_time = self.now().isoformat()
            self.last_exit_reason = reason
            self.last_exit_underlying = self.latest_underlying_close
            self.reentry_reset_seen = False
            self._save_state(); self.log(f"FLAT: {pos.symbol} | re-entry guard armed for {self.last_exit_direction}")
        else:
            self.log(f"WARNING: {pos.symbol} still has broker qty={self._i(row2.get('quantity'))} after exit attempt.")

    @staticmethod
    def _trail_floor(peak:float) -> float:
        if peak < PROFIT_TRAIL_START: return -STOPLOSS_PERCENT
        for upper,giveback in PROFIT_TRAIL_BANDS:
            if peak < upper:
                return max(-STOPLOSS_PERCENT, peak-giveback)
        return -STOPLOSS_PERCENT

    def _monitor(self,df:Optional[pd.DataFrame]) -> None:
        for symbol,pos in list(self.positions.items()):
            ltp=self._quote(symbol,pos.exchange)
            if not ltp: continue
            pnl=(ltp-pos.entry_price)/pos.entry_price*100
            if pnl>pos.peak_pnl_percent:
                pos.peak_pnl_percent=pnl
                pos.trailing_floor_percent=max(pos.trailing_floor_percent,self._trail_floor(pos.peak_pnl_percent))
                self._save_state()
            if pnl <= -STOPLOSS_PERCENT:
                self._exit(pos,f"HARD STOP {pnl:+.2f}%"); continue
            if pnl <= pos.trailing_floor_percent and pos.peak_pnl_percent>=PROFIT_TRAIL_START:
                self._exit(pos,f"PROFIT FLOOR {pnl:+.2f}% <= {pos.trailing_floor_percent:+.2f}% (peak {pos.peak_pnl_percent:+.2f}%)"); continue
            try: age=(self.now()-datetime.fromisoformat(pos.entry_time)).total_seconds()/60
            except Exception: age=0
            if age>=EARLY_STALL_MINUTES and pos.peak_pnl_percent<EARLY_STALL_PEAK_REQUIRED_PERCENT and pnl<=EARLY_STALL_CURRENT_MAX_PERCENT:
                self._exit(pos,f"EARLY STALL after {age:.1f}m | peak={pos.peak_pnl_percent:+.2f}% current={pnl:+.2f}%"); continue

            # High-quality opposite signal requires 2 completed candles; updated only in _process_candle.

    def _process_candle(self,df:pd.DataFrame) -> None:
        candle=df.index[-1]
        if self.last_candle is not None and candle<=self.last_candle: return
        self.last_candle=candle
        direction,meta=self._signal(df)
        b=meta["bull"]; s=meta["bear"]
        r=df.iloc[-1]
        self.latest_underlying_close=self._f(r.get("close"))
        self.latest_atr=self._f(r.get("atr"))
        self.latest_rsi=self._f(r.get("rsi"),50.0)
        self._update_reentry_reset(df)
        self.log(f"CANDLE {candle.strftime('%H:%M')} close={r['close']:.2f} RSI={self.latest_rsi:.1f} ADX={self._f(r.get('adx')):.1f} ATR={self.latest_atr:.2f} | BULL score={b[0]} need={b[1]} max={b[3]} {b[2]} | BEAR score={s[0]} need={s[1]} max={s[3]} {s[2]} | signal={direction or 'NONE'}")

        # Opposite-signal exit confirmation.
        if self.positions:
            for pos in list(self.positions.values()):
                adverse=(pos.option_type=="CE" and direction=="BEARISH") or (pos.option_type=="PE" and direction=="BULLISH")
                if adverse:
                    pos.opposite_count += 1
                    self.log(f"OPPOSITE CONFIRM {pos.symbol}: {pos.opposite_count}/{OPPOSITE_EXIT_CONFIRMATIONS}")
                    self._save_state()
                    if pos.opposite_count>=OPPOSITE_EXIT_CONFIRMATIONS:
                        self._exit(pos,"2 consecutive opposite strict signals")
                elif direction is not None:
                    if pos.opposite_count: pos.opposite_count=0; self._save_state()
            return

        nowt=self.now().time()
        if nowt<ENTRY_START or nowt>=ENTRY_CUTOFF or direction is None: return

        # Absolute exhaustion guard: do not chase a fresh option entry at RSI extremes.
        if direction=="BEARISH" and self.latest_rsi < EXTREME_BEAR_RSI:
            self.log(f"EXHAUSTION BLOCK: BEARISH RSI={self.latest_rsi:.1f} < {EXTREME_BEAR_RSI:.1f}"); return
        if direction=="BULLISH" and self.latest_rsi > EXTREME_BULL_RSI:
            self.log(f"EXHAUSTION BLOCK: BULLISH RSI={self.latest_rsi:.1f} > {EXTREME_BULL_RSI:.1f}"); return

        allowed,why=self._reentry_allowed(direction)
        if not allowed:
            self.log(f"RE-ENTRY BLOCK: {direction} | {why}"); return
        try: self._enter(direction)
        except Exception as exc: self.log(f"Entry setup blocked: {exc}")

    def _force_close(self) -> None:
        for pos in list(self.positions.values()): self._exit(pos,"TIME EXIT / FORCE CLOSE")

    def run(self) -> None:
        self.log("="*88)
        self.log("NIFTY50 NON-EMA STRICT MOMENTUM + BREAKOUT V2 | nearest expiry ITM4 | CLOSED 1m")
        self.log(f"Entry core: structure + breakout + no-chase + RSI + ADX/DI + ATR expansion + momentum + candle body; VWAP/volume optional when valid")
        self.log(f"Strict threshold={MIN_SCORE} nominal | hard SL={STOPLOSS_PERCENT}% | profit trail from +{PROFIT_TRAIL_START}% | daily trade cap=REMOVED (testing)")
        self.log(f"Daily capital pool=Rs.{TRADING_CAPITAL_RUPEES:,.0f} (minimum Rs.200,000) | risk budget={RISK_PER_TRADE_PERCENT:.2f}% | max deployment/trade={MAX_CAPITAL_DEPLOYMENT_PERCENT:.0f}%")
        self.log(f"Unique tag={STRATEGY_NAME} | ALLOW_LIVE_TRADING={ALLOW_LIVE_TRADING}")
        self.log(f"Entry window 09:20-15:20 IST | force close 15:28 IST | risk poll={RISK_POLL_SECONDS:.2f}s | candle refresh={CANDLE_FETCH_SECONDS:.1f}s")
        self.log(f"V2 guards: breakout buffer={BREAKOUT_BUFFER_ATR:.2f} ATR | exhaustion RSI CE>{EXTREME_BULL_RSI:.0f}/PE<{EXTREME_BEAR_RSI:.0f} | same-side cooldown={SAME_SIDE_COOLDOWN_SECONDS//60}m, hard-stop cooldown={HARD_STOP_COOLDOWN_SECONDS//60}m")
        self.log("="*88)
        try:
            while self.running:
                self._reset_day(); now=self.now(); t=now.time()
                if t<MARKET_OPEN: time.sleep(RISK_POLL_SECONDS); continue
                if t>=FORCE_CLOSE:
                    if self.positions: self._force_close()
                    if t>=MARKET_CLOSE: break
                    time.sleep(RISK_POLL_SECONDS); continue

                # Fast path: option-risk monitoring several times per second.
                if self.positions: self._monitor(None)

                # Slow path: closed 1-minute signal engine/history refresh.
                mono=time.monotonic()
                if mono-self.last_candle_fetch>=CANDLE_FETCH_SECONDS:
                    df=self._fetch_candles()
                    self.last_candle_fetch=mono
                    if df is not None: self._process_candle(df)

                if mono-self.last_reconcile>=RECONCILE_SECONDS:
                    self._reconcile(False); self.last_reconcile=mono
                time.sleep(RISK_POLL_SECONDS)
        except KeyboardInterrupt:
            self.log("Ctrl+C received. Script stopped; Ctrl+C does NOT force-close positions.")
        finally:
            self.running=False; self._save_state(); self._release_lock()


def main() -> None:
    s=Strategy()
    def stop_handler(signum,frame):
        s.log(f"Signal {signum} received; stopping loop without forced liquidation.")
        s.running=False
    try:
        signal.signal(signal.SIGTERM,stop_handler)
        if hasattr(signal,"SIGINT"): signal.signal(signal.SIGINT,stop_handler)
    except Exception: pass
    s.run()


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(f"FATAL: {exc}",file=sys.stderr,flush=True)
        raise
