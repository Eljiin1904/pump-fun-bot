import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

from solders.pubkey import Pubkey

from src.core.client import SolanaClient
from src.monitoring.logs_event_processor import LogsEventProcessor

logger = logging.getLogger(__name__)


class LogsListener:
    """
    Connects to a Solana logs WebSocket, subscribes to all logs
    matching the processor’s discriminator, and invokes a callback
    for each parsed token event.
    """

    def __init__(
        self,
        wss_endpoint: str,
        processor: LogsEventProcessor,
        client: SolanaClient,
    ) -> None:
        self.wss_endpoint = wss_endpoint
        self.processor = processor
        self.client = client

    async def listen_for_tokens(
        self,
        token_callback: Callable[[Any], Awaitable[None]],
    ) -> None:
        """
        token_callback: coroutine that takes a token-info dict.
        The 8-byte discriminator is now taken from self.processor.discriminator.
        """
        discriminator = self.processor.discriminator

        # open the WebSocket
        async with websockets.connect(self.wss_endpoint) as ws:
            # subscribe to logs that mention our event discriminator
            subscribe = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [discriminator]},
                    {"commitment": self.client.commitment.name.lower()},
                ],
            }
            await ws.send(json.dumps(subscribe))
            confirmation = json.loads(await ws.recv())
            sub_id = confirmation.get("result")
            logger.info(f"Logs subscription confirmed (ID={sub_id})")

            # process incoming notifications until cancelled
            await self._process_messages(ws, sub_id, token_callback)

    async def _process_messages(
        self,
        ws: websockets.WebSocketClientProtocol,
        sub_id: Any,
        token_callback: Callable[[Any], Awaitable[None]],
    ) -> None:
        """
        Internal loop: receive every logsNotification, hand logs
        to the LogsEventProcessor, then invoke the trader’s callback.
        """
        while True:
            raw = await ws.recv()
            msg = json.loads(raw)

            # ensure this is a log notification for our subscription
            if msg.get("method") != "logsNotification":
                continue
            params = msg.get("params", {})
            if params.get("subscription") != sub_id:
                continue

            logs = params.get("params", {}).get("result", {}).get("logs", [])
            try:
                # parse the batch of logs into a token_info dict
                token_info = await self.processor.parse_log_batch(logs)
                if token_info:
                    await token_callback(token_info)
            except Exception as e:
                logger.error(f"Error in LogsListener._process_messages: {e}", exc_info=True)
