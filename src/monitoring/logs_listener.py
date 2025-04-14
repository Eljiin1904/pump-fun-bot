# src/monitoring/logs_listener.py

import asyncio
import json
import websockets # Keep this import
import base64
# --- FIX: Add missing imports ---
from typing import Optional, Callable, Awaitable, List, Set # Added Set
from websockets.client import WebSocketClientProtocol # Added WebSocketClientProtocol
# --- End Fix ---

from solders.pubkey import Pubkey
from solana.rpc.commitment import Confirmed

# Import TokenInfo from the canonical trading module
from ..trading.base import TokenInfo
# Import BaseTokenListener if it's truly the parent class
from ..monitoring.base_listener import BaseTokenListener # Keep if needed
from ..core.client import SolanaClient
from ..core.pubkeys import PumpAddresses # Keep this import
from .logs_event_processor import LogsEventProcessor
from ..utils.logger import get_logger

logger = get_logger(__name__)

# Assuming BaseTokenListener is defined elsewhere and LogsListener inherits from it
class LogsListener(BaseTokenListener): # Keep inheritance if correct
    def __init__(self, wss_endpoint: str, client: SolanaClient):
        # If BaseTokenListener has an __init__, call it
        # super().__init__(wss_endpoint, client) # Uncomment if BaseTokenListener has __init__

        self.wss_endpoint = wss_endpoint
        # --- FIX: Use correct attribute name ---
        self.program_id = PumpAddresses.PROGRAM_ID
        # --- End Fix ---
        self.program_id_str = str(self.program_id) # Keep using the string version for mentions
        self.solana_client = client # Store the client wrapper if needed by processor
        self.processor = LogsEventProcessor(self.solana_client) # Pass client to processor
        # Use the imported type hint
        self.websocket: Optional[WebSocketClientProtocol] = None
        self.subscription_id: Optional[int] = None
        self._stop_event = asyncio.Event()
        # Use the imported type hint
        self.tasks: Set[asyncio.Task] = set() # Initialize task set if used for managing background tasks
        logger.info(f"LogsListener initialized for WSS: {self.wss_endpoint}")

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: Optional[str] = None,
        creator_address: Optional[str] = None
    ) -> None:
        """
        Connects to the WebSocket and processes logs continuously.
        """
        while not self._stop_event.is_set():
            try:
                logger.info(f"Attempting WebSocket connection to {self.wss_endpoint}...")
                # Use standard websockets.connect context manager
                async with websockets.connect(self.wss_endpoint, ping_interval=20, ping_timeout=60) as ws:
                    self.websocket = ws # Assign the connection object
                    logger.info("WebSocket connected. Subscribing...")
                    # Send subscription request
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1, # Use an ID for matching responses if needed
                        "method": "logsSubscribe",
                        # Subscribe to logs mentioning the pump.fun program ID
                        "params": [{"mentions": [self.program_id_str]}, {"commitment": "processed"}]
                    }))

                    # Wait for the subscription confirmation response
                    first_resp = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    resp_data = json.loads(first_resp)

                    # Check if subscription was successful
                    if resp_data.get("result") and isinstance(resp_data["result"], int):
                        self.subscription_id = resp_data["result"]
                        logger.info(f"Subscribed with ID: {self.subscription_id}")
                    elif resp_data.get("error"):
                        logger.error(f"Subscription failed: {resp_data['error']}")
                        await asyncio.sleep(5) # Wait before retrying connection
                        continue # Go back to the start of the while loop
                    else:
                        logger.error(f"Unexpected subscription response format: {first_resp}")
                        await asyncio.sleep(5)
                        continue

                    # Process messages received after successful subscription
                    async for message in ws:
                        if self._stop_event.is_set():
                            logger.debug("Stop event set, breaking message loop.")
                            break
                        # Process each message in a separate task to avoid blocking
                        # Create a task and add it to the set for potential management
                        task = asyncio.create_task(
                             self._process_log_notification(message, token_callback, match_string, creator_address)
                        )
                        self.tasks.add(task)
                        # Optional: Remove completed tasks to prevent memory leak
                        task.add_done_callback(self.tasks.discard)

            except websockets.exceptions.ConnectionClosedOK:
                logger.info("WebSocket connection closed normally.")
            except websockets.exceptions.ConnectionClosedError as e:
                logger.warning(f"WebSocket connection closed with error: {e}. Reconnecting...")
            except asyncio.TimeoutError:
                logger.warning("WebSocket connection or subscription timed out. Reconnecting...")
            except ConnectionRefusedError:
                 logger.error("WebSocket connection refused. Check endpoint/network. Retrying...")
            except Exception as e:
                # Catch other potential errors (JSON decoding, etc.)
                logger.error(f"Unexpected error in WebSocket listen loop: {type(e).__name__}: {e}. Reconnecting...", exc_info=False) # Less verbose logging for loop errors

            finally:
                # Cleanup before retry/exit
                self.websocket = None
                self.subscription_id = None
                if not self._stop_event.is_set():
                    logger.info("Waiting 5 seconds before attempting reconnection...")
                    await asyncio.sleep(5) # Wait before retrying connection

        logger.info("listen_for_tokens loop finished.")


    async def _process_log_notification(
        self,
        message: str,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: Optional[str],
        creator_address: Optional[str]
    ):
        """
        Processes a single log notification message.
        """
        try:
            data = json.loads(message)
            # Ensure it's a log notification
            if data.get("method") != "logsNotification":
                return

            # Extract relevant details safely using .get()
            params = data.get("params", {})
            result = params.get("result", {})
            value = result.get("value", {})
            logs: List[str] = value.get("logs", [])
            signature = value.get("signature")
            err = value.get("err")

            if err is not None:
                logger.debug(f"Skipping failed transaction: {signature}")
                return
            if not signature:
                logger.warning(f"Log notification missing signature: {value}")
                return

            # Check for the specific "Instruction: Create" log entry more robustly
            is_create_ix_log = False
            for log in logs:
                # Simple check, might need adjustment if format changes
                if "Program log: Instruction: Create" in log:
                    is_create_ix_log = True
                    break # Found it, no need to check further

            if is_create_ix_log:
                logger.info(f"Potential Create transaction detected: {signature}")
                # Call the processor to parse event data from logs
                token_info: Optional[TokenInfo] = await self.processor.process_create_event(logs, signature)

                if token_info:
                    # --- Apply Filters ---
                    name_symbol = (token_info.name + token_info.symbol).lower()
                    # Match string filter (case-insensitive)
                    if match_string and match_string.lower() not in name_symbol:
                        logger.debug(f"Token {token_info.symbol} filtered out by match string '{match_string}'.")
                        return
                    # Creator address filter (case-insensitive)
                    if creator_address and str(token_info.user).lower() != creator_address.lower():
                        logger.debug(f"Token {token_info.symbol} filtered out by creator address '{creator_address}'.")
                        return
                    # --- End Filters ---

                    logger.info(f"Invoking callback for token {token_info.symbol} ({token_info.mint})")
                    try:
                        await token_callback(token_info)
                    except Exception as cb_e:
                        logger.error(f"Error invoking token callback for {token_info.symbol}: {cb_e}", exc_info=True)
        except json.JSONDecodeError:
            logger.warning(f"Failed to decode JSON from WebSocket message: {message[:150]}...") # Show more context
        except Exception as e:
            logger.error(f"Error processing WebSocket message: {e}", exc_info=True)


    async def stop(self) -> None:
        """
        Signals the listener to stop and closes the WebSocket connection.
        """
        if not self._stop_event.is_set():
            logger.info("Stopping LogsListener...")
            self._stop_event.set()
            if self.websocket:
                logger.info("Closing WebSocket connection...")
                await self.websocket.close()
                self.websocket = None # Clear reference after closing
            logger.info("LogsListener stop signaled.")
        else:
            logger.info("LogsListener already stopping/stopped.")