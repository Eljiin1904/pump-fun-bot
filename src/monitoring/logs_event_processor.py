# src/monitoring/logs_event_processor.py
import asyncio
import time
from typing import Callable, Any, Coroutine
from solders.pubkey import Pubkey
from solders.rpc.responses import LogsNotification
from solders.transaction_status import UiTransactionEncoding
from based58 import b58decode
# --- Corrected Construct Imports ---
from construct import Bytes  # Import directly from construct for fixed-size bytes
from borsh_construct import CStruct, String  # Keep String from borsh_construct
# --- End Correction ---

from src.utils.logger import get_logger
from src.trading.base import TokenInfo
from src.core.client import SolanaClient
from src.core.pubkeys import SolanaProgramAddresses, PumpAddresses

logger = get_logger(__name__)

# --- "Create" Event Log Structure (IDL Verified) ---
# Using construct.Bytes for fixed-size pubkeys
CREATE_EVENT_LOG_LAYOUT = CStruct(
    "name" / String,  # Variable length string
    "symbol" / String,  # Variable length string
    "uri" / String,  # Variable length string
    "mint_bytes" / Bytes(32),  # Fixed 32 bytes for Pubkey
    "bonding_curve_bytes" / Bytes(32),  # Fixed 32 bytes for Pubkey
    "user_bytes" / Bytes(32)  # Fixed 32 bytes for Pubkey
)

# Discriminator for the "Create" event:
CREATE_EVENT_DISCRIMINATOR = bytes.fromhex("1b72a94ddeeb6376")


class LogsEventProcessor:
    def __init__(self, client: SolanaClient):
        self.client = client
        logger.info(f"Using Create Event Discriminator: {CREATE_EVENT_DISCRIMINATOR.hex()}")
        # Size calculation might be less accurate now with mixed types, but still indicative
        # logger.info(f"Expected Create Event Log Layout size: ~{CREATE_EVENT_LOG_LAYOUT.sizeof()} (excl. dynamic strings)")

    # ... (rest of the class, process_log_notification, and verify_create_event_discriminator
    #      remain exactly the same as the previous version) ...
    async def process_log_notification(
            self,
            notification: LogsNotification,
            token_callback: Callable[[TokenInfo], Coroutine[Any, Any, None]]
    ) -> None:
        if not isinstance(notification, LogsNotification): return
        val = getattr(notification.result, 'value', None)
        if val is None or not getattr(val, 'logs', None): return
        signature = getattr(val, 'signature', 'N/A')
        for log_entry in val.logs:
            if not log_entry.startswith("Program data: "): continue
            try:
                b58data = log_entry.split("Program data: ")[1]
                raw = b58decode(b58data)
                if not raw.startswith(CREATE_EVENT_DISCRIMINATOR): continue
                payload = raw[len(CREATE_EVENT_DISCRIMINATOR):]
                try:
                    evt = CREATE_EVENT_LOG_LAYOUT.parse(payload)
                except Exception as e:
                    logger.error(f"Failed parsing Create payload tx {signature}: {e}"); continue

                name = evt.name.strip('\x00 ');
                symbol = evt.symbol.strip('\x00 ');
                uri = evt.uri.strip('\x00 ')
                mint_pubkey = Pubkey(evt.mint_bytes);
                bonding_curve = Pubkey(evt.bonding_curve_bytes);
                creator = Pubkey(evt.user_bytes)

                mint_info = await self.client.get_mint_info(mint_pubkey)
                if not mint_info or mint_info.value is None: logger.warning(
                    f"Cannot fetch mint info for {mint_pubkey} tx {signature}"); continue
                decimals = mint_info.value.decimals

                assoc_curve_ata, _ = Pubkey.find_program_address([b"token", bytes(mint_pubkey)],
                                                                 PumpAddresses.PROGRAM_ID)

                ts = int(time.time());
                block_time = getattr(val, 'blockTime', None)
                if block_time is not None: ts = int(block_time)
                # Optional: Add fallback tx fetch for timestamp if needed

                info = TokenInfo(
                    mint=mint_pubkey, name=name, symbol=symbol, uri=uri, creator=creator,
                    bonding_curve_address=bonding_curve, associated_bonding_curve_address=assoc_curve_ata,
                    decimals=decimals, token_program_id=SolanaProgramAddresses.TOKEN_PROGRAM_ID,
                    creation_timestamp=ts)

                logger.info(f"Processed 'Create' event for mint {mint_pubkey} (symbol={symbol})")
                await token_callback(info)
                return  # Process first match
            except Exception as exc:
                logger.error(f"Error processing log entry tx {signature}: {exc}", exc_info=True); continue


def verify_create_event_discriminator(sample_b58: str) -> None:
    # ... (helper function remains the same) ...
    try:
        raw = b58decode(sample_b58)
    except Exception as e:
        logger.error(f"Failed to decode base58 sample: {e}"); return
    actual = raw[: len(CREATE_EVENT_DISCRIMINATOR)]
    logger.info(f"Actual discriminator: {actual.hex()}")
    if actual != CREATE_EVENT_DISCRIMINATOR:
        logger.error(f"Discriminator mismatch: expected {CREATE_EVENT_DISCRIMINATOR.hex()}, got {actual.hex()}")
    else:
        logger.info("CREATE_EVENT_DISCRIMINATOR verified successfully.")