"""
Layer 4 (live mode): Schwab Live Order Execution.

Every BUY order spends exactly MAX_ORDER_PCT_OF_ACCOUNT (default 10 %) of
your total account equity, expressed as a fractional share quantity so the
full budget is always deployed regardless of share price.

  $50  account · 10 % cap · NVDA @$216  →  0.02315 shares  (~$5.00)
  $500 account · 10 % cap · NVDA @$216  →  0.23148 shares  (~$50.00)
  $2k  account · 10 % cap · NVDA @$216  →  0.92593 shares  (~$200.00)

Safety guards (BUY only — enforced before any request is sent):
  1. Fetch live balance from Schwab — if this fails, block for safety
  2. dollar_budget = min(available_funds, equity × max_pct)   ← hard 10% cap
  3. Block only if available_funds == 0 (account literally empty)
"""

import json
import os
from datetime import datetime

import schwabdev


class SchwabLiveBroker:
    """
    Places real equity orders (whole or fractional) through Schwab's REST API.

    Configuration (.env):
        MAX_ORDER_PCT_OF_ACCOUNT  float 0–1   default 0.10  (10 % per trade)
        SCHWAB_ACCOUNT_HASH       optional    pin a specific linked account
    """

    def __init__(self) -> None:
        self._client = schwabdev.Client(
            app_key=os.environ["SCHWAB_CLIENT_ID"],
            app_secret=os.environ["SCHWAB_CLIENT_SECRET"],
            callback_url="https://127.0.0.1",
        )
        self._fills: list[dict] = []
        self._max_order_pct = float(os.getenv("MAX_ORDER_PCT_OF_ACCOUNT", "0.10"))

        account_hash_override = os.getenv("SCHWAB_ACCOUNT_HASH", "").strip()
        if account_hash_override:
            self._account_hash = account_hash_override
            print(f"[LIVE BROKER] Using account hash from .env: {self._account_hash[:8]}...")
        else:
            self._account_hash = self._fetch_first_account_hash()

        print(
            f"[LIVE BROKER] Position-size cap : "
            f"{self._max_order_pct * 100:.0f}% of account equity per order  "
            f"(fractional shares enabled)"
        )

    # ------------------------------------------------------------------
    # Startup
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

    # ------------------------------------------------------------------
    # Balance fetch
    # ------------------------------------------------------------------

    def _get_account_balances(self) -> tuple[float, float]:
        """
        Returns (liquidation_value, available_funds).
        Falls back to (0.0, 0.0) on any error, which will block the order.
        """
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
    # Fractional quantity calculation
    # ------------------------------------------------------------------

    def _calc_buy_qty(
        self,
        symbol: str,
        market_price: float,
        liq_val: float,
        avail_funds: float,
    ) -> float:
        """
        Calculate fractional share qty for a BUY so that the order value
        equals exactly the allowed budget (10% of equity, capped at available).

        Returns 0.0 only if the account balance could not be verified or
        there are literally no available funds.
        """
        # Guard: balance fetch failed
        if liq_val == 0.0:
            print("  ✗ [BLOCKED] Cannot verify account balance — order rejected for safety.")
            return 0.0

        # Guard: no spendable cash at all
        if avail_funds <= 0.0:
            print(f"  ✗ [BLOCKED] Available funds = $0 — nothing to spend.")
            return 0.0

        # Hard 10% cap: never spend more than max_pct of total equity,
        # and never more than what's actually available
        dollar_budget = min(avail_funds, liq_val * self._max_order_pct)

        # Fractional qty — 5 decimal places (Schwab's precision)
        qty = round(dollar_budget / market_price, 5)

        print(
            f"  [RISK CHECK]  equity=${liq_val:,.2f}  "
            f"available=${avail_funds:,.2f}  "
            f"budget(10%)=${dollar_budget:,.2f}  "
            f"→  {qty} shares of {symbol}  (~${qty * market_price:,.2f})"
        )
        return qty

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

        BUY:  ignores the `qty` argument; calculates fractional qty from
              the live account balance and the 10% position-size cap.
        SELL: uses the `qty` argument as-is.
        """
        action = action.upper()
        ts     = datetime.now().strftime("%H:%M:%S")

        if action == "BUY":
            liq_val, avail_funds = self._get_account_balances()
            actual_qty = self._calc_buy_qty(symbol, market_price, liq_val, avail_funds)
            if actual_qty == 0.0:
                return False  # blocked — reason already printed
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
        else:
            print("  No orders were placed this session.")
        print("=" * 54 + "\n")
