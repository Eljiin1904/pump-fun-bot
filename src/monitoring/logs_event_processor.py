# src/monitoring/logs_event_processor.py

import base64
import time
import hashlib
from typing import Optional, Dict, Any, List

# Imports
from solders.pubkey import Pubkey
from httpx import ReadTimeout

try:
    from borsh_construct import CStruct
    from construct import Bytes, Int32ul, PascalString, ConstructError
    BORSH_IMPORTED_SUCCESSFULLY = True
except ImportError as e:
    print(f"CRITICAL: Failed borsh/construct imports in {__name__}. Install/Fix. Error: {e}")
    BORSH_IMPORTED_SUCCESSFULLY = False
    CStruct = PascalString = Int32ul = Bytes = object
    ConstructError = Exception

# Project Imports
from ..core.client import SolanaClient
from ..core.curve import BondingCurveManager
from ..trading.base import TokenInfo
from ..utils.logger import get_logger

logger = get_logger(__name__)

CREATE_EVENT_DISCRIMINATOR = hashlib.sha256(b"event:CreateEvent").digest()[:8]
logger.info(f"Using Create Event Discriminator: {CREATE_EVENT_DISCRIMINATOR.hex()}")

class LogsEventProcessor:
    """Processes event data from logs and fetches related account details."""

    CREATE_EVENT_LAYOUT: Optional[Any] = None
    if BORSH_IMPORTED_SUCCESSFULLY:
        try:
            CREATE_EVENT_LAYOUT = CStruct(
                "name" / PascalString(Int32ul, "utf8"),
                "symbol" / PascalString(Int32ul, "utf8"),
                "uri" / PascalString(Int32ul, "utf8"),
                "mint" / Bytes(32),
                "bonding_curve" / Bytes(32),
                "user" / Bytes(32)
            )
            logger.info("LogsEventProcessor.CREATE_EVENT_LAYOUT defined successfully.")
        except Exception as e:
            logger.critical(f"Failed to define LogsEventProcessor.CREATE_EVENT_LAYOUT: {e}", exc_info=True)
            CREATE_EVENT_LAYOUT = None
    else:
        logger.critical("Cannot define LogsEventProcessor.CREATE_EVENT_LAYOUT due to missing imports.")

    def __init__(self, client: SolanaClient):
        self.client_wrapper = client
        try:
            self.curve_manager = BondingCurveManager(client)
        except Exception as e:
            logger.critical(f"FATAL: Error initializing LogsEventProcessor: {e}", exc_info=True)
            raise
        logger.info("LogsEventProcessor initialized.")

    # --- FIX: Added signature parameter for logging ---
    def _parse_create_event_log_data(self, signature: str, log_data_base64: str) -> Optional[Dict[str, Any]]:
    # --- End Fix ---
        """Decodes and deserializes Create event data, converting pubkey bytes."""
        if self.CREATE_EVENT_LAYOUT is None:
            logger.error(f"Cannot parse event for {signature}: CREATE_EVENT_LAYOUT is invalid.")
            return None
        try:
            log_data_bytes = base64.b64decode(log_data_base64)
            if not log_data_bytes.startswith(CREATE_EVENT_DISCRIMINATOR):
                return None

            data_args = log_data_bytes[8:]

            # --- Add basic length check ---
            min_expected_size = 32 + 32 + 32
            if len(data_args) < min_expected_size:
                 logger.warning(f"Skipping parse for {signature}: data length {len(data_args)} seems too short for layout.")
                 return None
            # --- End length check ---

            parsed_event_raw = self.CREATE_EVENT_LAYOUT.parse(data_args)
            # logger.info( f"Parsed Create event raw for {signature}: Name='{parsed_event_raw.get('name')}', Symbol='{parsed_event_raw.get('symbol')}'" )

            parsed_event_final = {}
            parsed_event_final["name"] = parsed_event_raw.get("name")
            parsed_event_final["symbol"] = parsed_event_raw.get("symbol")
            parsed_event_final["uri"] = parsed_event_raw.get("uri")
            mint_bytes = parsed_event_raw.get("mint")
            curve_bytes = parsed_event_raw.get("bonding_curve")
            user_bytes = parsed_event_raw.get("user")

            if not all([mint_bytes, curve_bytes, user_bytes]):
                 logger.error(f"Essential byte fields missing after parsing for {signature}. Raw: {parsed_event_raw}")
                 return None

            parsed_event_final["mint"] = Pubkey(mint_bytes)
            parsed_event_final["bonding_curve"] = Pubkey(curve_bytes)
            parsed_event_final["user"] = Pubkey(user_bytes)

            return parsed_event_final
        except ConstructError as e:
            logger.warning(f"Borsh construct parse event error for {signature} (data likely doesn't match layout - maybe different event?): {e}")
            logger.debug(f"Problematic data (hex) for {signature}: {data_args.hex() if 'data_args' in locals() else 'N/A'}")
            return None
        except base64.binascii.Error as e:
            logger.error(f"Base64 decode error for {signature}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected parse event error for {signature}: {e}", exc_info=True)
            return None

    async def _fetch_sol_vault_address(self, mint_pubkey: Pubkey) -> Optional[Pubkey]:
        """ Computes the SOL vault address (associated bonding curve PDA). """
        try:
            assoc_bc_pk, _ = BondingCurveManager.find_associated_bonding_curve_pda(mint_pubkey)
            return assoc_bc_pk
        except Exception as e:
            logger.error(f"Error computing associated bonding curve PDA for mint {mint_pubkey}: {e}", exc_info=True)
            return None

    async def process_create_event(self, logs: List[str], signature: str) -> Optional[TokenInfo]:
        """ Finds a Create event log, parses it, computes SOL vault, returns TokenInfo. """
        logger.debug(f"Processing create event logs for signature: {signature}")
        program_data_log: Optional[str] = None; create_ix_log_index = -1
        try:
            create_ix_log_index = next(i for i, log in enumerate(logs) if "Program log: Instruction: Create" in log)
            program_data_log = next((log for log in logs[create_ix_log_index+1:] if log.startswith("Program data:")), None)
        except StopIteration: return None
        if not program_data_log: return None

        try:
            parts = program_data_log.split(": ");
            if len(parts) < 2: logger.error(f"Could not split 'Program data:' log line for {signature}. Log: '{program_data_log}'"); return None
            encoded_data = parts[1].strip()
            # Pass signature for better logging
            parsed_event_data = self._parse_create_event_log_data(signature, encoded_data)
            if not parsed_event_data: return None

            mint_pk = parsed_event_data.get("mint"); bonding_curve_pda = parsed_event_data.get("bonding_curve"); creator_pk = parsed_event_data.get("user")
            name = parsed_event_data.get("name", "N/A"); symbol = parsed_event_data.get("symbol", "N/A"); uri = parsed_event_data.get("uri", "")
            if not all([mint_pk, bonding_curve_pda, creator_pk]): logger.error(f"Essential Pubkeys missing after parsing for {signature}. Parsed: {parsed_event_data}"); return None

            assoc_bc_pk = await self._fetch_sol_vault_address(mint_pk)
            if not assoc_bc_pk: logger.error(f"Failed to compute associated bonding curve PDA for mint {mint_pk} ({signature})"); return None

            logger.info(f"Successfully processed create event for mint {mint_pk}")
            creation_timestamp = time.time()
            return TokenInfo( name=name, symbol=symbol, uri=uri, mint=mint_pk, bonding_curve=bonding_curve_pda, associated_bonding_curve=assoc_bc_pk, user=creator_pk, created_timestamp=creation_timestamp )
        except Exception as e: logger.error(f"Error processing create event {signature}: {e}", exc_info=True); return None