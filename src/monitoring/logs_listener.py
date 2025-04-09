# src/monitoring/logs_listener.py

import asyncio
import json
import websockets # Ensure websockets is imported
from typing import Optional, Callable, Awaitable

from solders.pubkey import Pubkey
from solders.signature import Signature
from solana.rpc.commitment import Confirmed, Commitment

from .base_listener import BaseTokenListener, TokenInfo
from ..core.client import SolanaClient
from ..core.pubkeys import PumpAddresses
from .logs_event_processor import LogsEventProcessor
from ..utils.logger import get_logger

logger = get_logger(__name__)

class LogsListener(BaseTokenListener):
    # ... (__init__ remains the same as the last full version provided) ...
    def __init__(self, wss_endpoint: str, client: SolanaClient):
        self.wss_endpoint = wss_endpoint; self.program_id = PumpAddresses.PROGRAM; self.program_id_str = str(self.program_id);
        self.solana_client = client; self.processor = LogsEventProcessor(self.solana_client);
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None; self.subscription_id: Optional[int] = None;
        logger.info(f"LogsListener initialized for WSS: {wss_endpoint}")

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: Optional[str] = None,
        creator_address: Optional[str] = None
    ) -> None:
        """Listens for new tokens using WebSocket logs subscription."""
        while True:
            e_for_finally: Optional[Exception] = None # Initialize e before try
            try:
                logger.info(f"Attempting WebSocket connection to {self.wss_endpoint}...")
                async with websockets.connect(
                    self.wss_endpoint, ping_interval=20, ping_timeout=60
                ) as ws:
                    self.websocket = ws
                    logger.info("WebSocket connected. Subscribing to logs...")
                    await ws.send(json.dumps({ # Subscription payload
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": [self.program_id_str]}, {"commitment": "processed"}]
                    }))
                    first_resp = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    resp_data = json.loads(first_resp)
                    # ... (Subscription confirmation logic remains the same) ...
                    if resp_data.get("result") and isinstance(resp_data["result"], int): self.subscription_id = resp_data["result"]; logger.info(f"Sub ID: {self.subscription_id}")
                    elif resp_data.get("error"): logger.error(f"Sub fail: {resp_data['error']}"); await asyncio.sleep(5); continue
                    else: logger.error(f"Unexpected sub resp: {first_resp}"); await asyncio.sleep(5); continue

                    async for message in ws: # Listen loop
                        await self._process_log_notification(message, token_callback, match_string, creator_address)

            # --- Capture exception info for finally block ---
            except websockets.exceptions.ConnectionClosedOK: logger.info("WebSocket closed normally."); e_for_finally=None # No error
            except websockets.exceptions.ConnectionClosedError as e: logger.error(f"WebSocket closed error: {e}. Reconnecting..."); e_for_finally=e
            except asyncio.TimeoutError as e: logger.warning("WebSocket connection/sub timeout. Reconnecting..."); e_for_finally=e
            except websockets.exceptions.InvalidURI as e: logger.critical(f"Invalid WSS URI: {self.wss_endpoint}. Stopping."); e_for_finally=e; break # Stop retrying
            except websockets.exceptions.WebSocketException as e: logger.error(f"WebSocket exception: {e}. Reconnecting..."); e_for_finally=e
            except Exception as e: logger.error(f"Unexpected WS loop error: {e}. Reconnecting...", exc_info=True); e_for_finally=e
            # --- End Capture ---
            finally:
                 self.websocket = None; self.subscription_id = None
                 # --- Check captured exception 'e_for_finally' ---
                 # Avoid immediate tight loops on persistent errors like Invalid URI
                 if e_for_finally is None or not isinstance(e_for_finally, websockets.exceptions.InvalidURI):
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5) # Wait before attempting reconnect
                 # --- End Check ---

    # ... (_process_log_notification method remains the same) ...
    async def _process_log_notification( # ... (Implementation as before) ...
        self, message: str, token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: Optional[str], creator_address: Optional[str]
    ):
        try:
            data = json.loads(message);
            if data.get("method") != "logsNotification": return;
            log_details = data.get("params", {}).get("result", {}).get("value", {}); logs = log_details.get("logs", []); signature = log_details.get("signature"); err = log_details.get("err");
            if err is not None: return;
            if not signature: logger.warning(f"Log missing signature: {log_details}"); return;
            is_create_tx = any("Program log: Instruction: Create" in log for log in logs);
            if is_create_tx:
                logger.info(f"Potential Create tx: {signature}");
                token_info: Optional[TokenInfo] = await self.processor.extract_token_info_from_tx(signature);
                if token_info:
                    logger.debug(f"Extracted info for {token_info.symbol} ({token_info.mint})");
                    name_symbol = (token_info.name + token_info.symbol).lower();
                    if match_string and match_string.lower() not in name_symbol: logger.info(f"Filter out {token_info.symbol}: Name/Symbol ('{match_string}')"); return;
                    if creator_address and str(token_info.user).lower() != creator_address.lower(): logger.info(f"Filter out {token_info.symbol}: Creator ('{creator_address}')"); return;
                    logger.info(f"Invoking callback for {token_info.symbol} ({token_info.mint})");
                    await token_callback(token_info);
        except json.JSONDecodeError: logger.warning(f"Failed JSON decode WS: {message[:100]}...")
        except Exception as e: logger.error(f"Error processing WS msg: {e}", exc_info=True)

    # ... (stop method remains the same) ...
    async def stop(self): # ... (Implementation as before) ...
        logger.info("Stopping listener...");
        if self.websocket:
            try: await self.websocket.close()
            except Exception as e: logger.error(f"Error closing websocket: {e}")
        self.websocket = None; self.subscription_id = None; logger.info("Listener WS closed.")