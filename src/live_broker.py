"""
Layer 4 (live mode): Schwab Live Order Execution.

Sends real market orders to Schwab's Trader API.
Uses the same execute_order() / print_summary() interface as
LocalPaperLedger so main.py can swap between modes with zero other changes.
"""

import json
import os
from datetime import datetime

import schwabdev


class SchwabLiveBroker:
    """
    Places real equity orders through Schwab's REST trading API.

    On first use it resolves the account hash automatically by calling
    /trader/v1/accounts/accountNumbers.  If you have multiple linked
    Schwab accounts, set SCHWAB_ACCOUNT_HASH in .env to pin a specific one.
    """

    def __init__(self) -> None:
        self._client = schwabdev.Client(
            app_key=os.environ["SCHWAB_CLIENT_ID"],
            app_secret=os.environ["SCHWAB_CLIENT_SECRET"],
            callback_url="https://127.0.0.1",
        )
        self._fills: list[dict] = []

        # Resolve account hash (env override → first linked account)
        account_hash_override = os.getenv("SCHWAB_ACCOUNT_HASH", "").strip()
        if account_hash_override:
            self._account_hash = account_hash_override
            print(f"[LIVE BROKER] Using account hash from .env: {self._account_hash[:8]}...")
        else:
            self._account_hash = self._fetch_first_account_hash()

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
            raise RuntimeError("[LIVE BROKER] No linked accounts found on this Schwab login.")
        hash_value = accounts[0]["hashValue"]
        acct_num   = accounts[0]["accountNumber"]
        print(f"[LIVE BROKER] Trading account: ****{acct_num[-4:]}  (hash={hash_value[:8]}...)")
        if len(accounts) > 1:
            print(
                f"[LIVE BROKER] NOTE: {len(accounts)} accounts linked. "
                "Set SCHWAB_ACCOUNT_HASH in .env to pick a different one."
            )
        return hash_value

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def execute_order(
        self,
        symbol: str,
        action: str,
        qty: int,
        market_price: float,
    ) -> bool:
        """
        Place a live MARKET order for `qty` shares of `symbol`.

        Args:
            symbol:       Ticker (e.g. "AAPL")
            action:       "BUY" or "SELL"
            qty:          Number of shares
            market_price: Last known bid price — used for logging only.
                          The actual fill price comes from Schwab's matching engine.

        Returns:
            True if the order was accepted (HTTP 201), False otherwise.
        """
        action = action.upper()
        ts     = datetime.now().strftime("%H:%M:%S")

        order = {
            "orderType":          "MARKET",
            "session":            "NORMAL",   # regular market hours; use SEAMLESS for extended
            "duration":           "DAY",       # cancel at EOD if unfilled
            "orderStrategyType":  "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": action,     # "BUY" or "SELL"
                    "quantity":    qty,
                    "instrument": {
                        "symbol":    symbol,
                        "assetType": "EQUITY",
                    },
                }
            ],
        }

        resp = self._client.place_order(self._account_hash, order)

        if resp.status_code in (200, 201):
            # Schwab returns the assigned order ID in the Location header
            order_id = resp.headers.get("Location", "unknown").rstrip("/").split("/")[-1]
            fill_rec = {
                "time":      ts,
                "symbol":    symbol,
                "action":    action,
                "qty":       qty,
                "ref_price": round(market_price, 4),
                "order_id":  order_id,
            }
            self._fills.append(fill_rec)

            # Persist every fill to an append-only journal
            with open("live_fills.jsonl", "a") as fh:
                fh.write(json.dumps(fill_rec) + "\n")

            print(
                f"  ✓ [LIVE FILL]  {ts}  {action} {qty} {symbol} "
                f"@ ~${market_price:.2f}  order_id={order_id}"
            )
            return True

        else:
            print(
                f"  ✗ [LIVE ORDER FAILED]  {ts}  {action} {qty} {symbol}  "
                f"HTTP {resp.status_code}: {resp.text[:400]}"
            )
            return False

    # Alias — keeps any code that still calls execute_paper_order() working
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
                    f"{f['qty']:3d} × {f['symbol']:<6s}  "
                    f"ref=${f['ref_price']:.2f}  "
                    f"order_id={f['order_id']}"
                )
            print(f"\n  Fill details written to: live_fills.jsonl")
            print(f"  Check your Schwab account for actual execution prices.")
        else:
            print("  No orders were placed this session.")
        print("=" * 54 + "\n")
