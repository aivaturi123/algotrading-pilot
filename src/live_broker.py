"""
Layer 4 (live mode): Schwab Live Order Execution.

On startup this broker:
  1. Fetches every existing position from your Schwab account
  2. Loads them into an in-memory position tracker

On every tick main.py calls check_profit_targets(current_prices).
Any position — whether held before the session started or bought during it —
is automatically sold once it reaches TAKE_PROFIT_PCT gain (default 3 %).

Buy-side safety guards (enforced before every BUY):
  • Fetch live balance from Schwab — block if fetch fails
  • dollar_budget = min(available_funds, equity × MAX_ORDER_PCT_OF_ACCOUNT)
  • Block only if available_funds == 0
  • Fractional qty = dollar_budget / market_price (5 d.p.)
"""

import json
import os
from datetime import datetime

import schwabdev


class SchwabLiveBroker:
    """
    Places real equity orders (whole or fractional) through Schwab's REST API
    and monitors all open positions for take-profit exits.

    Configuration (.env):
        MAX_ORDER_PCT_OF_ACCOUNT  float 0–1   default 0.05  (5 % per trade)
        TAKE_PROFIT_PCT           float 0–1   default 0.03  (sell at +3 %)
        SCHWAB_ACCOUNT_HASH       optional    pin a specific linked account
    """

    def __init__(self) -> None:
        self._client = schwabdev.Client(
            app_key=os.environ["SCHWAB_CLIENT_ID"],
            app_secret=os.environ["SCHWAB_CLIENT_SECRET"],
            callback_url="https://127.0.0.1",
        )
        self._fills: list[dict] = []
        self._max_order_pct   = float(os.getenv("MAX_ORDER_PCT_OF_ACCOUNT", "0.05"))
        self._take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "0.03"))

        # { "AAPL": {"qty": 0.23148, "avg_cost": 216.05} }
        self._positions: dict[str, dict] = {}

        # Resolve account hash
        account_hash_override = os.getenv("SCHWAB_ACCOUNT_HASH", "").strip()
        if account_hash_override:
            self._account_hash = account_hash_override
            print(f"[LIVE BROKER] Using account hash from .env: {self._account_hash[:8]}...")
        else:
            self._account_hash = self._fetch_first_account_hash()

        print(
            f"[LIVE BROKER] Buy cap  : {self._max_order_pct  * 100:.0f}% of equity per order\n"
            f"[LIVE BROKER] Take-profit: +{self._take_profit_pct * 100:.0f}% → auto-sell"
        )

        # Load any positions already in the account before we started
        self._load_existing_positions()

    # ------------------------------------------------------------------
    # Startup helpers
    # ------------------------------------------------------------------

    def _fetch_first_account_hash(self) -> str:
        resp = self._client.linked_accounts()
        if not resp.ok:
            raise RuntimeError(
                f"[LIVE BROKER] Could not fetch linked accounts: "
                f"HTTP {resp.status_code}  {resp.text[:300]}"
            )
        accounts = resp.json()
        if not accounts:
            raise RuntimeError("[LIVE BROKER] No linked accounts found.")
        hash_value = accounts[0]["hashValue"]
        acct_num   = accounts[0]["accountNumber"]
        print(f"[LIVE BROKER] Trading account: ****{acct_num[-4:]}  (hash={hash_value[:8]}...)")
        if len(accounts) > 1:
            print(
                f"[LIVE BROKER] {len(accounts)} accounts linked — "
                "set SCHWAB_ACCOUNT_HASH in .env to use a different one."
            )
        return hash_value

    def _load_existing_positions(self) -> None:
        """
        Pull every open long equity position from Schwab and load it into
        self._positions so the take-profit scanner monitors them from tick 1.
        """
        resp = self._client.account_details(self._account_hash, fields="positions")
        if not resp.ok:
            print(
                f"[LIVE BROKER] WARNING: could not load existing positions "
                f"(HTTP {resp.status_code}). Starting with empty position book."
            )
            return

        positions = (
            resp.json()
               .get("securitiesAccount", {})
               .get("positions", [])
        )

        loaded = 0
        for p in positions:
            sym      = p.get("instrument", {}).get("symbol")
            qty      = float(p.get("longQuantity",  0.0))
            avg_cost = float(p.get("averagePrice",  0.0))
            if sym and qty > 0:
                self._positions[sym] = {"qty": qty, "avg_cost": avg_cost}
                print(
                    f"  [POSITION LOADED]  {sym:<6}  qty={qty}  "
                    f"avg_cost=${avg_cost:.4f}"
                )
                loaded += 1

        print(
            f"[LIVE BROKER] {loaded} existing position(s) loaded "
            f"— monitoring for +{self._take_profit_pct * 100:.0f}% take-profit"
        )

    # ------------------------------------------------------------------
    # Balance fetch
    # ------------------------------------------------------------------

    def _get_account_balances(self) -> tuple[float, float]:
        resp = self._client.account_details(self._account_hash)
        if not resp.ok:
            print(
                f"[LIVE BROKER] WARNING: balance fetch failed "
                f"(HTTP {resp.status_code}) — order blocked for safety."
            )
            return (0.0, 0.0)
        try:
            balances = resp.json()["securitiesAccount"]["currentBalances"]
            liq_val  = float(balances.get("liquidationValue", 0.0))
            avail    = float(balances.get("availableFunds",   0.0))
            return (liq_val, avail)
        except (KeyError, TypeError, ValueError) as exc:
            print(f"[LIVE BROKER] WARNING: could not parse balances ({exc}) — order blocked.")
            return (0.0, 0.0)

    # ------------------------------------------------------------------
    # Fractional qty calculator
    # ------------------------------------------------------------------

    def _calc_buy_qty(
        self,
        symbol: str,
        market_price: float,
        liq_val: float,
        avail_funds: float,
    ) -> float:
        if liq_val == 0.0:
            print("  ✗ [BLOCKED] Cannot verify account balance — order rejected for safety.")
            return 0.0
        if avail_funds <= 0.0:
            print(f"  ✗ [BLOCKED] Available funds = $0 — nothing to spend.")
            return 0.0

        dollar_budget = min(avail_funds, liq_val * self._max_order_pct)
        qty = round(dollar_budget / market_price, 5)

        print(
            f"  [RISK CHECK]  equity=${liq_val:,.2f}  "
            f"available=${avail_funds:,.2f}  "
            f"budget({self._max_order_pct*100:.0f}%)=${dollar_budget:,.2f}  "
            f"→  {qty} shares of {symbol}  (~${qty * market_price:,.2f})"
        )
        return qty

    # ------------------------------------------------------------------
    # Take-profit scanner  (called on every tick from main.py)
    # ------------------------------------------------------------------

    def check_profit_targets(self, current_prices: dict[str, float]) -> None:
        """
        For every open position, check whether the current price has reached
        the take-profit threshold.  If so, sell the entire position immediately.

        Call this on every incoming tick from main.py:
            broker.check_profit_targets({"NVDA": 216.06, "AAPL": 310.88, ...})
        """
        for symbol, pos in list(self._positions.items()):
            if symbol not in current_prices:
                continue

            current_price = current_prices[symbol]
            avg_cost      = pos["avg_cost"]
            qty           = pos["qty"]
            pct_gain      = (current_price - avg_cost) / avg_cost

            if pct_gain >= self._take_profit_pct:
                print(
                    f"\n  [TAKE PROFIT] {symbol}  "
                    f"cost=${avg_cost:.4f}  now=${current_price:.4f}  "
                    f"gain=+{pct_gain:.2%}  selling {qty} shares"
                )
                # Remove from tracker BEFORE placing order so a failed API
                # call doesn't trigger a repeated sell on the next tick.
                del self._positions[symbol]
                self.execute_order(symbol, "SELL", qty, current_price)

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def execute_order(
        self,
        symbol: str,
        action: str,
        qty: float,
        market_price: float,
    ) -> bool:
        """
        Place a live MARKET order.

        BUY:  ignores qty — calculates fractional qty from live account balance.
        SELL: uses qty as-is (full position size passed from check_profit_targets).
        """
        action = action.upper()
        ts     = datetime.now().strftime("%H:%M:%S")

        if action == "BUY":
            liq_val, avail_funds = self._get_account_balances()
            actual_qty = self._calc_buy_qty(symbol, market_price, liq_val, avail_funds)
            if actual_qty == 0.0:
                return False
        else:
            actual_qty = float(qty)

        order = {
            "orderType":          "MARKET",
            "session":            "NORMAL",
            "duration":           "DAY",
            "orderStrategyType":  "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": action,
                    "quantity":    actual_qty,
                    "instrument": {
                        "symbol":    symbol,
                        "assetType": "EQUITY",
                    },
                }
            ],
        }

        resp = self._client.place_order(self._account_hash, order)

        if resp.status_code in (200, 201):
            order_id = resp.headers.get("Location", "unknown").rstrip("/").split("/")[-1]
            fill_rec = {
                "time":      ts,
                "symbol":    symbol,
                "action":    action,
                "qty":       actual_qty,
                "ref_price": round(market_price, 4),
                "order_id":  order_id,
            }
            self._fills.append(fill_rec)
            with open("live_fills.jsonl", "a") as fh:
                fh.write(json.dumps(fill_rec) + "\n")
            print(
                f"  ✓ [LIVE FILL]  {ts}  {action} {actual_qty} {symbol} "
                f"@ ~${market_price:.2f}  order_id={order_id}"
            )

            # Keep position tracker in sync
            if action == "BUY":
                if symbol in self._positions:
                    old = self._positions[symbol]
                    new_qty  = old["qty"] + actual_qty
                    new_cost = (old["qty"] * old["avg_cost"] + actual_qty * market_price) / new_qty
                    self._positions[symbol] = {"qty": new_qty, "avg_cost": round(new_cost, 6)}
                else:
                    self._positions[symbol] = {"qty": actual_qty, "avg_cost": market_price}

            return True

        else:
            print(
                f"  ✗ [LIVE ORDER FAILED]  {ts}  {action} {actual_qty} {symbol}  "
                f"HTTP {resp.status_code}: {resp.text[:400]}"
            )
            return False

    def execute_paper_order(self, symbol, action, qty, market_price):
        return self.execute_order(symbol, action, qty, market_price)

    # ------------------------------------------------------------------
    # Position access
    # ------------------------------------------------------------------

    def get_positions(self) -> dict:
        """Return a copy of the current position tracker."""
        return dict(self._positions)

    # ------------------------------------------------------------------
    # Stop-loss context builder
    # ------------------------------------------------------------------

    def get_stop_loss_context(
        self,
        symbol: str,
        pos: dict,
        current_price: float,
    ) -> dict:
        """
        Assemble a rich market-data snapshot for Gemini stop-loss evaluation.

        Fetches from Schwab API:
          • Full real-time quote  (bid/ask, day range, volume, net change, 52-week H/L)
          • 5-minute OHLCV candles for the current session (up to last 12 bars ≈ 1 hour)

        Then packages position P&L metadata alongside all market data so
        Gemini has everything it needs to make a well-reasoned SELL/HOLD call.
        """
        avg_cost   = pos["avg_cost"]
        qty        = pos["qty"]
        dollar_loss = round((current_price - avg_cost) * qty, 2)
        pct_loss    = round((current_price - avg_cost) / avg_cost * 100, 3)

        context: dict = {
            "position": {
                "symbol":        symbol,
                "qty":           qty,
                "avg_cost":      round(avg_cost, 4),
                "current_price": round(current_price, 4),
                "pct_loss":      pct_loss,
                "dollar_loss":   dollar_loss,
            },
            "quote":        None,
            "candles_5min": None,
            "recent_trend": None,
        }

        # --- Real-time quote ---
        try:
            resp = self._client.quote(symbol, fields="all")
            if resp.ok:
                q = resp.json().get(symbol, {}).get("quote", {})
                context["quote"] = {
                    "bid":             q.get("bidPrice"),
                    "ask":             q.get("askPrice"),
                    "last":            q.get("lastPrice"),
                    "open":            q.get("openPrice"),
                    "high":            q.get("highPrice"),
                    "low":             q.get("lowPrice"),
                    "volume":          q.get("totalVolume"),
                    "net_change_pct":  q.get("netPercentChangeInDouble"),
                    "52w_high":        q.get("52WkHigh"),
                    "52w_low":         q.get("52WkLow"),
                }
            else:
                print(f"  [STOP LOSS CTX] Quote fetch failed HTTP {resp.status_code}")
        except Exception as exc:
            print(f"  [STOP LOSS CTX] Quote error: {exc}")

        # --- 5-minute candles (current session, last ~1 hour = 12 bars) ---
        try:
            resp = self._client.price_history(
                symbol,
                periodType="day",
                period=1,
                frequencyType="minute",
                frequency=5,
                needExtendedHoursData=False,
            )
            if resp.ok:
                candles = resp.json().get("candles", [])
                # Keep only the most recent 12 bars to stay concise
                recent = candles[-12:] if len(candles) > 12 else candles
                context["candles_5min"] = [
                    {
                        "open":   c["open"],
                        "high":   c["high"],
                        "low":    c["low"],
                        "close":  c["close"],
                        "volume": c["volume"],
                    }
                    for c in recent
                ]
                # Compute % move across available candles
                if len(recent) >= 2:
                    first_close = recent[0]["close"]
                    last_close  = recent[-1]["close"]
                    if first_close:
                        context["recent_trend"] = round(
                            (last_close - first_close) / first_close * 100, 3
                        )
            else:
                print(f"  [STOP LOSS CTX] Candle fetch failed HTTP {resp.status_code}")
        except Exception as exc:
            print(f"  [STOP LOSS CTX] Candle error: {exc}")

        return context

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def print_summary(self) -> None:
        print("\n" + "=" * 54)
        print("  LIVE TRADING SESSION SUMMARY")
        print("=" * 54)
        print(f"  Orders submitted : {len(self._fills)}")
        if self._fills:
            for f in self._fills:
                print(
                    f"    {f['time']}  {f['action']:4s}  "
                    f"{f['qty']} × {f['symbol']:<6s}  "
                    f"ref=${f['ref_price']:.2f}  "
                    f"order_id={f['order_id']}"
                )
            print(f"\n  Fill details written to: live_fills.jsonl")
            print(f"  Check your Schwab account for actual execution prices.")
        if self._positions:
            print(f"\n  Still open at session end:")
            for sym, pos in self._positions.items():
                print(f"    {sym:<6}  qty={pos['qty']}  avg_cost=${pos['avg_cost']:.4f}")
        if not self._fills:
            print("  No orders were placed this session.")
        print("=" * 54 + "\n")
