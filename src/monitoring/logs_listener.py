# src/monitoring/logs_listener.py

import asyncio
import json
import time
from typing import Optional, Callable, Awaitable, Any, Dict

import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError, ProtocolError

from solders.pubkey import Pubkey

from src.core.client import SolanaClient as SolanaClientWrapper
from src.core.pubkeys import PumpAddresses, SolanaProgramAddresses
# Corrected base class import name
from src.monitoring.base_listener import BaseTokenListener
from src.monitoring.logs_event_processor import LogsEventProcessor
from src.trading.base import TokenInfo
from src.utils.logger import get_logger

logger = get_logger(__name__)

MAX_RECONNECT_ATTEMPTS = 5
INITIAL_RECONNECT_DELAY = 5  # seconds


class LogsListener(BaseTokenListener):
    """Listens for pump.fun program logs via WebSocket."""

    # Constructor accepts client, endpoint, processor - This is correct
    def __init__(
            self,
            client: SolanaClientWrapper,
            wss_endpoint: str,
            processor: LogsEventProcessor
    ):
        self.client = client
        self.wss_endpoint = wss_endpoint
        self.processor = processor
        self.logger = logger

        self.websocket: Optional[WebSocketClientProtocol] = None
        self.subscription_id: Optional[int] = None
        self.is_running = False
        self._current_callback: Optional[Callable[[TokenInfo], Awaitable[None]]] = None
        self._next_req_id = 1
        # Add _stopped_event if needed for cleaner shutdown signalling internal loops
        self._stopped_event = asyncio.Event()

        # listen_for_tokens method seems correct (uses manual JSON subscribe)

    async def listen_for_tokens(
            self,
            token_callback: Callable[[TokenInfo], Awaitable[None]],
    ) -> None:
        if self.is_running:
            return

        self._current_callback = token_callback
        self.is_running = True
        self._stopped_event.clear()  # Reset stop event on start
        self.logger.info(f"Starting LogsListener for endpoint: {self.wss_endpoint}")

        reconnect_attempts = 0
        delay = INITIAL_RECONNECT_DELAY

        # Use self.is_running for the outer reconnect loop condition
        while self.is_running and reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
            try:
                async with websockets.connect(
                        self.wss_endpoint, ping_interval=20, ping_timeout=20
                ) as ws:
                    self.websocket = ws
                    reconnect_attempts = 0  # Reset on success
                    delay = INITIAL_RECONNECT_DELAY  # Reset delay
                    self.logger.info("WebSocket connected. Subscribing to logs...")

                    req_id = self._next_req_id
                    self._next_req_id += 1

                    subscribe_payload = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [str(PumpAddresses.PROGRAM_ID)]},
                            {"commitment": "confirmed"}  # Use string directly
                        ]
                    }
                    await ws.send(json.dumps(subscribe_payload))
                    self.logger.debug(f"Sent logsSubscribe (ID {req_id})")

                    # Process messages until connection closes or stopped
                    await self._process_messages(ws, req_id)

            except (
            ConnectionClosedOK, ConnectionClosedError, ProtocolError, websockets.exceptions.InvalidStatusCode) as e:
                # Handle disconnects and specific errors like bad status code
                reconnect_attempts += 1
                log_level = logger.error if isinstance(e, websockets.exceptions.InvalidStatusCode) else logger.warning
                log_level(
                    f"WebSocket disconnected ({reconnect_attempts}/{MAX_RECONNECT_ATTEMPTS}): {e}"
                )
                if isinstance(e, websockets.exceptions.InvalidStatusCode):
                    logger.error("Stopping attempts due to invalid status code (check endpoint/key).")
                    self.is_running = False  # Stop trying if auth/endpoint is bad
                elif self.is_running and reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)  # Exponential backoff capped at 60s
                elif self.is_running:  # Max attempts reached
                    logger.error("Max reconnect attempts reached. Stopping listener.")
                    self.is_running = False
            except Exception as e:
                self.logger.error(f"Unexpected error in listen_for_tokens: {e}", exc_info=True)
                self.is_running = False  # Stop on unexpected errors
                break  # Exit loop
            finally:
                # Cleanup for this attempt
                self.websocket = None
                self.subscription_id = None

        self.logger.info("LogsListener listen_for_tokens loop finished.")
        self._stopped_event.set()  # Signal completion

    async def _process_messages(
            self,
            websocket: WebSocketClientProtocol,
            subscribe_req_id: int
    ) -> None:
        """Processes messages received on the WebSocket."""
        self.subscription_id = None  # Reset sub ID for this connection
        while self.is_running:  # Check is_running flag
            try:
                raw_msg = await asyncio.wait_for(websocket.recv(), timeout=60.0)
                msg = json.loads(raw_msg)

                # Handle subscription confirmation
                if msg.get("id") == subscribe_req_id and "result" in msg:
                    if isinstance(msg["result"], int):
                        self.subscription_id = msg["result"]
                        self.logger.info(f"Subscription confirmed with ID: {self.subscription_id}")
                    else:
                        self.logger.warning(
                            f"Subscription confirmation received, but result is not an integer ID: {msg['result']}")
                    continue  # Move to next message once confirmation is handled

                # Handle notifications
                params = msg.get("params")
                if msg.get("method") == "logsNotification" and params:
                    subscription_id_in_params = params.get("subscription")
                    # Process only if it matches our current subscription ID
                    if self.subscription_id is not None and subscription_id_in_params == self.subscription_id:
                        notification_result = params.get("result")
                        # Ensure the result structure is valid before passing
                        if isinstance(notification_result,
                                      dict) and "value" in notification_result and self._current_callback:
                            # --- CORRECTED PROCESSOR CALL ---
                            # Call the method that expects the result dictionary
                            await self.processor.process_log_notification_result(
                                notification_result, self._current_callback
                            )
                            # --- END CORRECTION ---
                        # Optional: Log if result format is bad
                        # elif notification_result: logger.warning("...")
                    # Optional: Log if message is for a different/old subscription
                    # elif self.subscription_id is not None: logger.warning("...")
                    continue  # Move to next message

                # Optional: Handle other message types if needed
                # logger.debug(f"Received unhandled WS message type: {msg}")

            except asyncio.TimeoutError:
                # Send ping on timeout
                try:
                    pong_waiter = await websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=10)
                except asyncio.TimeoutError:
                    self.logger.warning("WebSocket pong timeout. Breaking process loop.")
                    break  # Assume connection is dead, exit to reconnect
                except Exception as e_ping:
                    self.logger.warning(f"WebSocket ping failed: {e_ping}. Breaking process loop.")
                    break  # Assume connection is dead, exit to reconnect
            except (ConnectionClosedOK, ConnectionClosedError, ProtocolError):
                self.logger.warning("WebSocket closed during message processing.")
                break  # Exit loop normally on close
            except json.JSONDecodeError:
                self.logger.warning(f"Received non-JSON WS message: {raw_msg[:100]}")
                continue  # Skip malformed message
            except Exception as e:
                self.logger.error(f"Error processing message: {e}", exc_info=True)
                # Consider if we should break or continue on processing errors
                await asyncio.sleep(1)  # Small delay before continuing

    async def stop(self) -> None:
        """Signals the listener to stop and attempts to clean up resources."""
        if not self.is_running and not self.websocket:
            return

        self.logger.info("Stopping LogsListener...")
        self.is_running = False  # Signal loops to stop
        self._stopped_event.set()  # Signal completion event if anything is waiting on it

        ws = self.websocket  # Local ref
        sub_id = self.subscription_id  # Local ref
        self.websocket = None  # Clear instance vars immediately
        self.subscription_id = None

        if ws and sub_id is not None:
            try:
                # Send unsubscribe using manual JSON
                req_id = self._next_req_id
                self._next_req_id += 1
                unsubscribe = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "logsUnsubscribe",
                    "params": [sub_id],
                }
                await ws.send(json.dumps(unsubscribe))
                self.logger.info(f"Sent unsubscribe for subscription ID: {sub_id}")
            except Exception as e:
                self.logger.warning(f"Error sending unsubscribe (connection might be closed): {e}")

        if ws:  # Close websocket if reference exists
            try:
                await ws.close(code=1000, reason="Client stopping")
                self.logger.info("WebSocket connection closed by stop().")
            except Exception as e:
                self.logger.warning(f"Error closing WebSocket during stop(): {e}")

        self._current_callback = None
        self.logger.info("LogsListener shutdown attempt complete.")