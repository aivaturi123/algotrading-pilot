"""
Layer 1 (paper-trading substitute): generates synthetic tick packets that
mirror the exact JSON shape the real Schwab WebSocket stream emits.
Swap this import for src.live_stream once your Schwab app is approved.
"""

import time
import random
from typing import Iterator

# Realistic baseline prices for each symbol
_BASE_PRICES: dict[str, float] = {
    "AAPL": 195.0,
    "TSLA": 250.0,
    "NVDA": 875.0,
}


def stream_market_data(
    symbols: list[str] | None = None,
    delay: float = 0.04,
) -> Iterator[dict]:
    """
    Yields synthetic tick dicts that look like Schwab LEVELONE_EQUITIES packets.
    Runs indefinitely until the caller stops iterating (e.g. Ctrl-C).

    Each yielded dict has this shape (matching the Schwab streaming spec):
        {
            "service": "LEVELONE_EQUITIES",
            "timestamp": <epoch ms>,
            "command": "SUBS",
            "content": [{
                "key":  <symbol>,
                "1":    <bid price>,
                "2":    <ask price>,
                "8":    <last trade volume>,
            }]
        }

    Occasional ~3-sigma spikes are injected with 2 % probability per tick
    so the downstream threshold logic has events to respond to during testing.
    """
    if symbols is None:
        symbols = ["AAPL"]

    prices = {s: _BASE_PRICES.get(s, 100.0) for s in symbols}

    while True:
        symbol = random.choice(symbols)
        price = prices[symbol]

        # Normal micro-movement
        change = random.gauss(0, 0.15)

        # 2 % chance of an aggressive spike that trips the 2.5-sigma gate
        if random.random() < 0.02:
            change = random.uniform(0.8, 1.5)

        price = round(max(1.0, price + change), 2)
        prices[symbol] = price

        volume = max(0, int(random.gauss(1_000, 300)))
        spread = round(random.uniform(0.01, 0.05), 2)

        yield {
            "service": "LEVELONE_EQUITIES",
            "timestamp": int(time.time() * 1_000),
            "command": "SUBS",
            "content": [{
                "key": symbol,
                "1": price,                      # bid
                "2": round(price + spread, 2),   # ask
                "8": volume,                     # volume
            }],
        }

        time.sleep(delay)
