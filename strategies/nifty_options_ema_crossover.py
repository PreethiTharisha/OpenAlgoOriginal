"""EMA crossover options strategy for Nifty 50 using OpenAlgo APIs."""

import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Literal

import pandas as pd
from openalgo import api

from services.option_symbol_service import get_option_symbol
from utils.logging import get_logger


PositionDirection = Literal["CALL", "PUT"]


class NiftyOptionsEMACrossoverStrategy:
    """Encapsulates the EMA crossover strategy for Nifty options."""

    def __init__(self) -> None:
        self.logger = get_logger(self.__class__.__name__)
        self.api_key = os.getenv("BROKER_API_SECRET") or os.getenv("OPENALGO_API_KEY")
        if not self.api_key:
            error_msg = "BROKER_API_SECRET/OPENALGO_API_KEY must be provided via .env"
            self.logger.error(error_msg)
            raise EnvironmentError(error_msg)

        self.host = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
        self.ws_url = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")
        self.client = api(api_key=self.api_key, host=self.host, ws_url=self.ws_url)

        self.strategy_name = os.getenv("STRATEGY_NAME", "NIFTY_EMA_OPTIONS")
        self.underlying_symbol = os.getenv("UNDERLYING_SYMBOL", "NIFTY")
        self.underlying_exchange = os.getenv("UNDERLYING_EXCHANGE", "NSE_INDEX")
        self.options_exchange = os.getenv("OPTIONS_EXCHANGE", "NFO")

        self.candle_interval = os.getenv("CANDLE_TIMEFRAME", "5m")
        self.lookback_days = int(os.getenv("LOOKBACK_DAYS", "3"))
        self.poll_interval_seconds = int(os.getenv("SIGNAL_REFRESH_SECONDS", str(5 * 60)))
        self.entry_offset = os.getenv("ENTRY_OFFSET", "ITM2").upper()
        self.trade_lot_multiplier = max(1, int(os.getenv("TRADE_LOT_MULTIPLIER", "1")))
        self.product_type = os.getenv("PRODUCT_TYPE", "NRML")
        self.use_long_ema = os.getenv("USE_EMA_200_CONFIRMATION", "True").lower() in ["1", "true", "yes"]

        self.ema_periods = [5, 20, 40, 60]
        if self.use_long_ema:
            self.ema_periods.append(200)
        self.min_candles = max(max(self.ema_periods) * 3, 120)

        self.latest_underlying_ltp: Optional[float] = None
        self.last_processed_timestamp: Optional[pd.Timestamp] = None

        self.position_state: Optional[PositionDirection] = None
        self.active_contract: Dict[str, Any] = {}
        self.running = True

        self.logger.info(
            "Initialized NiftyOptionsEMACrossoverStrategy (interval=%s, lookback=%sdays, offset=%s)",
            self.candle_interval,
            self.lookback_days,
            self.entry_offset,
        )

    # ------------------------------------------------------------------
    # Market data helpers
    # ------------------------------------------------------------------
    def fetch_market_data(self) -> Optional[pd.DataFrame]:
        """Fetch recent candles and return only fully closed candles."""
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=self.lookback_days)

            self.logger.debug(
                "Fetching candles from %s to %s", start_date.date(), end_date.date()
            )

            df = self.client.history(
                symbol=self.underlying_symbol,
                exchange=self.underlying_exchange,
                interval=self.candle_interval,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
            )

            if df is None or df.empty:
                self.logger.warning("History API returned no candles")
                return None

            candle_time = self._build_candle_time(df)
            if candle_time is None:
                self.logger.error("Could not locate timestamp information for candles")
                return None

            df = df.assign(candle_time=candle_time)
            df = df.drop_duplicates(subset=["candle_time"])
            df = df.sort_values("candle_time").reset_index(drop=True)

            if len(df) <= 1:
                self.logger.warning("Not enough candles after sorting (got %d)", len(df))
                return None

            df = df.iloc[:-1].copy()  # Drop current forming candle
            df = df.dropna(subset=["close"])
            if len(df) < self.min_candles:
                self.logger.warning(
                    "Need at least %d candles for EMAs, got %d", self.min_candles, len(df)
                )
                return None

            df["close"] = df["close"].astype(float)
            last_time = df["candle_time"].iloc[-1]

            if self.last_processed_timestamp and last_time <= self.last_processed_timestamp:
                self.logger.debug("No new candle to act on (last=%s)", last_time)
                return None

            self.last_processed_timestamp = last_time
            self.latest_underlying_ltp = df["close"].iloc[-1]

            return df

        except Exception as exc:
            self.logger.exception("Failed to fetch market data: %s", exc)
            return None

    def _build_candle_time(self, df: pd.DataFrame) -> Optional[pd.Series]:
        """Return a pd.Series of candle timestamps (columns or index)."""
        for candidate in ["datetime", "timestamp", "date", "time"]:
            if candidate in df.columns:
                return pd.to_datetime(df[candidate])

        if df.index.dtype.kind in "Mm":
            return pd.to_datetime(df.index)

        return None

    # ------------------------------------------------------------------
    # Indicator helpers
    # ------------------------------------------------------------------
    def calculate_emas(self, candles: pd.DataFrame) -> Optional[pd.DataFrame]:
        """Append EMA columns to market data and drop incomplete rows."""
        if candles is None or candles.empty:
            return None

        ema_df = candles.copy()
        close = ema_df["close"].astype(float)

        for period in self.ema_periods:
            ema_df[f"ema_{period}"] = close.ewm(span=period, adjust=False).mean()

        ema_df = ema_df.dropna().reset_index(drop=True)
        if ema_df.shape[0] < 3:
            self.logger.debug("Not enough EMA rows after dropna (%d)", ema_df.shape[0])
            return None

        return ema_df

    # ------------------------------------------------------------------
    # Signal helpers
    # ------------------------------------------------------------------
    def check_entry_conditions(self, ema_df: pd.DataFrame) -> Optional[PositionDirection]:
        """Check if EMA relationship satisfies an entry signal."""
        if self.position_state is not None:
            return None

        latest = ema_df.iloc[-1]
        previous = ema_df.iloc[-2]

        above_all = (
            latest["ema_5"] > latest["ema_20"]
            and latest["ema_5"] > latest["ema_40"]
            and latest["ema_5"] > latest["ema_60"]
        )
        trending_above = above_all and (not self.use_long_ema or latest["ema_5"] > latest["ema_200"])
        maintaining_above = (
            previous["ema_5"] > previous["ema_20"]
            and previous["ema_5"] > previous["ema_40"]
            and previous["ema_5"] > previous["ema_60"]
        )

        if trending_above and maintaining_above:
            self.logger.info("Bullish EMA structure detected (CALL entry)")
            return "CALL"

        below_all = (
            latest["ema_5"] < latest["ema_20"]
            and latest["ema_5"] < latest["ema_40"]
            and latest["ema_5"] < latest["ema_60"]
        )
        trending_below = below_all and (not self.use_long_ema or latest["ema_5"] < latest["ema_200"])
        maintaining_below = (
            previous["ema_5"] < previous["ema_20"]
            and previous["ema_5"] < previous["ema_40"]
            and previous["ema_5"] < previous["ema_60"]
        )

        if trending_below and maintaining_below:
            self.logger.info("Bearish EMA structure detected (PUT entry)")
            return "PUT"

        return None

    def check_exit_conditions(self, ema_df: pd.DataFrame) -> Optional[PositionDirection]:
        """Detect EMA5 cross against EMA20 to trigger exit."""
        if self.position_state is None:
            return None

        latest = ema_df.iloc[-1]
        previous = ema_df.iloc[-2]

        if self.position_state == "CALL":
            if previous["ema_5"] >= previous["ema_20"] and latest["ema_5"] < latest["ema_20"]:
                self.logger.info("EMA5 crossed below EMA20 -> exiting CALL")
                return "CALL"
        else:
            if previous["ema_5"] <= previous["ema_20"] and latest["ema_5"] > latest["ema_20"]:
                self.logger.info("EMA5 crossed above EMA20 -> exiting PUT")
                return "PUT"

        return None

    # ------------------------------------------------------------------
    # Contract selection and order helpers
    # ------------------------------------------------------------------
    def select_option_contract(self, direction: PositionDirection, expiry: str) -> Optional[Dict[str, Any]]:
        """Resolve the actual option symbol that is two strikes ITM."""
        option_type = "CE" if direction == "CALL" else "PE"

        success, data, _ = get_option_symbol(
            underlying=self.underlying_symbol,
            exchange=self.underlying_exchange,
            expiry_date=expiry,
            strike_int=None,
            offset=self.entry_offset,
            option_type=option_type,
            api_key=self.api_key,
            underlying_ltp=self.latest_underlying_ltp,
        )

        if not success or not data:
            self.logger.error("Failed to resolve %s contract: %s", direction, data)
            return None

        return data

    def _get_nearest_expiry(self) -> Optional[str]:
        """Fetch nearest expiry string from the API."""
        try:
            response = self.client.expiry(
                symbol=self.underlying_symbol,
                exchange=self.options_exchange,
                instrumenttype="options",
                strike_count=8,
            )
            if response.get("status") == "success":
                expiries = response.get("data", [])
                if expiries:
                    return expiries[0]
            self.logger.warning("Expiry API did not provide data: %s", response)
        except Exception as exc:
            self.logger.exception("Expiry lookup failed: %s", exc)
        return None

    def place_order(self, symbol: str, exchange: str, action: str, quantity: int, reason: str) -> Optional[Dict[str, Any]]:
        """Place a single-market order (entry or exit)."""
        try:
            self.logger.info("Placing %s order for %s qty=%s (reason=%s)", action, symbol, quantity, reason)
            response = self.client.placeorder(
                strategy=self.strategy_name,
                symbol=symbol,
                exchange=exchange,
                action=action,
                quantity=str(quantity),
                price_type="MARKET",
                product=self.product_type,
                disclosed_quantity="0",
            )

            if response.get("status") == "success":
                self.logger.info("Order accepted: %s", response.get("orderid"))
                return response

            self.logger.error("Order failed: %s", response)
        except Exception as exc:
            self.logger.exception("Order placement error: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def _reset_position(self) -> None:
        self.position_state = None
        self.active_contract = {}

    def initiate_trade(self, direction: PositionDirection) -> None:
        """Attempt to enter a new position in the specified direction."""
        if self.position_state:
            return

        expiry = self._get_nearest_expiry()
        if not expiry:
            self.logger.warning("Skipping entry because expiry could not be resolved")
            return

        contract = self.select_option_contract(direction, expiry)
        if not contract:
            return

        lotsize = int(contract.get("lotsize", 1))
        quantity = max(1, lotsize * self.trade_lot_multiplier)
        symbol = contract.get("symbol")
        exchange = contract.get("exchange")

        if not symbol or not exchange:
            self.logger.error("Contract missing required information: %s", contract)
            return

        if not signal := self.place_order(symbol, exchange, "BUY", quantity, f"entry_{direction}"):
            return

        self.position_state = direction
        self.active_contract = {
            "symbol": symbol,
            "exchange": exchange,
            "quantity": quantity,
            "direction": direction,
            "signal_response": signal,
        }

    def square_off_positions(self, reason: str) -> None:
        """Exit any active position."""
        if not self.position_state or not self.active_contract:
            return

        exit_symbol = self.active_contract["symbol"]
        exit_exchange = self.active_contract["exchange"]
        exit_quantity = int(self.active_contract.get("quantity", 0))

        if exit_quantity <= 0:
            self.logger.error("Invalid quantity for exit: %s", exit_quantity)
            return

        if not self.place_order(exit_symbol, exit_exchange, "SELL", exit_quantity, f"exit_{reason}"):
            self.logger.warning("Exit attempt failed; will retry next cycle")
            return

        self.logger.info("Position closed (%s) for %s", self.position_state, reason)
        self._reset_position()

    # ------------------------------------------------------------------
    # Main execution loop
    # ------------------------------------------------------------------
    def execute_strategy(self) -> None:
        """Run the strategy loop indefinitely."""
        self.logger.info("Starting strategy loop")
        while self.running:
            try:
                candles = self.fetch_market_data()
                if candles is None:
                    time.sleep(30)
                    continue

                ema_df = self.calculate_emas(candles)
                if ema_df is None:
                    time.sleep(30)
                    continue

                exit_signal = self.check_exit_conditions(ema_df)
                if exit_signal:
                    self.square_off_positions(exit_signal)
                    time.sleep(self.poll_interval_seconds)
                    continue

                entry_signal = self.check_entry_conditions(ema_df)
                if entry_signal:
                    self.initiate_trade(entry_signal)

                time.sleep(self.poll_interval_seconds)

            except KeyboardInterrupt:
                self.logger.info("Received stop signal; shutting down")
                self.running = False
                if self.position_state:
                    self.square_off_positions("shutdown")
                break

            except Exception as exc:
                self.logger.exception("Unexpected error in strategy loop: %s", exc)
                time.sleep(30)

        self.logger.info("Strategy loop terminated")


if __name__ == "__main__":
    strategy = NiftyOptionsEMACrossoverStrategy()
    strategy.execute_strategy()
