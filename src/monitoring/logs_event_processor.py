# src/monitoring/logs_event_processor.py (Correct v2/v3 Layout & has process_log_entry)

import base64
import time
from typing import Optional, Dict, Any

# Use absolute imports
try:
    from solders.pubkey import Pubkey
    # --- Import layout components matching main-v2/v3 commit ---
    from borsh_construct import CStruct
    from construct import Bytes, PascalString, Int32ul, Adapter, ConstructError
    # --- End Import ---
    from ..core.client import SolanaClient
    from ..trading.base import TokenInfo # Assuming TokenInfo is defined correctly
    from ..utils.logger import get_logger
except ImportError as e:
    print(f"ERROR importing in logs_event_processor: {e}")
    # Dummies (keep for safety during init)
    Pubkey=type('DummyPubkey', (object,), {'__init__': lambda s,b: None}); CStruct=lambda *x: type('DummyCStruct', (object,), {'parse': lambda s,d: object(), 'sizeof': 0})() ; Bytes=lambda x: object(); PascalString=lambda x,y: object(); Int32ul=object(); SolanaClient=object; TokenInfo=object; get_logger=lambda x: type('dummy', (object,), {'info':print, 'error':print, 'warning':print, 'debug':print})(); Adapter=object; ConstructError=Exception

logger = get_logger(__name__)

# --- Create Event Discriminator ---
CREATE_EVENT_DISCRIMINATOR = bytes.fromhex("1b72a94ddeeb6376")
logger.info(f"Using Create Event Discriminator: {CREATE_EVENT_DISCRIMINATOR.hex()}")

# --- Create Event Layout (Based STRICTLY on main-v2/v3 commit) ---
# Defined at module level, ensuring imports above are correct.
try:
    CREATE_EVENT_LAYOUT = CStruct(
        "name" / PascalString(Int32ul, "utf8"),
        "symbol" / PascalString(Int32ul, "utf8"),
        "uri" / PascalString(Int32ul, "utf8"),
        "mint" / Bytes(32),             # Fixed 32 bytes for Pubkey
        "bonding_curve" / Bytes(32),    # Fixed 32 bytes for Pubkey
        "user" / Bytes(32)              # Fixed 32 bytes for Pubkey (Creator is named 'user')
    )
    logger.info(f"LogsEventProcessor.CREATE_EVENT_LAYOUT defined successfully.")
except NameError as e_name:
    logger.error(f"FATAL: NameError defining CREATE_EVENT_LAYOUT: {e_name}. Check construct/borsh_construct imports.", exc_info=True)
    CREATE_EVENT_LAYOUT = None
except Exception as e_layout:
    logger.error(f"FATAL: Error defining CREATE_EVENT_LAYOUT: {e_layout}.", exc_info=True)
    CREATE_EVENT_LAYOUT = None

class LogsEventProcessor:
    def __init__(self, client: SolanaClient): # Expects SolanaClient
        self.client = client
        logger.info("LogsEventProcessor initialized.")
        if CREATE_EVENT_LAYOUT is None:
            logger.error("LogsEventProcessor initialized WITHOUT a valid CREATE_EVENT_LAYOUT.")

    # --- METHOD RESTORED ---
    def process_log_entry(self, log_entry: Dict[str, Any]) -> Optional[TokenInfo]:
        """ Processes a single log entry using the correct layout. """
        if CREATE_EVENT_LAYOUT is None: return None

        log_messages = log_entry.get("logs", [])
        signature = log_entry.get("signature")
        if not log_messages: return None
        create_event_data_b64: Optional[str] = None
        for msg in log_messages:
            if msg.startswith("Program data:"):
                parts = msg.split("Program data:", 1);
                if len(parts) > 1: data_part = parts[1].strip();
                if data_part: create_event_data_b64 = data_part; break
        if not create_event_data_b64: return None

        try:
            event_data_bytes = base64.b64decode(create_event_data_b64)
            if not event_data_bytes.startswith(CREATE_EVENT_DISCRIMINATOR): return None
            data_to_parse = event_data_bytes[len(CREATE_EVENT_DISCRIMINATOR):]

            parsed_event = CREATE_EVENT_LAYOUT.parse(data_to_parse)

            user_pk = Pubkey(getattr(parsed_event, "user")) if hasattr(parsed_event, "user") else None
            mint_pk = Pubkey(getattr(parsed_event, "mint")) if hasattr(parsed_event, "mint") else None
            bonding_curve_pk = Pubkey(getattr(parsed_event, "bonding_curve")) if hasattr(parsed_event, "bonding_curve") else None

            token_info = TokenInfo(
                name=getattr(parsed_event, "name", "UNKNOWN"),
                symbol=getattr(parsed_event, "symbol", "UNK"),
                uri=getattr(parsed_event, "uri", ""),
                creator=user_pk, # Map 'user' field to 'creator'
                mint=mint_pk,
                bonding_curve=bonding_curve_pk,
                timestamp=time.time()
            )

            if not all([token_info.creator, token_info.mint, token_info.bonding_curve]):
                logger.warning(f"Parsed event {signature} missing Pubkeys. User:{user_pk}, Mint:{mint_pk}, Curve:{bonding_curve_pk}")
                return None

            # Log success within listener after queuing
            return token_info

        except ConstructError as e: logger.error(f"Construct error log {signature}: {e}. Layout: {CREATE_EVENT_LAYOUT}, Data(len={len(data_to_parse)}): {data_to_parse[:64].hex()}..."); return None
        except base64.binascii.Error as e: logger.error(f"Base64 decode error log {signature}: {e}. Data: {create_event_data_b64}"); return None
        except Exception as e: logger.error(f"Unexpected error processing log {signature}: {e}", exc_info=True); return None
    # --- END METHOD RESTORED ---

# --- END OF FILE ---