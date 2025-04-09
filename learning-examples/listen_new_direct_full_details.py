# learning-examples/listen_new_direct_full_details.py

import asyncio
import base64
import json
import os
import struct
import sys
import websockets
from typing import Callable, Awaitable, Optional

# --- Add Project Root to Path for 'src.' imports ---
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# --- End Path Addition ---

from solders.pubkey import Pubkey

# --- Use Absolute Imports from src ---
try:
    # --- Corrected Import ---
    from src.core.pubkeys import PumpAddresses, SolanaProgramAddresses # Use correct class name
    # --- End Correction ---
    from src.core.client import SolanaClient # If needed later
    from src.trading.base import TokenInfo # If needed later
    from src.utils.logger import get_logger
    logger = get_logger(__name__)
except ImportError as e:
    import logging; logging.basicConfig(level=logging.INFO); logger = logging.getLogger(__name__)
    logger.error(f"Could not import modules from src: {e}.")
    # Define dummies only if script NEEDS to continue partially
    class PumpAddresses: PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
    class SolanaProgramAddresses: TOKEN_PROGRAM_ID=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"); ASSOCIATED_TOKEN_PROGRAM_ID=Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
# --- End Absolute Imports ---

# --- Load WSS endpoint ---
WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT", "wss://api.mainnet-beta.solana.com")

# --- Placeholder Create Instruction Parsing ---
# CRITICAL: Replace with actual verified discriminator and parsing logic
CREATE_INSTRUCTION_DISCRIMINATOR = base64.b64decode("GgpVz9kP9xA=") # Example - VERIFY!
def parse_create_instruction(data):
    # ... (Placeholder parsing logic - needs real implementation) ...
    if not data.startswith(CREATE_INSTRUCTION_DISCRIMINATOR): return None
    logger.warning("Using placeholder Create IX parser!")
    # ...
    return None # Return None until implemented


# --- Listener Logic ---
async def listen_for_new_tokens():
    # ... (Implementation remains the same as previous version) ...
    while True:
        try:
            if not WSS_ENDPOINT or "default_wss_here" in WSS_ENDPOINT or "api.mainnet-beta.solana.com" in WSS_ENDPOINT:
                 logger.error("Invalid WSS_ENDPOINT configured. Set SOLANA_NODE_WSS_ENDPOINT in .env"); break
            async with websockets.connect(WSS_ENDPOINT, ping_interval=20, ping_timeout=60) as websocket:
                subscription_message = json.dumps({ # ... subscription payload ...
                    "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                    "params": [{"mentions": [str(PumpAddresses.PROGRAM)]}, {"commitment": "processed"}],
                })
                await websocket.send(subscription_message); logger.info(f"Listening on {WSS_ENDPOINT} for {PumpAddresses.PROGRAM}"); await websocket.recv();
                while True:
                    try:
                        response = await websocket.recv(); data = json.loads(response);
                        if data.get("method") == "logsNotification":
                            log_data = data.get("params", {}).get("result", {}).get("value", {}); logs = log_data.get("logs", []);
                            is_create_log = any("Program log: Instruction: Create" in log for log in logs);
                            if is_create_log:
                                 program_data_log = next((log for log in logs if log.startswith("Program data:")), None);
                                 if program_data_log:
                                      try:
                                          encoded_data = program_data_log.split(": ")[1]; decoded_data = base64.b64decode(encoded_data);
                                          parsed_data = parse_create_instruction(decoded_data); # Uses placeholder
                                          if parsed_data:
                                              print("\n--- New Token Creation Detected ---"); print(f"Signature: {log_data.get('signature')}");
                                              for key, value in parsed_data.items(): print(f"  {key}: {value}"); print("-" * 30);
                                      except Exception as e: logger.error(f"Failed parse Program data log: {program_data_log}, Error: {e}");
                    except websockets.exceptions.ConnectionClosed: logger.warning("WS Closed."); break
                    except Exception as e: logger.error(f"Error processing message: {e}"); continue
        except Exception as e: logger.error(f"WS Connection error: {e}"); logger.info("Reconnecting in 5s..."); await asyncio.sleep(5)


if __name__ == "__main__":
    from dotenv import load_dotenv
    print("Loading .env variables for script...")
    load_dotenv()
    WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT", "") # Reload WSS
    if not WSS_ENDPOINT: logger.error("WSS Endpoint not found in environment variables.")
    else:
        try: asyncio.run(listen_for_new_tokens())
        except KeyboardInterrupt: print("\nListener interrupted by user.")