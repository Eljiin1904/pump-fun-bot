# src/monitoring/logs_event_processor.py

import asyncio
import base64  # Import standard base64 library
import time
from typing import Optional, Callable, Any, Dict, Coroutine

from solders.pubkey import Pubkey

# Import base58 decode function - ensure 'based58' library is installed
try:
    from based58 import b58decode
except ImportError:
    def b58decode(val):
        raise NotImplementedError("based58 library not found. `pip install based58`")

# Borsh imports
from construct import Bytes as ConstructBytes
from borsh_construct import CStruct, String

# Local Imports
from src.utils.logger import get_logger
from src.trading.base import TokenInfo
from src.core.client import SolanaClient
from src.core.pubkeys import SolanaProgramAddresses, PumpAddresses

logger = get_logger(__name__)

# --- "Create" Event Log Structure (IDL Verified) ---
CREATE_EVENT_LOG_LAYOUT = CStruct(
    "name" / String,
    "symbol" / String,
    "uri" / String,
    "mint_bytes" / ConstructBytes(32),
    "bonding_curve_bytes" / ConstructBytes(32),
    "user_bytes" / ConstructBytes(32)
)

# Discriminator for the "Create" event:
CREATE_EVENT_DISCRIMINATOR = bytes.fromhex("1b72a94ddeeb6376")


class LogsEventProcessor:
    """
    Processes WebSocket log notifications to extract pump.fun 'Create' events.
    Handles both Base64 and Base58 encoded log data.
    """

    def __init__(self, client: SolanaClient):
        self.client = client
        logger.info(f"Using Create Event Discriminator: {CREATE_EVENT_DISCRIMINATOR.hex()}")

    async def process_log_notification_result(
        self,
        notification_result: Dict[str, Any],
        token_callback: Callable[[TokenInfo], Coroutine[Any, Any, None]]
    ) -> None:
        log_result = notification_result.get('value')
        if not isinstance(log_result, dict):
            return

        logs = log_result.get('logs')
        if not isinstance(logs, list):
            return

        signature_str = str(log_result.get('signature', 'N/A'))
        slot = notification_result.get('context', {}).get('slot')

        for log_entry in logs:
            if not isinstance(log_entry, str) or not log_entry.startswith("Program data: "):
                continue

            encoded_data_str = log_entry.split("Program data: ", 1)[1]
            raw_log_data: Optional[bytes] = None

            # --- ATTEMPT DECODING: Base64 first, then Base58 ---
            try:
                raw_log_data = base64.b64decode(encoded_data_str, validate=True)
            except Exception:
                try:
                    raw_log_data = b58decode(encoded_data_str.encode('ascii'))
                    logger.warning(f"Decoded via Base58 fallback in tx {signature_str}")
                except Exception:
                    continue
            # --- END DECODING ---

            if raw_log_data is None or not raw_log_data.startswith(CREATE_EVENT_DISCRIMINATOR):
                continue

            payload = raw_log_data[len(CREATE_EVENT_DISCRIMINATOR):]
            try:
                evt = CREATE_EVENT_LOG_LAYOUT.parse(payload)
            except Exception as e:
                logger.error(f"Parse error tx {signature_str}: {e}")
                logger.debug(f"Payload hex: {payload.hex()}")
                continue

            name = evt.name.rstrip("\x00 ")
            symbol = evt.symbol.rstrip("\x00 ")
            uri = evt.uri.rstrip("\x00 ")
            mint_pubkey = Pubkey(evt.mint_bytes)
            bonding_curve_pubkey = Pubkey(evt.bonding_curve_bytes)
            creator_pubkey = Pubkey(evt.user_bytes)

            # Fetch decimals by parsing the mint account
            decimals = await self._fetch_decimals(mint_pubkey, signature_str)
            if decimals is None:
                logger.warning(f"Skipping token {symbol}: cannot determine decimals")
                continue

            # Derive curve's associated token account PDA
            assoc_curve_ata = self._derive_assoc_curve_ata(mint_pubkey)

            creation_ts = int(slot) if slot is not None else int(time.time())

            token_info = TokenInfo(
                mint=mint_pubkey,
                name=name,
                symbol=symbol,
                uri=uri,
                creator=creator_pubkey,
                bonding_curve_address=bonding_curve_pubkey,
                associated_bonding_curve_address=assoc_curve_ata,
                decimals=decimals,
                token_program_id=SolanaProgramAddresses.TOKEN_PROGRAM_ID,
                creation_timestamp=creation_ts
            )

            logger.info(
                f"Processed 'Create' event for {symbol} ({mint_pubkey})"
            )
            await token_callback(token_info)
            return

    async def _fetch_decimals(self, mint_pubkey: Pubkey, signature_str: str) -> Optional[int]:
        """
        Fetches the mint account and parses the 'decimals' byte (offset 44)
        from the SPL Token Mint layout.
        """
        try:
            # Small delay to allow RPC indexers
            await asyncio.sleep(0.1)

            resp = await self.client.get_account_info(
                mint_pubkey,
                encoding="base64"
            )
            acct = getattr(resp, "value", None)
            if not acct or not isinstance(acct.data, list):
                return None

            data_base64 = acct.data[0]
            raw = base64.b64decode(data_base64)
            # In SPL Mint layout, decimals is the u8 at offset 44
            return raw[44]
        except Exception as e:
            logger.error(
                f"Exception fetching/parsing mint account for {mint_pubkey} (tx {signature_str}): {e}"
            )
            return None

    def _derive_assoc_curve_ata(self, mint_pubkey: Pubkey) -> Pubkey:
        pda, _ = Pubkey.find_program_address(
            [b"token", bytes(mint_pubkey)],
            PumpAddresses.PROGRAM_ID
        )
        return pda

    def verify_create_event_discriminator(self, sample_b58_str: str) -> None:
        pass  # optional helper
