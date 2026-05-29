"""
Layer 1 (live mode): Schwab WebSocket stream via schwabdev.
Import this instead of mock_stream once your Schwab Developer Portal app
shows status "Ready for Use" and your tokens.json has been generated.
"""

import json
import os
import queue

import schwabdev


class SchwabLiveStream:
    """
    Wraps schwabdev's Stream class so the rest of the pipeline consumes
    ticks in the same dict format as mock_stream produces.

    The WebSocket runs on a background daemon thread managed by schwabdev.
    Incoming raw JSON strings are parsed and normalised into our standard
    tick shape, then pushed into a thread-safe queue for the main loop.
    """

    def __init__(self, symbols: list[str], debug: bool = False) -> None:
        self.symbols = symbols
        self.debug = debug
        self._queue: queue.Queue[dict] = queue.Queue()

        self._client = schwabdev.Client(
            app_key=os.environ["SCHWAB_CLIENT_ID"],
            app_secret=os.environ["SCHWAB_CLIENT_SECRET"],
            callback_url="https://127.0.0.1",
        )
        # Stream is a separate class — must be instantiated with the client
        self._stream = schwabdev.Stream(self._client)

    def _on_message(self, raw: str) -> None:
        """
        Receiver callback registered with schwabdev.
        Raw is a JSON string — parse it, extract LEVELONE_EQUITIES content,
        and push each tick into the queue in our standard format.
        """
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            if self.debug:
                print(f"  [STREAM RAW] Could not parse: {str(raw)[:120]}")
            return

        if self.debug:
            print(f"  [STREAM RAW] keys={list(msg.keys())}  preview={str(msg)[:200]}")

        # Schwab streaming envelope: {"data": [{...}, ...]}
        for block in msg.get("data", []):
            if block.get("service") != "LEVELONE_EQUITIES":
                continue
            for item in block.get("content", []):
                tick = {
                    "service":   "LEVELONE_EQUITIES",
                    "timestamp": block.get("timestamp", 0),
                    "command":   block.get("command", "SUBS"),
                    "content":   [item],
                }
                self._queue.put(tick)

    def start(self) -> None:
        """
        Pre-load subscriptions before starting the stream so they are
        replayed automatically right after login completes.
        """
        # Register subscription BEFORE start() so schwabdev replays it
        # immediately after the login handshake — avoids the race condition
        # where send() fires before self.active is True.
        self._stream.send(
            self._stream.level_one_equities(
                keys=self.symbols,
                fields=[1, 2, 8],      # 1=bid, 2=ask, 8=volume (key is always present)
                command="SUBS",        # full replace, not incremental add
            )
        )

        self._stream.start(self._on_message, daemon=True)
        print(f"[LIVE STREAM] Subscribed to: {', '.join(self.symbols)}")

    def iter_ticks(self):
        """
        Yield ticks from the background thread one at a time.
        Uses a 1-second timeout so Ctrl+C can interrupt the blocking queue call.
        """
        while True:
            try:
                yield self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

    def stop(self) -> None:
        self._stream.stop()
        print("[LIVE STREAM] Connection closed.")
