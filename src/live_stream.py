"""
Layer 1 (live mode): Schwab WebSocket stream via schwabdev.
Import this instead of mock_stream once your Schwab Developer Portal app
shows status "Ready for Use" and your tokens.json has been generated.

Usage in main.py (live mode):
    from src.live_stream import SchwabLiveStream
    stream = SchwabLiveStream(symbols=["AAPL", "TSLA"])
    stream.start(on_tick_callback)
"""

import os
import queue
import threading
from typing import Callable

try:
    import schwabdev
except ImportError:
    schwabdev = None  # type: ignore[assignment]


class SchwabLiveStream:
    """
    Wraps the schwabdev streaming client so the rest of the pipeline can
    consume ticks the same way it consumes mock_stream ticks.

    The WebSocket runs on a background thread; ticks are pushed into a
    thread-safe queue and yielded by the `iter_ticks` generator so the
    main loop stays single-threaded and easy to reason about.
    """

    def __init__(self, symbols: list[str]) -> None:
        if schwabdev is None:
            raise ImportError(
                "schwabdev is not installed. Run: pip install schwabdev"
            )
        self.symbols = symbols
        self._queue: queue.Queue[dict] = queue.Queue()
        self._client = schwabdev.Client(
            app_key=os.environ["SCHWAB_CLIENT_ID"],
            app_secret=os.environ["SCHWAB_CLIENT_SECRET"],
            callback_url="https://127.0.0.1",
        )

    def _on_message(self, raw: dict) -> None:
        """Callback registered with schwabdev — pushes every tick into the queue."""
        self._queue.put(raw)

    def start(self) -> None:
        """
        Subscribe to LEVELONE_EQUITIES for all configured symbols and begin
        streaming.  Blocks until the connection is established, then returns.
        The WebSocket runs on a daemon thread managed by schwabdev.
        """
        self._client.stream.start(
            self._on_message,
            daemon=True,
        )
        # Subscribe to level-1 equity quotes for our symbols
        self._client.stream.send(
            self._client.stream.level_one_equities(
                ",".join(self.symbols),
                "1,2,8",   # fields: bid, ask, volume
            )
        )
        print(f"[LIVE STREAM] Subscribed to: {', '.join(self.symbols)}")

    def iter_ticks(self):
        """Yield ticks from the background WebSocket thread one at a time."""
        while True:
            yield self._queue.get()

    def stop(self) -> None:
        self._client.stream.stop()
        print("[LIVE STREAM] Connection closed.")
