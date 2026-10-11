"""
OpenAlgo Strict Hierarchical Multi-Filter Options Strategy
Generated for SANDBOX / ANALYZER testing first.

Architecture:
1) Market regime: EMA9/EMA21
2) Entry trigger: EMA5/EMA9
3) VWAP
4) Market structure (HH/HL or LH/LL)
5) ADX trend strength
6) ATR volatility + no-chase
7) Momentum + acceleration
8) Breakout confirmation
9) Dynamic option selection using spread/liquidity
10) Option EMA/RSI/OI/volume confirmation + adaptive loss/profit ladder
11) Time-based entry cutoff and force exit

IMPORTANT:
- No strategy guarantees profit.
- Keep OpenAlgo Analyzer Mode ON until you validate logs and backtests.
- Lot size is read dynamically from the instrument master/option-chain.
"""

from __future__ import annotations
import os, sys, time, json, math, traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Optional
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from openalgo import api

# -------------------- INSTANCE .ENV ONLY --------------------
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

API_KEY = os.getenv("OPENALGO_API_KEY", "").strip()
API_HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000").strip()
WS_URL = os.getenv("OPENALGO_WS_URL", "ws://127.0.0.1:8765").strip()
ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"

if not API_KEY:
    raise RuntimeError(
        f"OPENALGO_API_KEY missing in {ENV_PATH}. "
        "Add OPENALGO_API_KEY=<your OpenAlgo app API key>"
    )

client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)

@dataclass
class Config:
    strategy: str
    market: str
    underlying: str
    underlying_exchange: str
    option_exchange: str
    timeframe: str
    lookback_days: int
    adx_min: float
    max_atr_extension: float
    min_ema921_gap_atr: float
    score_threshold: int
    preferred_offsets: tuple
    capital: float
    capital_per_trade: float
    hard_sl_pct: float
    profit_lock_start_pct: float
    profit_lock_step_pct: float
    entry_start: dtime
    last_entry: dtime
    force_exit: dtime
    poll_seconds: int
    product: str = "MIS"
    max_relative_spread: float = 0.015

CFG = Config(
strategy="NIFTY50_MULTI_FILTER_V4_3_1_GREEKS_MICROSTRUCTURE_FIXED",
market="NSE_INDEX",
underlying="NIFTY",
underlying_exchange="NSE_INDEX",
option_exchange="NFO",
timeframe="1m",
lookback_days=3,
adx_min=22.0,
max_atr_extension=1.80,
    min_ema921_gap_atr=0.10,
score_threshold=0,
preferred_offsets=("ATM","ITM1","ITM2"),
capital=500000,
capital_per_trade=100000,
hard_sl_pct=0.10,
profit_lock_start_pct=0.20,
profit_lock_step_pct=0.05,
entry_start=dtime(9,20),
last_entry=dtime(15,20),
force_exit=dtime(15,25),
poll_seconds=5,
)

STATE_FILE = Path(__file__).with_name(f".{CFG.strategy.lower()}_state.json")
LOG_FILE = Path(__file__).with_name(
    f"{CFG.strategy}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_IST.log"
)

# -------------------- Logging --------------------
def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST] {msg}"
    # Windows terminals may use CP1252; never let console encoding crash strategy.
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        safe_line = line.encode(enc, errors="replace").decode(enc, errors="replace")
        print(safe_line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# -------------------- Persistence --------------------
def load_state():
    if not STATE_FILE.exists():
        return {"position": None, "last_entry_bar": None, "armed_ce": True, "armed_pe": True, "last_exit_side": None, "last_exit_ts": None, "last_exit_pnl": 0.0}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"position": None, "last_entry_bar": None, "armed_ce": True, "armed_pe": True, "last_exit_side": None, "last_exit_ts": None, "last_exit_pnl": 0.0}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

state = load_state()

# -------------------- Indicator helpers --------------------
def ema(s, span):
    return s.ewm(span=span, adjust=False).mean()

def true_range(df):
    pc = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs()
    ], axis=1).max(axis=1)

def atr(df, period=14):
    return true_range(df).ewm(alpha=1/period, adjust=False).mean()

def adx(df, period=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = true_range(df)
    atr_w = tr.ewm(alpha=1/period, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_w
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False).mean()

def session_vwap(df):
    # Daily reset based on exchange timestamps converted by SDK/DataFrame.
    if "datetime" in df.columns:
        dates = pd.to_datetime(df["datetime"]).dt.date
    elif isinstance(df.index, pd.DatetimeIndex):
        dates = df.index.date
    else:
        dates = pd.Series([datetime.now().date()] * len(df), index=df.index)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].fillna(0).clip(lower=0)
    pv = typical * vol
    out = pd.Series(index=df.index, dtype=float)
    for d in pd.unique(dates):
        mask = dates == d
        vv = vol[mask].cumsum().replace(0, np.nan)
        out.loc[mask] = pv[mask].cumsum() / vv
    # Index feeds may expose zero volume. Fall back to rolling typical-price mean.
    return out.fillna(typical.rolling(20, min_periods=1).mean())

def enrich(df):
    x = df.copy()
    for c in ["open", "high", "low", "close", "volume"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.dropna(subset=["open","high","low","close"]).reset_index(drop=True)
    x["ema5"] = ema(x["close"], 5)
    x["ema9"] = ema(x["close"], 9)
    x["ema21"] = ema(x["close"], 21)
    x["atr14"] = atr(x, 14)
    x["adx14"] = adx(x, 14)
    x["vwap"] = session_vwap(x)
    x["gap59"] = x["ema5"] - x["ema9"]
    x["gap921"] = x["ema9"] - x["ema21"]
    return x


def rsi(series, period=9):
    s = pd.to_numeric(series, errors="coerce")
    d = s.diff()
    gain = d.clip(lower=0.0)
    loss = (-d.clip(upper=0.0))
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def normalize_history_frame(raw):
    """Accept DataFrame/list/common OpenAlgo dict wrappers."""
    if isinstance(raw, pd.DataFrame):
        return raw.copy()
    if isinstance(raw, dict):
        for key in ("data", "history", "candles", "records"):
            if key in raw:
                inner = raw.get(key)
                if isinstance(inner, pd.DataFrame):
                    return inner.copy()
                if isinstance(inner, list):
                    return pd.DataFrame(inner)
                if isinstance(inner, dict):
                    try:
                        return pd.DataFrame(inner)
                    except Exception:
                        return pd.DataFrame([inner])
        try:
            return pd.DataFrame(raw)
        except Exception:
            return pd.DataFrame([raw])
    if isinstance(raw, (list, tuple)):
        return pd.DataFrame(list(raw))
    return pd.DataFrame()


def option_history(symbol, exchange):
    end = datetime.now().date()
    start = end - timedelta(days=2)
    raw = client.history(
        symbol=symbol,
        exchange=exchange,
        interval="1m",
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
    )
    df = normalize_history_frame(raw)
    if df.empty:
        raise RuntimeError(f"No option history for {exchange}:{symbol}")
    # normalize columns in case SDK casing varies
    df.columns = [str(c).strip().lower() for c in df.columns]
    for c in ["open", "high", "low", "close", "volume", "oi"]:
        if c not in df.columns:
            df[c] = np.nan if c == "oi" else 0.0
    for c in ["open", "high", "low", "close", "volume", "oi"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return df

# -------------------- Market-data helpers --------------------
def history(symbol, exchange):
    end = datetime.now().date()
    start = end - timedelta(days=CFG.lookback_days)
    return client.history(
        symbol=symbol,
        exchange=exchange,
        interval=CFG.timeframe,
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
    )

def quote(symbol, exchange):
    r = client.quotes(symbol=symbol, exchange=exchange)
    if not isinstance(r, dict) or r.get("status") != "success":
        raise RuntimeError(f"Quote failed for {exchange}:{symbol}: {r}")
    d = r.get("data") or {}
    return {k: float(d.get(k, 0) or 0) for k in
            ["ltp","bid","ask","open","high","low","prev_close","volume","oi"]}

def parse_expiry(v):
    if pd.isna(v) or not str(v).strip():
        return None
    s = str(v).strip().upper()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d%b%y", "%d%b%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None

def nearest_index_expiry():
    r = client.expiry(symbol=CFG.underlying, exchange=CFG.option_exchange,
                      instrumenttype="options")
    if not isinstance(r, dict) or r.get("status") != "success":
        raise RuntimeError(f"Expiry lookup failed: {r}")
    today = datetime.now().date()
    vals = []
    for raw in r.get("data", []):
        d = parse_expiry(raw)
        if d and d >= today:
            vals.append(d)
    if not vals:
        raise RuntimeError(f"No current {CFG.underlying} option expiry found")
    return min(vals).strftime("%d%b%y").upper()

# -------------------- MCX dynamic contract discovery --------------------
_mcx_cache = {"at": None, "df": None, "future": None}

def mcx_instruments():
    now = datetime.now()
    if (_mcx_cache["df"] is not None and _mcx_cache["at"] and
            (now - _mcx_cache["at"]).total_seconds() < 1800):
        return _mcx_cache["df"]
    df = client.instruments(exchange="MCX")
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df.get("data", df) if isinstance(df, dict) else df)
    if df.empty:
        raise RuntimeError("MCX instrument master is empty")
    _mcx_cache["df"], _mcx_cache["at"] = df.copy(), now
    return df

def nearest_mcx_future():
    if _mcx_cache["future"]:
        return _mcx_cache["future"]
    df = mcx_instruments().copy()
    for c in ["name","symbol","instrumenttype","expiry"]:
        if c not in df.columns:
            df[c] = ""
    mask = (
        df["name"].astype(str).str.upper().eq(CFG.underlying.upper())
        | df["symbol"].astype(str).str.upper().str.startswith(CFG.underlying.upper())
    )
    fut = df[mask & df["instrumenttype"].astype(str).str.upper().str.contains("FUT")].copy()
    fut["expdate"] = fut["expiry"].apply(parse_expiry)
    today = datetime.now().date()
    fut = fut[fut["expdate"].notna() & (fut["expdate"] >= today)].sort_values("expdate")
    if fut.empty:
        raise RuntimeError(f"No running MCX {CFG.underlying} future found")
    sym = str(fut.iloc[0]["symbol"])
    _mcx_cache["future"] = sym
    return sym

# -------------------- Signal engine --------------------
def closed_bar_frame(df):
    x = enrich(df)
    if len(x) < 40:
        raise RuntimeError(f"Insufficient candles: {len(x)}")
    # Treat final row as potentially forming; use -2 as latest closed candle.
    return x


def signal_from(df):
    """
    Strict hierarchical signal:
      Mandatory gates:
        - EMA9/21 regime and slope
        - EMA5/9 trigger and expanding gap
        - VWAP alignment
        - ADX threshold
        - ATR no-chase threshold
      Confirmation:
        - At least 2 of 3: structure, breakout, acceleration
    """
    x = closed_bar_frame(df)
    b = x.iloc[-2]
    p = x.iloc[-3]
    pp = x.iloc[-4]

    bull_regime = (
        b.ema9 > b.ema21
        and b.ema9 > p.ema9
        and b.ema21 >= p.ema21
    )
    bear_regime = (
        b.ema9 < b.ema21
        and b.ema9 < p.ema9
        and b.ema21 <= p.ema21
    )

    bull_trigger = (
        b.ema5 > b.ema9
        and b.gap59 > p.gap59
        and p.gap59 >= pp.gap59
    )
    bear_trigger = (
        b.ema5 < b.ema9
        and b.gap59 < p.gap59
        and p.gap59 <= pp.gap59
    )

    bull_vwap = b.close > b.vwap
    bear_vwap = b.close < b.vwap

    adx_ok = np.isfinite(b.adx14) and b.adx14 >= CFG.adx_min

    extension = abs(b.close - b.ema9) / max(float(b.atr14), 1e-9)
    atr_ok = np.isfinite(extension) and extension <= CFG.max_atr_extension

    # V3 regime-strength filter:
    # require EMA9/EMA21 separation of at least 0.10 ATR.
    # This reduced weak/flat-regime entries in the supplied historical datasets.
    regime_gap_atr = abs(b.ema9 - b.ema21) / max(float(b.atr14), 1e-9)
    regime_gap_ok = (
        np.isfinite(regime_gap_atr)
        and regime_gap_atr >= CFG.min_ema921_gap_atr
    )

    bull_structure = (
        x.iloc[-2].high > x.iloc[-3].high > x.iloc[-4].high
        and x.iloc[-2].low > x.iloc[-3].low > x.iloc[-4].low
    )
    bear_structure = (
        x.iloc[-2].high < x.iloc[-3].high < x.iloc[-4].high
        and x.iloc[-2].low < x.iloc[-3].low < x.iloc[-4].low
    )

    bull_breakout = b.close > x.iloc[-7:-2]["high"].max()
    bear_breakout = b.close < x.iloc[-7:-2]["low"].min()

    r1 = (b.close - p.close) / max(abs(p.close), 1e-9)
    r2 = (p.close - pp.close) / max(abs(pp.close), 1e-9)
    bull_accel = r1 > 0 and r1 >= r2
    bear_accel = r1 < 0 and r1 <= r2

    bull_confirms = int(bull_structure) + int(bull_breakout) + int(bull_accel)
    bear_confirms = int(bear_structure) + int(bear_breakout) + int(bear_accel)

    bull_mandatory = (
        bull_regime and bull_trigger and bull_vwap and adx_ok and atr_ok and regime_gap_ok
    )
    bear_mandatory = (
        bear_regime and bear_trigger and bear_vwap and adx_ok and atr_ok and regime_gap_ok
    )

    bull_valid = bull_mandatory and bull_confirms >= 2
    bear_valid = bear_mandatory and bear_confirms >= 2

    direction = "CE" if bull_valid and not bear_valid else "PE" if bear_valid and not bull_valid else None
    score = (5 + bull_confirms) if direction == "CE" else (5 + bear_confirms) if direction == "PE" else 0

    # Setup reset logic: after an entry, the same direction may not re-enter
    # until EMA5 first loses its trigger relationship to EMA9.
    # V4.2: true fast-EMA reset only.
    # V4.1 rearmed too easily when the EMA gap merely stopped expanding,
    # causing repeated same-direction entries inside one extended trend leg.
    reset_ce = b.ema5 <= b.ema9
    reset_pe = b.ema5 >= b.ema9

    bar_id = str(x.iloc[-2].get("datetime", x.index[-2]))
    metrics = {
        "close": float(b.close),
        "ema5": float(b.ema5), "ema9": float(b.ema9), "ema21": float(b.ema21),
        "adx": float(b.adx14), "atr": float(b.atr14), "vwap": float(b.vwap),
        "extension_atr": float(extension),
        "vwap_dist_atr": float(abs(float(b.close) - float(b.vwap)) / max(float(b.atr14), 1e-9)),
        "regime_gap_atr": float(regime_gap_atr),
        "bull_confirms": int(bull_confirms),
        "bear_confirms": int(bear_confirms),
        "bull_valid": bool(bull_valid), "bear_valid": bool(bear_valid),
        "reset_ce": bool(reset_ce), "reset_pe": bool(reset_pe),
        "bar_id": bar_id,
    }
    return direction, score, metrics

# -------------------- Option-selection engine --------------------
def option_quality(q):
    ltp, bid, ask = q["ltp"], q["bid"], q["ask"]
    if ltp <= 0:
        return -999, None
    spread = max(0.0, ask - bid) if ask > 0 and bid > 0 else None
    rel_spread = (spread / ltp) if spread is not None else None
    if rel_spread is not None and rel_spread > CFG.max_relative_spread:
        return -999, rel_spread
    s = 0
    if rel_spread is None or rel_spread <= CFG.max_relative_spread:
        s += 1
    if q["volume"] > 0:
        s += 1
    if q["prev_close"] > 0:
        if ltp >= q["prev_close"]:
            s += 1
    return s, rel_spread

def _safe_float(v, default=0.0):
    try:
        x = float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default


def _option_micro_metrics(q, atm_iv=0.0):
    ltp = max(_safe_float(q.get("ltp")), 0.0)
    bid = max(_safe_float(q.get("bid")), 0.0)
    ask = max(_safe_float(q.get("ask")), 0.0)
    bid_qty = max(_safe_float(q.get("bid_qty")), 0.0)
    ask_qty = max(_safe_float(q.get("ask_qty")), 0.0)

    spread_pct = None
    if ltp > 0 and bid > 0 and ask > 0 and ask >= bid:
        spread_pct = (ask - bid) / ltp

    delta = abs(_safe_float(q.get("delta")))
    gamma = abs(_safe_float(q.get("gamma")))
    theta = abs(_safe_float(q.get("theta")))
    vega = abs(_safe_float(q.get("vega")))
    iv = max(_safe_float(q.get("iv")), 0.0)

    iv_ratio = (iv / atm_iv) if iv > 0 and atm_iv > 0 else None
    theta_pct = (theta / ltp) if theta > 0 and ltp > 0 else None
    vega_pct = (vega / ltp) if vega > 0 and ltp > 0 else None
    gamma_50_delta = (gamma * 50.0 / delta) if gamma > 0 and delta > 0 else None
    depth_ratio = (bid_qty / ask_qty) if bid_qty > 0 and ask_qty > 0 else None

    score = 0.0
    reject = []

    # Executability / spread.
    if spread_pct is not None:
        if spread_pct <= 0.004:
            score += 2.0
        elif spread_pct <= 0.008:
            score += 1.0
        elif spread_pct > 0.012:
            reject.append(f"wide spread {spread_pct*100:.2f}%")
    else:
        score += 0.25  # fail-soft when analyzer/broker gives no B/A

    # Directional sensitivity.
    if 0.50 <= delta <= 0.75:
        score += 2.0
    elif 0.40 <= delta <= 0.85:
        score += 1.0
    elif delta > 0 and (delta < 0.30 or delta > 0.95):
        reject.append(f"delta unsuitable {delta:.2f}")

    # Relative IV sanity.
    if iv_ratio is not None:
        if 0.80 <= iv_ratio <= 1.15:
            score += 1.0
        elif iv_ratio > 1.35:
            reject.append(f"IV rich vs ATM {iv_ratio:.2f}x")

    # Time decay burden.
    if theta_pct is not None:
        if theta_pct <= 0.04:
            score += 1.0
        elif theta_pct > 0.12:
            reject.append(f"theta burden {theta_pct*100:.1f}%/day")

    # Gamma instability.
    if gamma_50_delta is not None:
        if gamma_50_delta <= 0.25:
            score += 1.0
        elif gamma_50_delta > 0.75:
            reject.append(f"gamma instability {gamma_50_delta:.2f}")

    # Vega exposure relative to premium.
    if vega_pct is not None:
        if vega_pct <= 0.12:
            score += 1.0
        elif vega_pct > 0.30:
            score -= 0.5

    # Top-of-book depth.
    if depth_ratio is not None:
        if depth_ratio >= 0.75:
            score += 1.0
        elif depth_ratio < 0.20 and (spread_pct or 0) > 0.005:
            reject.append(f"weak bid depth {depth_ratio:.2f}")

    if _safe_float(q.get("volume")) > 0:
        score += 0.5
    if _safe_float(q.get("oi")) > 0:
        score += 0.5

    return {
        "score": score,
        "reject": reject,
        "spread_pct": spread_pct,
        "delta": delta if delta > 0 else None,
        "gamma": gamma if gamma > 0 else None,
        "theta": theta if theta > 0 else None,
        "vega": vega if vega > 0 else None,
        "iv": iv if iv > 0 else None,
        "iv_ratio": iv_ratio,
        "theta_pct": theta_pct,
        "vega_pct": vega_pct,
        "gamma_50_delta": gamma_50_delta,
        "depth_ratio": depth_ratio,
    }


def select_index_option(option_type):
    expiry = nearest_index_expiry()
    try:
        chain = client.optionchain(
            underlying=CFG.underlying,
            exchange=CFG.underlying_exchange,
            expiry_date=expiry,
            strike_count=8,
            with_greeks=True,
        )
    except TypeError:
        chain = client.optionchain(
            underlying=CFG.underlying,
            exchange=CFG.underlying_exchange,
            expiry_date=expiry,
            strike_count=8,
        )

    if not isinstance(chain, dict) or chain.get("status") != "success":
        raise RuntimeError(f"Option chain failed: {chain}")

    key = option_type.lower()
    rows = chain.get("chain", [])

    # Same-side ATM IV reference.
    atm_iv = 0.0
    for row in rows:
        leg = row.get(key) or {}
        if leg.get("label") == "ATM":
            atm_iv = _safe_float(
                leg.get("implied_volatility", leg.get("iv", 0))
            )
            break

    candidates = []
    preference_bonus = {"ITM1": 1.0, "ITM2": 0.6, "ATM": 0.3}

    for row in rows:
        leg = row.get(key) or {}
        label = leg.get("label")
        if label not in CFG.preferred_offsets:
            continue

        sym = leg.get("symbol")
        if not sym:
            continue

        q = {
            "ltp": _safe_float(leg.get("ltp")),
            "bid": _safe_float(leg.get("bid")),
            "ask": _safe_float(leg.get("ask")),
            "bid_qty": _safe_float(leg.get("bid_qty")),
            "ask_qty": _safe_float(leg.get("ask_qty")),
            "volume": _safe_float(leg.get("volume")),
            "prev_close": _safe_float(leg.get("prev_close")),
            "oi": _safe_float(leg.get("oi")),
            "open": _safe_float(leg.get("open")),
            "high": _safe_float(leg.get("high")),
            "low": _safe_float(leg.get("low")),
            "delta": _safe_float(leg.get("delta")),
            "gamma": _safe_float(leg.get("gamma")),
            "theta": _safe_float(leg.get("theta")),
            "vega": _safe_float(leg.get("vega")),
            "iv": _safe_float(
                leg.get("implied_volatility", leg.get("iv", 0))
            ),
        }

        qual, rel = option_quality(q)
        if qual < 0:
            continue

        micro = _option_micro_metrics(q, atm_iv=atm_iv)

        if micro["reject"]:
            log(f"MICRO REJECT {sym} {label}: " + "; ".join(micro["reject"]))
            continue

        total_rank = (
            micro["score"]
            + preference_bonus.get(label, 0.0)
            + 0.25 * max(qual, 0)
        )

        candidates.append({
            "symbol": sym,
            "exchange": CFG.option_exchange,
            "lotsize": int(float(leg.get("lotsize", 1) or 1)),
            "label": label,
            "q": q,
            "quality": qual,
            "rel_spread": rel if rel is not None else 999,
            "micro": micro,
            "micro_rank": total_rank,
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda z: (-z["micro_rank"], z["rel_spread"], -z["q"]["volume"])
    )

    chosen = candidates[0]
    m = chosen["micro"]
    sp = "n/a" if m["spread_pct"] is None else f"{m['spread_pct']*100:.2f}%"
    ivr = "n/a" if m["iv_ratio"] is None else f"{m['iv_ratio']:.2f}x"
    th = "n/a" if m["theta_pct"] is None else f"{m['theta_pct']*100:.2f}%/d"
    gp = "n/a" if m["gamma_50_delta"] is None else f"{m['gamma_50_delta']:.2f}"
    vp = "n/a" if m["vega_pct"] is None else f"{m['vega_pct']*100:.2f}%"
    dp = "n/a" if m["depth_ratio"] is None else f"{m['depth_ratio']:.2f}"

    log(
        f"MICRO SELECT {chosen['symbol']} {chosen['label']} rank={chosen['micro_rank']:.2f} "
        f"spread={sp} delta={m['delta'] if m['delta'] is not None else 'n/a'} "
        f"IV={m['iv'] if m['iv'] is not None else 'n/a'} IV/ATM={ivr} "
        f"thetaBurden={th} gamma50/delta={gp} vega/premium={vp} depth={dp}"
    )
    return chosen

def select_mcx_option(option_type):
    fut = nearest_mcx_future()
    spot = quote(fut, "MCX")["ltp"]
    df = mcx_instruments().copy()
    for c in ["name","symbol","instrumenttype","expiry","strike","lotsize"]:
        if c not in df.columns:
            df[c] = np.nan if c in ["strike","lotsize"] else ""
    mask_name = (
        df["name"].astype(str).str.upper().eq(CFG.underlying.upper())
        | df["symbol"].astype(str).str.upper().str.startswith(CFG.underlying.upper())
    )
    suffix = option_type.upper()
    opt = df[mask_name & df["symbol"].astype(str).str.upper().str.endswith(suffix)].copy()
    opt["expdate"] = opt["expiry"].apply(parse_expiry)
    opt["strike_num"] = pd.to_numeric(opt["strike"], errors="coerce")
    today = datetime.now().date()
    opt = opt[opt["expdate"].notna() & (opt["expdate"] >= today) & opt["strike_num"].notna()]
    if opt.empty:
        raise RuntimeError(f"No running MCX {CFG.underlying} {option_type} options found")
    near_exp = opt["expdate"].min()
    opt = opt[opt["expdate"] == near_exp]
    if suffix == "CE":
        itm = opt[opt["strike_num"] <= spot].sort_values("strike_num", ascending=False).head(3)
    else:
        itm = opt[opt["strike_num"] >= spot].sort_values("strike_num", ascending=True).head(3)
    if itm.empty:
        itm = opt.iloc[(opt["strike_num"] - spot).abs().argsort()[:3]]
    candidates = []
    for _, row in itm.iterrows():
        sym = str(row["symbol"])
        q = quote(sym, "MCX")
        qual, rel = option_quality(q)
        if qual < 0:
            continue
        candidates.append({
            "symbol": sym, "exchange": "MCX",
            "lotsize": int(float(row.get("lotsize",1) or 1)),
            "label": "DYNAMIC_ITM",
            "q": q, "quality": qual,
            "rel_spread": rel if rel is not None else 999,
        })
    if not candidates:
        return None
    candidates.sort(key=lambda z: (-z["quality"], z["rel_spread"], -z["q"]["volume"]))
    return candidates[0]

def select_option(option_type):
    return select_mcx_option(option_type) if CFG.market == "MCX" else select_index_option(option_type)



# -------------------- V4.1 selected-option confirmation --------------------
def classify_oi_buildup(prev_close, close, prev_oi, oi):
    if not all(np.isfinite(v) for v in [prev_close, close, prev_oi, oi]):
        return "OI_NA"
    if prev_oi <= 0:
        return "OI_NA"
    price_up = close > prev_close
    oi_up = oi > prev_oi
    if price_up and oi_up:
        return "LONG_BUILDUP"
    if (not price_up) and oi_up:
        return "SHORT_BUILDUP"
    if price_up and (not oi_up):
        return "SHORT_COVERING"
    return "LONG_UNWINDING"


def option_confirmation(opt, underlying_score, is_reentry=False):
    """
    Score the selected option itself without replacing the original NIFTY signal.

    Maximum practical score:
      +2 fast EMA alignment + price above EMA9
      +1 EMA9 >= EMA21
      +1 RSI9 > 52 and rising (not excessively stretched)
      +1 favorable option OI/price behavior
      +1 volume >= recent average
      +1 good spread/liquidity
      +1 acceptable absolute delta, when supplied by option chain

    Fail-soft:
      Missing OI/Greeks do not reject a strong NIFTY setup.
    """
    result = {
        "score": 0, "required": 4, "ema_fast": False, "ema_regime": False,
        "rsi": None, "rsi_rising": False, "oi_state": "OI_NA",
        "volume_ratio": None, "delta": None, "history_ok": False,
        "option_extension_atr": None, "hard_reject": False,
        "micro_score": None, "spread_pct": None, "iv": None,
        "iv_ratio": None, "theta_pct": None, "vega_pct": None,
        "gamma_50_delta": None, "depth_ratio": None,
        "reason": "",
    }

    # Strong original NIFTY signal gets a lower confirmation threshold,
    # so V4.1 does not over-filter genuine moves.
    result["required"] = 3 if underlying_score >= 8 else 4
    if is_reentry:
        result["required"] = max(result["required"], 5)

    try:
        df = option_history(opt["symbol"], opt["exchange"])
        if len(df) < 25:
            result["reason"] = f"insufficient option candles={len(df)}"
        else:
            x = df.copy()
            x["ema5"] = ema(x["close"], 5)
            x["ema9"] = ema(x["close"], 9)
            x["ema21"] = ema(x["close"], 21)
            x["rsi9"] = rsi(x["close"], 9)

            # Final row can be forming; use latest completed candle.
            b = x.iloc[-2]
            p = x.iloc[-3]

            # For a long CE or long PE, the OPTION PREMIUM itself should be rising.
            fast_ok = (
                np.isfinite(b.ema5) and np.isfinite(b.ema9)
                and b.ema5 > b.ema9
                and b.close >= b.ema9
            )
            if fast_ok:
                result["score"] += 2
                result["ema_fast"] = True

            regime_ok = (
                np.isfinite(b.ema9) and np.isfinite(b.ema21)
                and b.ema9 >= b.ema21
            )
            if regime_ok:
                result["score"] += 1
                result["ema_regime"] = True

            rv = float(b.rsi9)
            rp = float(p.rsi9)
            result["rsi"] = rv
            result["rsi_rising"] = rv > rp

            x["atr14_opt"] = atr(x, 14)
            opt_atr = float(x.iloc[-2]["atr14_opt"])
            opt_ext = (
                float((b.close - b.ema9) / opt_atr)
                if np.isfinite(opt_atr) and opt_atr > 0 else 0.0
            )
            result["option_extension_atr"] = opt_ext

            # Momentum confirmation. Initial entries can tolerate stronger RSI,
            # but same-direction re-entries must not chase an already stretched premium.
            if rv > 52 and rv > rp and rv <= 82:
                result["score"] += 1

            if is_reentry:
                if rv > 72:
                    result["hard_reject"] = True
                    result["reason"] += f" reentry RSI too stretched ({rv:.1f}>72);"
                if not (rv > rp):
                    result["hard_reject"] = True
                    result["reason"] += " reentry RSI not rising;"
                if opt_ext > 1.50:
                    result["hard_reject"] = True
                    result["reason"] += f" option premium extended {opt_ext:.2f}ATR above EMA9;"

            prev_oi = float(p.oi) if np.isfinite(p.oi) else np.nan
            cur_oi = float(b.oi) if np.isfinite(b.oi) else np.nan
            oi_state = classify_oi_buildup(
                float(p.close), float(b.close), prev_oi, cur_oi
            )
            result["oi_state"] = oi_state
            # For a BUY option, premium-up + OI-up (long buildup) or
            # premium-up + OI-down (short covering) supports the entry.
            if oi_state in ("LONG_BUILDUP", "SHORT_COVERING"):
                result["score"] += 1

            recent_vol = pd.to_numeric(x["volume"].iloc[-12:-2], errors="coerce")
            avg_vol = float(recent_vol.mean()) if len(recent_vol) else 0.0
            vr = (float(b.volume) / avg_vol) if avg_vol > 0 else None
            result["volume_ratio"] = vr
            if vr is not None and vr >= 1.0:
                result["score"] += 1

            # If OI is unavailable, re-entry needs at least ordinary volume participation.
            if is_reentry and oi_state == "OI_NA" and vr is not None and vr < 0.80:
                result["hard_reject"] = True
                result["reason"] += f" OI unavailable and reentry volume weak ({vr:.2f}x);"

            result["history_ok"] = True
    except Exception as e:
        result["reason"] = f"option history unavailable: {e}"

    # Existing option-quality gate remains useful.
    if int(opt.get("quality", 0)) >= 2:
        result["score"] += 1

    micro = opt.get("micro") or _option_micro_metrics(opt.get("q") or {}, atm_iv=0.0)
    result["micro_score"] = micro.get("score")
    result["spread_pct"] = micro.get("spread_pct")
    result["delta"] = micro.get("delta")
    result["iv"] = micro.get("iv")
    result["iv_ratio"] = micro.get("iv_ratio")
    result["theta_pct"] = micro.get("theta_pct")
    result["vega_pct"] = micro.get("vega_pct")
    result["gamma_50_delta"] = micro.get("gamma_50_delta")
    result["depth_ratio"] = micro.get("depth_ratio")

    delta = result["delta"] or 0.0
    if 0.50 <= delta <= 0.75:
        result["score"] += 2
    elif 0.40 <= delta <= 0.85:
        result["score"] += 1

    if (result["micro_score"] or 0) >= 5:
        result["score"] += 1

    # Microstructure / Greeks sanity guard.
    if result["spread_pct"] is not None and result["spread_pct"] > 0.012:
        result["hard_reject"] = True
        result["reason"] += f" spread too wide {result['spread_pct']*100:.2f}%;"

    if result["theta_pct"] is not None and result["theta_pct"] > 0.12:
        result["hard_reject"] = True
        result["reason"] += f" theta burden too high {result['theta_pct']*100:.1f}%/day;"

    if is_reentry:
        if result["spread_pct"] is not None and result["spread_pct"] > 0.008:
            result["hard_reject"] = True
            result["reason"] += f" reentry spread {result['spread_pct']*100:.2f}% > 0.80%;"
        if result["iv_ratio"] is not None and result["iv_ratio"] > 1.25:
            result["hard_reject"] = True
            result["reason"] += f" reentry IV rich {result['iv_ratio']:.2f}x ATM;"
        if result["theta_pct"] is not None and result["theta_pct"] > 0.08:
            result["hard_reject"] = True
            result["reason"] += f" reentry theta burden {result['theta_pct']*100:.1f}%/day;"
        if result["depth_ratio"] is not None and result["depth_ratio"] < 0.35:
            result["hard_reject"] = True
            result["reason"] += f" reentry weak bid depth {result['depth_ratio']:.2f};"

    # Fail-soft safeguard:
    # if option candle/OI history is temporarily unavailable, do not suppress
    # an otherwise very strong original NIFTY setup just because an API failed.
    if not result["history_ok"]:
        if underlying_score >= 8 and int(opt.get("quality", 0)) >= 2:
            result["required"] = min(result["required"], result["score"])
            result["reason"] = (
                result["reason"] + " | STRONG-NIFTY FALLBACK: "
                "history unavailable, liquidity/quality fallback accepted"
            ).strip(" |")
        else:
            result["required"] = min(result["required"], 2)

    return result

# -------------------- V4 broker reconciliation / square-off safety --------------------
def actual_open_qty(symbol, exchange):
    """Return strategy-level open quantity for this exact symbol; None if status is unknown."""
    try:
        r = client.openposition(
            strategy=CFG.strategy,
            symbol=symbol,
            exchange=exchange,
            product=CFG.product,
        )
        if isinstance(r, dict) and r.get("status") == "success":
            return int(float(r.get("quantity", 0) or 0))
        log(f"OPENPOSITION unavailable for {exchange}:{symbol}: {r}")
    except Exception as e:
        log(f"OPENPOSITION error for {exchange}:{symbol}: {e}")
    return None


def reconcile_tracked_position():
    """
    Reconcile local state with OpenAlgo/broker state every loop.
    Manual exit: local position exists but actual strategy quantity is zero -> clear local state.
    Quantity mismatch: synchronize tracked quantity downward/upward to actual quantity.
    This does NOT alter signal generation.
    """
    pos = state.get("position")
    if not pos:
        return True

    actual = actual_open_qty(pos["symbol"], pos["exchange"])
    if actual is None:
        # Unknown is not treated as flat. Fail safe and keep local state.
        return False

    if actual == 0:
        log(f"MANUAL/EXTERNAL EXIT DETECTED: {pos['symbol']} is flat at broker/OpenAlgo. Clearing local state.")
        old_type = pos.get("option_type")
        state["position"] = None
        # Do not immediately re-enter the exact same stale setup after a manual exit.
        # A normal EMA5/EMA9 trigger reset will re-arm it, preserving V3 signal quality.
        if old_type == "CE":
            state["armed_ce"] = False
        elif old_type == "PE":
            state["armed_pe"] = False
        save_state(state)
        return True

    if abs(actual) != int(pos.get("qty", 0)):
        log(f"POSITION QTY RECONCILE {pos['symbol']}: local={pos.get('qty')} actual={actual}")
        pos["qty"] = abs(actual)
        state["position"] = pos
        save_state(state)
    return True


def wait_until_flat(symbol, exchange, timeout_seconds=12, poll=1.0):
    """Confirm actual strategy position is flat before allowing an opposite/new position."""
    deadline = time.time() + timeout_seconds
    last = None
    while time.time() < deadline:
        last = actual_open_qty(symbol, exchange)
        if last == 0:
            return True
        if last is None:
            time.sleep(poll)
            continue
        time.sleep(poll)
    log(f"FLAT CONFIRMATION TIMEOUT {exchange}:{symbol}; last_qty={last}")
    return False


def cancel_strategy_orders():
    try:
        r = client.cancelallorder(strategy=CFG.strategy)
        if isinstance(r, dict) and r.get("status") == "success":
            log(f"Pending strategy orders cancelled: {r.get('message', 'success')}")
            return True
        log(f"Cancel-all returned: {r}")
    except Exception as e:
        log(f"Cancel-all error: {e}")
    return False


def strategy_failsafe_close(reason):
    """End-of-day/shutdown failsafe: cancel pending orders and ask OpenAlgo to close strategy positions."""
    cancel_strategy_orders()
    try:
        r = client.closeposition(strategy=CFG.strategy)
        log(f"STRATEGY CLOSEPOSITION ({reason}): {r}")
        return isinstance(r, dict) and r.get("status") == "success"
    except Exception as e:
        log(f"STRATEGY CLOSEPOSITION error ({reason}): {e}")
        return False

# -------------------- Quantity / orders --------------------
def dynamic_quantity(ltp, lot_size):
    if ltp <= 0 or lot_size <= 0:
        return 0
    budget = min(CFG.capital, CFG.capital_per_trade)
    one_lot = ltp * lot_size
    lots = max(0, int(budget // one_lot))
    return lots * lot_size

def analyzer_mode_on():
    try:
        r = client.analyzerstatus()
        text = json.dumps(r).lower()
        return ("true" in text or "analyze" in text or '"mode": "analyzer"' in text
                or '"mode":"analyzer"' in text)
    except Exception:
        return False

def order_allowed():
    if ALLOW_LIVE_TRADING:
        return True
    if analyzer_mode_on():
        return True
    log("ORDER BLOCKED: Analyzer Mode not confirmed and ALLOW_LIVE_TRADING=false.")
    return False

def executed_price(order_id, fallback_symbol, fallback_exchange):
    if order_id:
        for _ in range(4):
            try:
                time.sleep(1)
                r = client.orderstatus(order_id=order_id, strategy=CFG.strategy)
                if isinstance(r, dict) and r.get("status") == "success":
                    d = r.get("data", {})
                    px = float(d.get("average_price", 0) or 0)
                    if px > 0:
                        return px
            except Exception:
                pass
    return quote(fallback_symbol, fallback_exchange)["ltp"]

def place_buy(opt):
    qty = dynamic_quantity(opt["q"]["ltp"], opt["lotsize"])
    if qty <= 0:
        log(f"SKIP: budget cannot buy one lot of {opt['symbol']}")
        return False
    if not order_allowed():
        return False
    r = client.placeorder(
        strategy=CFG.strategy,
        symbol=opt["symbol"],
        action="BUY",
        exchange=opt["exchange"],
        price_type="MARKET",
        product=CFG.product,
        quantity=qty,
    )
    if not isinstance(r, dict) or r.get("status") != "success":
        log(f"ENTRY ORDER FAILED: {r}")
        return False
    px = executed_price(r.get("orderid"), opt["symbol"], opt["exchange"])
    pos = {
        "symbol": opt["symbol"], "exchange": opt["exchange"],
        "option_type": "CE" if opt["symbol"].upper().endswith("CE") else "PE",
        "qty": qty, "entry": px, "lotsize": opt["lotsize"],
        # Software pre-trigger at -9% aims to keep actual market fill near -10%.
        "hard_stop": px * 0.91,
        "max_ltp": px, "lock_price": None,
        "effective_stop": px * 0.91,
        "entered_at": datetime.now().isoformat(),
    }
    state["position"] = pos
    save_state(state)
    log(
        f"BUY {opt['symbol']} qty={qty} @ ~{px:.2f} | "
        f"INITIAL SOFTWARE STOP={pos['hard_stop']:.2f} (-9% trigger, target <=10% fill)"
    )
    return True

def exit_position(reason):
    pos = state.get("position")
    if not pos:
        return True
    if not order_allowed():
        log(f"EXIT REQUIRED but order blocked: {reason}")
        return False

    # Reconcile immediately before exit. If user already exited manually, do not send a new SELL.
    actual = actual_open_qty(pos["symbol"], pos["exchange"])
    if actual == 0:
        log(f"EXIT SKIPPED ({reason}): {pos['symbol']} already flat (manual/external exit).")
        state["position"] = None
        save_state(state)
        return True
    if actual is None:
        log(f"EXIT DEFERRED ({reason}): could not confirm actual broker quantity.")
        return False

    qty_to_close = abs(int(actual))
    if qty_to_close <= 0:
        return False

    # For the long-option architecture, positive actual qty is closed with SELL.
    # If a negative quantity is ever detected, BUY it back rather than increasing the short.
    action = "SELL" if actual > 0 else "BUY"
    r = client.placeorder(
        strategy=CFG.strategy,
        symbol=pos["symbol"],
        action=action,
        exchange=pos["exchange"],
        price_type="MARKET",
        product=CFG.product,
        quantity=qty_to_close,
    )
    if not isinstance(r, dict) or r.get("status") != "success":
        log(f"EXIT ORDER FAILED ({reason}): {r}")
        return False

    px = executed_price(r.get("orderid"), pos["symbol"], pos["exchange"])
    # Do not clear state until OpenAlgo/broker confirms flat.
    if not wait_until_flat(pos["symbol"], pos["exchange"]):
        log(f"EXIT SENT but NOT YET CONFIRMED FLAT ({reason}); local state retained.")
        return False

    # P&L display assumes original architecture is long options.
    pnl = (px - float(pos["entry"])) * int(pos["qty"])
    pct = (px / float(pos["entry"]) - 1) * 100 if float(pos["entry"]) else 0.0
    log(f"EXIT {pos['symbol']} @ ~{px:.2f} | {reason} | P&L approx Rs {pnl:.2f} ({pct:+.2f}%) | FLAT CONFIRMED")
    state["last_exit_side"] = pos.get("option_type")
    state["last_exit_ts"] = datetime.now().isoformat()
    state["last_exit_pnl"] = float(pnl)
    state["position"] = None
    save_state(state)
    return True

# -------------------- Position manager --------------------
def manage_position(underlying_direction=None, underlying_score=0):
    pos = state.get("position")
    if not pos:
        return
    try:
        q = quote(pos["symbol"], pos["exchange"])
        ltp = q["ltp"]
        entry = float(pos["entry"])
        pos["max_ltp"] = max(float(pos.get("max_ltp", entry)), ltp)

        peak_gain = pos["max_ltp"] / entry - 1.0

        # Base protection:
        # 1) software exit trigger at -9% (to reduce slippage beyond the intended -10%)
        # 2) trailing 10% below the best option premium seen since entry
        base_stop = entry * 0.91
        trailing_10 = pos["max_ltp"] * 0.90
        effective_stop = max(base_stop, trailing_10)
        ladder_name = "10% TRAIL"

        # Adaptive profit/loss ladder. These floors are based on the best gain reached.
        if peak_gain >= 0.20:
            steps = math.floor((peak_gain - 0.20) / 0.05)
            locked_gain = 0.15 + steps * 0.05
            effective_stop = max(effective_stop, entry * (1 + locked_gain))
            ladder_name = f"PROFIT LOCK +{locked_gain*100:.0f}%"
        elif peak_gain >= 0.15:
            effective_stop = max(effective_stop, entry * 1.10)
            ladder_name = "LADDER +10%"
        elif peak_gain >= 0.10:
            effective_stop = max(effective_stop, entry * 1.05)
            ladder_name = "LADDER +5%"
        elif peak_gain >= 0.08:
            effective_stop = max(effective_stop, entry * 1.03)
            ladder_name = "LADDER +3%"
        elif peak_gain >= 0.05:
            effective_stop = max(effective_stop, entry)
            ladder_name = "BREAKEVEN FLOOR"
        elif peak_gain >= 0.02:
            effective_stop = max(effective_stop, entry * 0.96)
            ladder_name = "RECOVERY FLOOR -4%"

        pos["effective_stop"] = effective_stop
        pos["lock_price"] = effective_stop

        if ltp <= effective_stop:
            state["position"] = pos
            save_state(state)
            reason = (
                "INITIAL/HARD LOSS PROTECTION"
                if peak_gain < 0.02
                else f"ADAPTIVE TRAIL ({ladder_name})"
            )
            exit_position(reason)
            return

        # Directional reversal: preserve existing V3/V4 behavior.
        if underlying_direction and underlying_score >= max(7, CFG.score_threshold - 3):
            if pos["option_type"] != underlying_direction:
                state["position"] = pos
                save_state(state)
                exit_position("UNDERLYING REGIME REVERSAL")
                return

        state["position"] = pos
        save_state(state)

        gain = (ltp / entry - 1) * 100
        peak_pct = peak_gain * 100
        log(
            f"MONITOR {pos['symbol']} LTP={ltp:.2f} P&L={gain:+.2f}% "
            f"PEAK={peak_pct:+.2f}% STOP={effective_stop:.2f} {ladder_name}"
        )
    except Exception as e:
        log(f"Position monitor error: {e}")

# -------------------- Trading clock --------------------
def now_t():
    return datetime.now().time().replace(microsecond=0)

def entry_window():
    t = now_t()
    return CFG.entry_start <= t < CFG.last_entry

def force_exit_due():
    return now_t() >= CFG.force_exit

# -------------------- Main loop --------------------
def underlying_contract():
    if CFG.market == "MCX":
        return nearest_mcx_future(), "MCX"
    return CFG.underlying, CFG.underlying_exchange

def main():
    log("=" * 80)
    log(f"START {CFG.strategy} | market={CFG.market} timeframe={CFG.timeframe}")
    log(f"Capital=Rs {CFG.capital:,.0f} | per-trade cap=Rs {CFG.capital_per_trade:,.0f}")
    log(f"EMA regime 9/21 + trigger 5/9 | ADX>={CFG.adx_min} | "
        f"ATR extension<={CFG.max_atr_extension}")
    log(f"Safe gate: ALLOW_LIVE_TRADING={ALLOW_LIVE_TRADING}; Analyzer Mode is accepted.")
    log("V4.3.1: V3/V4 NIFTY signal + V4.2 re-entry guard retained.")
    log("V4.3.1 option ranking uses spread/depth + IV/ATM + delta/gamma/theta/vega + volume/OI.")
    log("Re-entry guard: true EMA5/9 reset + 5m cooldown + max 12ATR VWAP displacement + option no-chase checks.")
    log("Risk: -9% software trigger + 10% trailing loss + adaptive +2/+5/+8/+10/+15/+20 profit ladder.")
    log("=" * 80)

    eod_cleanup_done = False

    while True:
        try:
            if force_exit_due():
                if not eod_cleanup_done:
                    # Run end-of-day cleanup exactly once.
                    cancel_strategy_orders()
                    reconcile_tracked_position()
                    if state.get("position"):
                        exit_position("FORCE EXIT TIME")
                    strategy_failsafe_close("FORCE EXIT TIME")
                    reconcile_tracked_position()
                    eod_cleanup_done = True
                    log("EOD CLEANUP COMPLETE: no further entries; cleanup will not repeat.")
                time.sleep(30)
                continue

            sym, exch = underlying_contract()
            df = history(sym, exch)
            direction, score, m = signal_from(df)

            # Rearm a direction only after its fast trigger has reset.
            if m.get("reset_ce"):
                state["armed_ce"] = True
            if m.get("reset_pe"):
                state["armed_pe"] = True
            save_state(state)

            log(
                f"{sym} close={m['close']:.2f} E5={m['ema5']:.2f} "
                f"E9={m['ema9']:.2f} E21={m['ema21']:.2f} "
                f"ADX={m['adx']:.1f} ATR={m['atr']:.2f} VWAP={m['vwap']:.2f} "
                f"ext={m.get('extension_atr', 0.0):.2f}ATR vwapDist={m.get('vwap_dist_atr', 0.0):.2f}ATR "
                f"gap921={m['regime_gap_atr']:.2f}ATR "
                f"confirm CE/PE={m['bull_confirms']}/{m['bear_confirms']} "
                f"armed CE/PE={state.get('armed_ce', True)}/{state.get('armed_pe', True)} "
                f"dir={direction}"
            )

            # V4 safety: reconcile broker/OpenAlgo state before managing or entering.
            reconcile_tracked_position()

            # Manage existing position first. V3 signal logic is unchanged.
            manage_position(direction, score)

            if state.get("position"):
                time.sleep(CFG.poll_seconds)
                continue

            if not entry_window():
                time.sleep(CFG.poll_seconds)
                continue

            if not direction:
                time.sleep(CFG.poll_seconds)
                continue

            # One entry per setup. Same-direction re-entry needs a trigger reset first.
            if direction == "CE" and not state.get("armed_ce", True):
                time.sleep(CFG.poll_seconds)
                continue
            if direction == "PE" and not state.get("armed_pe", True):
                time.sleep(CFG.poll_seconds)
                continue

            # Prevent repeated entry on the same completed bar.
            if state.get("last_entry_bar") == m["bar_id"]:
                time.sleep(CFG.poll_seconds)
                continue

            # V4.2 same-direction re-entry protection.
            is_reentry = state.get("last_exit_side") == direction
            if is_reentry:
                # Short cooldown prevents immediate churn after a stop/trail exit.
                try:
                    last_exit_dt = datetime.fromisoformat(state.get("last_exit_ts")) if state.get("last_exit_ts") else None
                except Exception:
                    last_exit_dt = None
                if last_exit_dt and (datetime.now() - last_exit_dt).total_seconds() < 300:
                    log("SKIP REENTRY: 5-minute cooldown after previous same-direction exit.")
                    time.sleep(CFG.poll_seconds)
                    continue

                # Trend-exhaustion guard: V4.1 repeatedly bought PEs after NIFTY
                # was already 15-30 ATR below VWAP. Preserve the first trend entry,
                # but don't chase a same-direction re-entry when session displacement
                # is already extreme.
                if m.get("vwap_dist_atr", 0.0) > 12.0:
                    log(
                        f"SKIP REENTRY: NIFTY is {m.get('vwap_dist_atr', 0.0):.2f} ATR from VWAP "
                        f"(max 12 ATR for same-direction re-entry)."
                    )
                    time.sleep(CFG.poll_seconds)
                    continue

            opt = select_option(direction)
            if not opt:
                log("No acceptable liquid option candidate found.")
                time.sleep(CFG.poll_seconds)
                continue

            # Add up to 3 points from option quality to the underlying score.
            total_score = score + int(opt["quality"])
            rel = opt["rel_spread"]
            spread_txt = "n/a" if rel == 999 else f"{rel*100:.2f}%"
            log(
                f"CANDIDATE {opt['symbol']} {opt['label']} LTP={opt['q']['ltp']:.2f} "
                f"spread={spread_txt} vol={opt['q']['volume']:.0f} "
                f"quality={opt['quality']} total_score={total_score}"
            )

            # V4.1 selected-option confirmation layer.
            oc = option_confirmation(opt, score, is_reentry=is_reentry)
            rsi_txt = "n/a" if oc["rsi"] is None else f"{oc['rsi']:.1f}"
            vr_txt = "n/a" if oc["volume_ratio"] is None else f"{oc['volume_ratio']:.2f}x"
            delta_txt = "n/a" if oc["delta"] is None else f"{oc['delta']:.2f}"
            opt_ext_txt = "n/a" if oc["option_extension_atr"] is None else f"{oc['option_extension_atr']:.2f}ATR"
            iv_txt = "n/a" if oc["iv"] is None else f"{oc['iv']:.1f}"
            theta_txt = "n/a" if oc["theta_pct"] is None else f"{oc['theta_pct']*100:.1f}%"
            vega_txt = "n/a" if oc["vega_pct"] is None else f"{oc['vega_pct']*100:.1f}%"
            gamma_txt = "n/a" if oc["gamma_50_delta"] is None else f"{oc['gamma_50_delta']:.2f}"
            depth_txt = "n/a" if oc["depth_ratio"] is None else f"{oc['depth_ratio']:.2f}"
            log(
                f"OPTION CONFIRM {opt['symbol']} score={oc['score']}/{oc['required']} "
                f"EMAfast={oc['ema_fast']} EMAregime={oc['ema_regime']} "
                f"RSI9={rsi_txt} rising={oc['rsi_rising']} "
                f"OI={oc['oi_state']} VOL={vr_txt} DELTA={delta_txt} "
                f"IV={iv_txt} THETA={theta_txt} VEGA={vega_txt} "
                f"GAMMA50={gamma_txt} DEPTH={depth_txt} "
                f"OPT_EXT={opt_ext_txt} REENTRY={is_reentry}"
            )

            if oc.get("hard_reject"):
                log(f"SKIP: option re-entry no-chase rule. {oc.get('reason','')}")
                time.sleep(CFG.poll_seconds)
                continue

            if oc["score"] < oc["required"]:
                log(
                    f"SKIP: selected option confirmation insufficient "
                    f"({oc['score']} < {oc['required']}). "
                    f"{oc.get('reason','')}"
                )
                time.sleep(CFG.poll_seconds)
                continue

            # Final V4 safety gate: reconcile once more immediately before a new BUY.
            reconcile_tracked_position()
            if state.get("position"):
                log("NEW ENTRY BLOCKED: an existing strategy position is still open/not flat-confirmed.")
                time.sleep(CFG.poll_seconds)
                continue

            if place_buy(opt):
                state["last_entry_bar"] = m["bar_id"]
                if direction == "CE":
                    state["armed_ce"] = False
                else:
                    state["armed_pe"] = False
                save_state(state)

            time.sleep(CFG.poll_seconds)

        except KeyboardInterrupt:
            log("CTRL+C received. Cancelling pending orders and reconciling before shutdown.")
            cancel_strategy_orders()
            reconcile_tracked_position()
            if state.get("position"):
                exit_position("MANUAL SHUTDOWN")
            strategy_failsafe_close("MANUAL SHUTDOWN")
            reconcile_tracked_position()
            break
        except Exception as e:
            log(f"LOOP ERROR: {e}")
            log(traceback.format_exc().splitlines()[-1])
            time.sleep(max(5, CFG.poll_seconds))

if __name__ == "__main__":
    main()
