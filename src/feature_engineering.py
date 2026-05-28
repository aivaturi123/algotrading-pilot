"""
Layer 2: Analytics Engine.
Maintains a fixed-size rolling window of price ticks per symbol using Polars
(Rust-backed, zero-copy DataFrames).  Computes rolling mean and standard
deviation locally — $0.00 token cost, runs entirely on the CPU.
"""

import polars as pl


class PolarsTickAggregator:
    """
    Accumulates raw tick packets into per-symbol Polars DataFrames and
    exposes one-row signal snapshots for the AI filter to consume.

    Args:
        window_size: Number of most-recent ticks to keep per symbol.
                     30-50 ticks gives a tight, fast-reacting rolling window.
    """

    def __init__(self, window_size: int = 30) -> None:
        self.window_size = window_size
        self._frames: dict[str, pl.DataFrame] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add_tick(self, raw_tick: dict) -> None:
        """Parse a raw Schwab-format tick packet and append it to the buffer."""
        content = raw_tick["content"][0]
        symbol: str = content["key"]

        new_row = pl.DataFrame({
            "timestamp":  [int(raw_tick["timestamp"])],
            "symbol":     [symbol],
            "bid":        [float(content.get("1", 0.0))],
            "ask":        [float(content.get("2", 0.0))],
            "volume":     [int(content.get("8", 0))],
        })

        if symbol not in self._frames:
            self._frames[symbol] = new_row
        else:
            combined = pl.concat([self._frames[symbol], new_row])
            # Keep only the most recent `window_size` rows
            self._frames[symbol] = combined.tail(self.window_size)

    def calculate_signals(self, symbol: str) -> pl.DataFrame:
        """
        Returns a single-row DataFrame enriched with rolling statistics, or
        an empty DataFrame if fewer than 5 ticks have been collected (not
        enough data for a meaningful standard deviation).

        Columns returned:
            timestamp, symbol, bid, ask, volume,
            rolling_mean_bid, rolling_std_bid, cumulative_volume
        """
        if symbol not in self._frames:
            return pl.DataFrame()

        df = self._frames[symbol]

        if len(df) < 5:
            return pl.DataFrame()

        # Aggregate over the whole window (already capped at window_size rows)
        return df.with_columns([
            pl.col("bid").mean().alias("rolling_mean_bid"),
            pl.col("bid").std().alias("rolling_std_bid"),
            pl.col("volume").sum().alias("cumulative_volume"),
        ]).tail(1)   # Return only the latest enriched row

    def get_buffer(self, symbol: str) -> pl.DataFrame:
        """Expose the full raw buffer for a symbol (useful for debugging)."""
        return self._frames.get(symbol, pl.DataFrame())
