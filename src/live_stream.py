"""
Layer 1 (live mode): Schwab WebSocket stream via schwabdev.
Import this instead of mock_stream once your Schwab Developer Portal app
shows status "Ready for Use" and your tokens.db has been generated.
"""

import json
import os
import queue
import time

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

        # ---------------------------------------------------------------
        # Subscription acknowledgments arrive in msg["response"].
        # Always print these so we see successes AND failures (code 22 etc.)
        # ---------------------------------------------------------------
        for block in msg.get("response", []):
            service = block.get("service", "")
            command = block.get("command", "")
            content = block.get("content", {})
            code    = content.get("code", -1)
            msg_txt = content.get("msg", "")
            # Print subscription responses unconditionally (not just in debug mode)
            # so code-22 errors are always visible.
            if service not in ("ADMIN",) or code != 0:
                print(
                    f"  [STREAM RESP] service={service}  command={command}  "
                    f"code={code}  msg={msg_txt!r}"
                )
            if code == 22:
                print(
                    "  [STREAM ERROR] code 22 = Schwab rejected the subscription.\n"
                    "  Possible fixes:\n"
                    "    1. Log into schwab.com → trade → streaming → accept Real-Time\n"
                    "       Quote Terms (first-time only).\n"
                    "    2. Delete ~/.schwabdev/tokens.db and re-authenticate.\n"
                    "    3. Verify your Schwab app is 'Ready for Use' in the dev portal."
                )

        # ---------------------------------------------------------------
        # Schwab streaming data envelope: {"data": [{...}, ...]}
        # ---------------------------------------------------------------
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
        Pre-register subscriptions BEFORE starting the stream.

        Calling send() while the event loop is not yet running causes
        schwabdev to record the request in self._stream.subscriptions
        without actually sending anything.  When _run_streamer later
        connects and logs in, it auto-replays those queued subscriptions
        using the ADD command — which Schwab's server accepts reliably.

        This avoids the SUBS-vs-ADD ambiguity and any post-login timing
        race that occurred with our previous approach.
        """
        symbols_str = ",".join(self.symbols)

        # ------------------------------------------------------------------
        # Optional diagnostic: verify REST quote access before streaming
        # ------------------------------------------------------------------
        if self.debug:
            try:
                r = self._client.quotes(self.symbols[:2])
                if r.ok:
                    data = r.json()
                    sample = list(data.keys())[:2]
                    print(
                        f"  [LIVE STREAM] REST quotes API OK — "
                        f"sample symbols in response: {sample}"
                    )
                else:
                    print(
                        f"  [LIVE STREAM] REST quotes API returned HTTP {r.status_code}. "
                        f"Body: {r.text[:200]}"
                    )
            except Exception as exc:
                print(f"  [LIVE STREAM] REST quotes API exception: {exc}")

        # ------------------------------------------------------------------
        # Pre-register subscription — event loop is None here so send()
        # just records into self._stream.subscriptions (nothing is sent yet).
        # ------------------------------------------------------------------
        self._stream.send(
            self._stream.level_one_equities(
                keys=symbols_str,
                fields="0,1,2,3,4,5,6,7,8",   # 0=Symbol, 1=Bid, 2=Ask, 3=Last,
                                                # 4=BidSz, 5=AskSz, 8=Volume
                command="ADD",
            )
        )

        if self.debug:
            print(
                f"  [LIVE STREAM] Queued subscription for {len(self.symbols)} symbol(s):"
                f" {symbols_str}\n"
                f"  [LIVE STREAM] Recorded subscriptions: {self._stream.subscriptions}"
            )

        # ------------------------------------------------------------------
        # Start the background WebSocket thread.
        # Inside _run_streamer, schwabdev will:
        #   1. Connect to Schwab's WS
        #   2. Send LOGIN request
        #   3. Receive login response → call _on_message → set active = True
        #   4. Auto-replay queued subscriptions as ADD → call _on_message
        #   5. Enter the main listener loop
        # ------------------------------------------------------------------
        self._stream.start(self._on_message, daemon=True)

        # Wait until login response is received and stream is marked active
        timeout = 15
        waited  = 0
        while not self._stream.active and waited < timeout:
            time.sleep(0.2)
            waited += 0.2

        if not self._stream.active:
            print("[LIVE STREAM] WARNING: stream did not become active within 15 s")
        else:
            # Give the event loop time to finish sending the queued subscription
            # and receive the ACK before we start reading ticks.
            time.sleep(0.8)
            print(f"[LIVE STREAM] Stream active. Subscription sent for: {', '.join(self.symbols)}")

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
