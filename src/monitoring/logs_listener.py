# src/monitoring/logs_listener.py (Cleaned - Uses Processor Correctly)

import asyncio
import json
from typing import Optional

from solders.pubkey import Pubkey
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
from websockets.client import connect as ws_connect

try:
    from .base_listener import BaseTokenListener
    from .logs_event_processor import LogsEventProcessor # Import the processor
    from ..core.client import SolanaClient
    from ..core.pubkeys import PumpAddresses
    from ..utils.logger import get_logger
except ImportError as e: print(f"ERROR importing in logs_listener: {e}"); # Dummies...

logger = get_logger(__name__)

class LogsListener(BaseTokenListener):
    def __init__(self, client: SolanaClient, wss_endpoint: str):
        super().__init__(client)
        self.wss_endpoint = wss_endpoint
        try:
            self.processor = LogsEventProcessor(client=self.client) # Initialize processor
        except Exception as e_proc_init: logger.error(f"FATAL: Failed init LogsEventProcessor: {e_proc_init}", exc_info=True); raise RuntimeError("Failed init LogsEventProcessor") from e_proc_init
        logger.info(f"LogsListener initialized for WSS: {self.wss_endpoint}")

    async def _listen(self, token_queue: asyncio.Queue, match_string: Optional[str] = None, creator_address: Optional[str] = None):
        logger.info(f"LogsListener _listen started. Filters - Match: '{match_string}', Creator: '{creator_address}'")
        subscribe_request = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe", "params": [{"mentions": [str(PumpAddresses.PROGRAM)]}, {"commitment": "processed"}]}

        while not self._stop_event.is_set(): # Check stop event from base class
            try:
                logger.info(f"Connecting to WebSocket: {self.wss_endpoint}...")
                async with ws_connect(self.wss_endpoint, ping_interval=30, ping_timeout=60, create_protocol=None) as websocket:
                    logger.info("WS connected. Subscribing..."); await websocket.send(json.dumps(subscribe_request));
                    first_resp_data = json.loads(await asyncio.wait_for(websocket.recv(), timeout=15))
                    if 'result' in first_resp_data and isinstance(first_resp_data['result'], int): logger.info(f"Subscribed. ID: {first_resp_data['result']}")
                    elif 'error' in first_resp_data: raise ConnectionAbortedError(f"Subscription fail: {first_resp_data['error']}")
                    else: logger.warning(f"Sub response unexpected: {first_resp_data}")
                    logger.info("Listening for logs...")
                    while not self._stop_event.is_set(): # Check stop event in inner loop too
                        try:
                            message = await websocket.recv()
                            data = json.loads(message)
                            if data.get("method") == "logsNotification":
                                log_entry = data.get("params", {}).get("result", {})
                                if not log_entry: continue
                                try:
                                    # --- Call the processor method ---
                                    token_info = self.processor.process_log_entry(log_entry)
                                    # --- End Call ---
                                    if token_info:
                                        # Apply Filters (ensure case-insensitive match)
                                        passes = True
                                        if match_string and not (match_string.lower() in token_info.name.lower() or match_string.lower() in token_info.symbol.lower()): passes = False
                                        if creator_address and str(token_info.creator) != creator_address: passes = False
                                        if passes: logger.debug(f"Queueing {token_info.symbol} ({token_info.mint})"); await token_queue.put(token_info)
                                except Exception as e_proc: logger.error(f"Error processing log entry {log_entry.get('signature')}: {e_proc}", exc_info=True)
                        except asyncio.CancelledError: logger.info("WS inner loop cancelled."); raise
                        except (ConnectionClosed, ConnectionClosedError, ConnectionClosedOK) as e_closed: logger.warning(f"WS closed: {e_closed}. Reconnecting..."); break
                        except json.JSONDecodeError as e_json: logger.error(f"WS JSON decode error: {e_json}. Msg: {message[:200]}...");
                        except Exception as e_inner: logger.error(f"WS inner loop error: {e_inner}", exc_info=True); await asyncio.sleep(1)
            except asyncio.CancelledError: logger.info("WS task cancelled."); break
            except Exception as e_outer: logger.error(f"WS outer loop error: {e_outer}", exc_info=True)
            if not self._stop_event.is_set(): wait_time = 10; logger.info(f"Attempting WS reconnect after {wait_time}s..."); await asyncio.sleep(wait_time)
        logger.info("LogsListener _listen finished.")

# --- END OF FILE ---