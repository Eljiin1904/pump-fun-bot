# src/monitoring/logs_listener.py

import json
import asyncio
import websockets
from solders.pubkey import Pubkey
from .logs_event_processor import LogsEventProcessor


class LogsListener:
    """
    Subscribes to Solana program logs via logsSubscribe and
    hands parsed TokenInfo objects off to the PumpTrader.
    """

    def __init__(
        self,
        client,
        token_queue: asyncio.Queue,
        wss_endpoint: str,
        program_id: Pubkey,
        discriminator: str,
    ):
        self.client = client
        self.queue = token_queue
        self.wss_endpoint = wss_endpoint
        self.program_id = program_id
        self.processor = LogsEventProcessor(discriminator, program_id)
        self._ws = None
        self._running = False

    async def listen_for_tokens(
        self,
        token_callback,
        match_string=None,
        creator_address=None,
    ):
        """
        Connects to the WebSocket, subscribes to logs, and
        for each new TokenInfo extracted calls token_callback.
        """
        self._running = True
        async with websockets.connect(self.wss_endpoint) as ws:
            self._ws = ws
            # subscribe to logs mentioning our program ID
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [str(self.program_id)]},
                    {"commitment": "confirmed"},
                ],
            }))
            # wait for the confirmation
            await ws.recv()

            while self._running:
                raw = await ws.recv()
                msg = json.loads(raw)
                result = msg.get("params", {}).get("result")
                if not result:
                    continue

                for token_info in self.processor.extract_new_tokens(
                    result, match_string, creator_address
                ):
                    # enqueue or directly callback, as your PumpTrader expects
                    await token_callback(token_info)

    async def stop(self):
        """Stops listening and closes the WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
