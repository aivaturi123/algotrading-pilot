"""
Layer 4 (paper mode): Local Paper Trading Engine.
Intercepts execution commands before they reach any broker.  Manages a
simulated cash balance, tracks open positions and average entry prices,
and writes every fill to an append-only JSON journal on disk.

Behaviour mirrors SchwabLiveBroker as closely as possible:
  • BUY qty is recalculated from MAX_ORDER_PCT_OF_ACCOUNT (default 5 %) of cash balance
    and supports fractional shares — the qty passed by main.py is ignored for BUY.
  • SELL uses the exact qty passed (whole position on take-profit / stop-loss).
  • take-profit and stop-loss checks use the same env vars as live mode.
"""

import json
import os
from datetime import datetime
from pathlib import Path


class LocalPaperLedger:
    """
    Simulates broker fills against live streaming prices without sending
    any real orders.

    Args:
        initial_cash:  Starting virtual balance in USD.
        journal_path:  Where to persist fill history as newline-delimited JSON.
    """

    def __init__(
        self,
        initial_cash: float = 100_000.0,
        journal_path: str = "paper_fills.jsonl",
    ) -> None:
        self.balance = initial_cash
        self.initial_balance = initial_cash
        # { "AAPL": {"qty": 0.51234, "avg_price": 195.20} }
        self.positions: dict[str, dict] = {}
        self.history: list[dict] = []
        self._journal = Path(journal_path)

        # Mirror live broker config — read once at startup
        self._max_order_pct   = float(os.getenv("MAX_ORDER_PCT_OF_ACCOUNT", "0.05"))
        self._take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT",          "0.03"))

    # ------------------------------------------------------------------
    # Core order routing
    # ------------------------------------------------------------------

    def execute_paper_order(
        self,
        symbol: str,
        action: str,
        qty: float,        # ignored for BUY — recalculated from balance cap
        market_price: float,
    ) -> bool:
        """
        Simulate a market fill at `market_price`.

        BUY:  ignores the passed qty and calculates a fractional quantity
              capped at MAX_ORDER_PCT_OF_ACCOUNT of current cash balance,
              exactly mirroring SchwabLiveBroker._calc_buy_qty().
        SELL: uses qty as-is (full position size from take-profit / stop-loss).

        Returns True if the order was accepted, False if rejected.
        """
        action = action.upper()

        # ------------------------------------------------------------------
        # BUY: recalculate fractional qty from MAX_ORDER_PCT_OF_ACCOUNT cash cap
        # ------------------------------------------------------------------
        if action == "BUY":
            if self.balance <= 0:
                print(f"[PAPER LEDGER] REJECTED BUY {symbol}: balance is $0.")
                return False

            dollar_budget = self.balance * self._max_order_pct
            qty = round(dollar_budget / market_price, 5)

            if qty <= 0:
                print(
                    f"[PAPER LEDGER] REJECTED BUY {symbol}: "
                    f"calculated qty={qty} (budget=${dollar_budget:,.2f}, "
                    f"price=${market_price:.2f})"
                )
                return False

            order_value = qty * market_price

            print(
                f"  [PAPER RISK CHECK]  balance=${self.balance:,.2f}  "
                f"budget({self._max_order_pct*100:.0f}%)=${dollar_budget:,.2f}  "
                f"→ {qty} shares of {symbol}  (~${order_value:,.2f})"
            )

            self.balance -= order_value
            if symbol in self.positions:
                pos = self.positions[symbol]
                total_qty  = pos["qty"] + qty
                total_cost = (pos["qty"] * pos["avg_price"]) + order_value
                pos["avg_price"] = total_cost / total_qty
                pos["qty"]       = total_qty
            else:
                self.positions[symbol] = {"qty": qty, "avg_price": market_price}

        # ------------------------------------------------------------------
        # SELL: use the qty passed in (whole position)
        # ------------------------------------------------------------------
        elif action == "SELL":
            order_value = qty * market_price

            if symbol not in self.positions or self.positions[symbol]["qty"] < qty - 1e-9:
                print(
                    f"[PAPER LEDGER] REJECTED SELL {qty} {symbol}: "
                    "insufficient shares"
                )
                return False

            self.balance += order_value
            pos = self.positions[symbol]
            pos["qty"] = round(pos["qty"] - qty, 9)
            if pos["qty"] <= 1e-9:          # float-safe "position is closed"
                del self.positions[symbol]

        else:
            print(f"[PAPER LEDGER] Unknown action '{action}' — ignoring.")
            return False

        entry = {
            "time":           datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            "symbol":         symbol,
            "action":         action,
            "qty":            qty,
            "price":          round(market_price, 4),
            "order_value":    round(order_value, 2),
            "cash_remaining": round(self.balance, 2),
        }
        self.history.append(entry)
        self._append_to_journal(entry)

        print(
            f"[PAPER FILL] {entry['time']} | {action} {qty} {symbol} "
            f"@ ${market_price:.2f}  |  Cash: ${self.balance:,.2f}"
        )
        return True

    # Alias — same interface as SchwabLiveBroker so main.py can use one call
    def execute_order(self, symbol, action, qty, market_price):
        return self.execute_paper_order(symbol, action, qty, market_price)

    def get_positions(self) -> dict:
        """
        Return positions in the same normalised shape as SchwabLiveBroker:
            { "AAPL": {"qty": 0.51234, "avg_cost": 195.20}, ... }
        (Paper ledger stores avg_price internally; we expose avg_cost here
        so main.py can use a single code-path for both paper and live.)
        """
        return {
            sym: {"qty": pos["qty"], "avg_cost": pos["avg_price"]}
            for sym, pos in self.positions.items()
        }

    def check_profit_targets(self, current_prices: dict) -> None:
        """
        Mirror of SchwabLiveBroker.check_profit_targets for paper mode.
        Sells any simulated position that has reached TAKE_PROFIT_PCT gain.
        Uses self._take_profit_pct read once at startup — no per-tick os.getenv().
        """
        for symbol, pos in list(self.positions.items()):
            if symbol not in current_prices:
                continue
            current_price = current_prices[symbol]
            avg_cost      = pos["avg_price"]
            qty           = pos["qty"]
            pct_gain      = (current_price - avg_cost) / avg_cost
            if pct_gain >= self._take_profit_pct:
                print(
                    f"\n  [TAKE PROFIT] {symbol}  "
                    f"cost=${avg_cost:.4f}  now=${current_price:.4f}  "
                    f"gain=+{pct_gain:.2%}  selling {qty} shares"
                )
                self.execute_paper_order(symbol, "SELL", qty, current_price)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def portfolio_summary(self) -> dict:
        """Return a snapshot of current virtual portfolio state."""
        return {
            "cash":             round(self.balance, 2),
            "initial_cash":     self.initial_balance,
            "open_positions":   self.positions,
            "total_fills":      len(self.history),
            "net_cash_change":  round(self.balance - self.initial_balance, 2),
        }

    def print_summary(self) -> None:
        summary = self.portfolio_summary()
        print("\n" + "=" * 50)
        print("  PAPER TRADING PORTFOLIO SUMMARY")
        print("=" * 50)
        print(f"  Cash balance : ${summary['cash']:>12,.2f}")
        print(f"  Net PnL      : ${summary['net_cash_change']:>+12,.2f}")
        print(f"  Total fills  : {summary['total_fills']}")
        if summary["open_positions"]:
            print("\n  Open positions:")
            for sym, pos in summary["open_positions"].items():
                print(f"    {sym:6s}  qty={pos['qty']}  avg_entry=${pos['avg_price']:.4f}")
        else:
            print("  No open positions.")
        print("=" * 50 + "\n")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_to_journal(self, entry: dict) -> None:
        with self._journal.open("a") as f:
            f.write(json.dumps(entry) + "\n")
