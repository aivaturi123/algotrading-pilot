"""
Layer 4 (paper mode): Local Paper Trading Engine.
Intercepts execution commands before they reach any broker.  Manages a
simulated $100,000 cash balance, tracks open positions and average entry
prices, and writes every fill to an append-only JSON journal on disk.
"""

import json
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
        # { "AAPL": {"qty": 10, "avg_price": 175.20} }
        self.positions: dict[str, dict] = {}
        self.history: list[dict] = []
        self._journal = Path(journal_path)

    # ------------------------------------------------------------------
    # Core order routing
    # ------------------------------------------------------------------

    def execute_paper_order(
        self,
        symbol: str,
        action: str,
        qty: int,
        market_price: float,
    ) -> bool:
        """
        Simulate a market fill at `market_price`.

        Returns True if the order was accepted, False if rejected.
        """
        action = action.upper()
        order_value = qty * market_price

        if action == "BUY":
            if order_value > self.balance:
                print(
                    f"[PAPER LEDGER] REJECTED BUY {qty} {symbol}: "
                    f"need ${order_value:,.2f}, have ${self.balance:,.2f}"
                )
                return False

            self.balance -= order_value
            if symbol in self.positions:
                pos = self.positions[symbol]
                total_qty = pos["qty"] + qty
                total_cost = (pos["qty"] * pos["avg_price"]) + order_value
                pos["avg_price"] = total_cost / total_qty
                pos["qty"] = total_qty
            else:
                self.positions[symbol] = {"qty": qty, "avg_price": market_price}

        elif action == "SELL":
            if symbol not in self.positions or self.positions[symbol]["qty"] < qty:
                print(
                    f"[PAPER LEDGER] REJECTED SELL {qty} {symbol}: "
                    "insufficient shares"
                )
                return False

            self.balance += order_value
            pos = self.positions[symbol]
            pos["qty"] -= qty
            if pos["qty"] == 0:
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
                print(f"    {sym:6s}  qty={pos['qty']}  avg_entry=${pos['avg_price']:.2f}")
        else:
            print("  No open positions.")
        print("=" * 50 + "\n")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_to_journal(self, entry: dict) -> None:
        with self._journal.open("a") as f:
            f.write(json.dumps(entry) + "\n")
