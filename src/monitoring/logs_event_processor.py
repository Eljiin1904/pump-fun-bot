# src/monitoring/logs_event_processor.py

import base64
import time
import asyncio # <-- Keep asyncio import for sleep
from typing import Optional, List, Dict, Any

from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction, Transaction
from solders.transaction_status import EncodedConfirmedTransactionWithStatusMeta, UiTransactionEncoding
# --- Import Exceptions for Retry ---
from solana.exceptions import SolanaRpcException
from httpx import ReadTimeout, ConnectTimeout, HTTPStatusError, RequestError
# --- End Import Exceptions ---

# Imports from project
from ..core.pubkeys import SolanaProgramAddresses, PumpAddresses
from ..core.client import SolanaClient
from ..trading.base import TokenInfo
from ..utils.logger import get_logger

logger = get_logger(__name__)

# --- Placeholders - Needs Verification ---
# CRITICAL: Replace these with values verified from explorer/IDL
CREATE_IX_DISCRIMINATOR_PLACEHOLDER = b'\x1a\xc2\x0a\xca\x91\x89\x96\xd7' # Example "global:create" - VERIFY!
MINT_INDEX_IN_CREATE = 0 # VERIFY!
BONDING_CURVE_INDEX_IN_CREATE = 1 # VERIFY!
ASSOC_BONDING_CURVE_INDEX_IN_CREATE = 2 # VERIFY! SOL Vault
CREATOR_INDEX_IN_CREATE = 10 # VERIFY!
# --- End Placeholders ---

# --- Constants for Retry Logic ---
GET_TX_RETRIES = 3
GET_TX_INITIAL_DELAY = 0.5 # Initial delay in seconds
GET_TX_BACKOFF_FACTOR = 2   # Factor to increase delay each retry (e.g., 0.5, 1.0, 2.0)
# --- End Constants ---

class LogsEventProcessor:
    """Processes transaction details fetched based on log notifications."""

    def __init__(self, client: SolanaClient):
        self.client_wrapper = client
        try:
            self.async_client = client.get_async_client() # Get async client via wrapper method
            if self.async_client is None: raise AttributeError("get_async_client() returned None")
        except AttributeError: logger.critical("FATAL: Missing get_async_client() on SolanaClient!"); raise
        except Exception as e: logger.critical(f"FATAL: Error getting async client: {e}", exc_info=True); raise

    async def extract_token_info_from_tx(self, signature: str) -> Optional[TokenInfo]:
        """
        Fetches transaction details by signature with retries, and attempts to parse
        pump.fun 'Create' instruction details to extract token information.
        """
        logger.debug(f"Attempting extraction for sig: {signature}")
        sig_obj = Signature.from_string(signature)
        tx_details: Optional[EncodedConfirmedTransactionWithStatusMeta] = None
        last_error: Optional[Exception] = None

        # --- Retry Loop for get_transaction ---
        for attempt in range(GET_TX_RETRIES):
            try:
                logger.debug(f"get_transaction attempt {attempt + 1}/{GET_TX_RETRIES} for {signature}")
                tx_resp = await self.async_client.get_transaction(
                    sig_obj,
                    max_supported_transaction_version=0,
                    commitment="confirmed",
                    encoding="base64" # Use base64 encoding
                )

                if tx_resp is not None and tx_resp.value is not None:
                    tx_details = tx_resp.value
                    logger.debug(f"get_transaction successful for {signature} on attempt {attempt + 1}")
                    break # Exit loop on success
                else:
                    # Log specific error if available in response, otherwise generic invalid
                    rpc_error = tx_resp.error if tx_resp and hasattr(tx_resp, 'error') else "Invalid/Null Response"
                    logger.warning(f"get_transaction attempt {attempt + 1} invalid response for {signature}. Error: {rpc_error}")
                    # Decide if retryable based on error? For now, retry invalid response once or twice.
                    if attempt < GET_TX_RETRIES - 1:
                        delay = GET_TX_INITIAL_DELAY * (GET_TX_BACKOFF_FACTOR ** attempt)
                        logger.warning(f"Retrying after {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue # Go to next attempt
                    else:
                        logger.error(f"Failed get_transaction after {GET_TX_RETRIES} attempts (invalid response): {signature}")
                        return None # Failed after retries

            # --- Catch Specific Retryable Errors ---
            except (SolanaRpcException, ReadTimeout, ConnectTimeout, RequestError, HTTPStatusError) as e:
                last_error = e # Store last error
                logger.warning(f"get_transaction attempt {attempt + 1} failed for {signature} with {type(e).__name__}: {e}.")
                if attempt < GET_TX_RETRIES - 1:
                    delay = GET_TX_INITIAL_DELAY * (GET_TX_BACKOFF_FACTOR ** attempt)
                    logger.warning(f"Retrying after {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue # Go to next attempt
                else:
                    logger.error(f"Failed get_transaction after {GET_TX_RETRIES} attempts ({type(e).__name__}): {signature}")
                    return None # Failed after retries

            # Catch other unexpected errors during fetch attempt
            except Exception as e:
                 last_error = e # Store last error
                 logger.error(f"Unexpected error in get_transaction attempt {attempt+1} for {signature}: {e}", exc_info=True)
                 # Stop retrying on unexpected errors
                 return None
        # --- End Retry Loop ---

        # Check if tx_details was successfully populated after loop
        if tx_details is None:
            logger.error(f"Failed to get valid transaction details for {signature} after all retries. Last error: {last_error}")
            return None

        # --- Proceed with Processing if tx_details is valid ---
        try:
            tx = tx_details.transaction
            if tx is None: logger.warning(f"Response missing tx details for {signature}"); return None
            meta = tx.meta

            if meta is None or meta.err is not None: logger.debug(f"Tx {signature} failed (err={meta.err}) or no meta."); return None

            inner_tx = tx.transaction
            if isinstance(inner_tx, VersionedTransaction): message = inner_tx.message
            elif isinstance(inner_tx, Transaction): message = inner_tx.message
            else: logger.error(f"Unexpected tx type {signature}: {type(inner_tx)}"); return None

            # --- Find 'Create' IX ---
            create_ix_details = None; pump_program_id_str = str(PumpAddresses.PROGRAM)
            for ix in message.instructions:
                # Added check for length before indexing account_keys
                if ix.program_id_index < len(message.account_keys):
                    prog_id = message.account_keys[ix.program_id_index]
                    if str(prog_id) == pump_program_id_str and ix.data.startswith(CREATE_IX_DISCRIMINATOR_PLACEHOLDER): # Use actual discriminator
                        logger.debug(f"Found potential Create IX in {signature}"); create_ix_details = ix; break
            if not create_ix_details: logger.debug(f"No Create IX found in {signature}"); return None

            # --- Extract accounts --- (VERIFY INDICES!)
            try:
                 # Added check for accounts list length
                 if len(create_ix_details.accounts) <= max(MINT_INDEX_IN_CREATE, BONDING_CURVE_INDEX_IN_CREATE, ASSOC_BONDING_CURVE_INDEX_IN_CREATE, CREATOR_INDEX_IN_CREATE):
                      logger.error(f"Instruction accounts length mismatch for Create IX {signature}. Check indices.")
                      return None
                 # Access accounts using verified indices
                 mint_pk = message.account_keys[create_ix_details.accounts[MINT_INDEX_IN_CREATE]]
                 bonding_curve_pk = message.account_keys[create_ix_details.accounts[BONDING_CURVE_INDEX_IN_CREATE]]
                 assoc_bc_pk = message.account_keys[create_ix_details.accounts[ASSOC_BONDING_CURVE_INDEX_IN_CREATE]]
                 creator_pk = message.account_keys[create_ix_details.accounts[CREATOR_INDEX_IN_CREATE]]
                 logger.debug(f"Extracted Accounts: Mint={mint_pk}, Curve={bonding_curve_pk}, Vault={assoc_bc_pk}, Creator={creator_pk}")
            except IndexError: logger.error(f"Account index OOB parsing Create IX {signature}. Check layout/indices."); return None
            except Exception as e: logger.error(f"Error extracting accounts {signature}: {e}", exc_info=True); return None

            # --- Extract metadata --- (Placeholder - NEEDS IMPLEMENTATION!)
            name = f"NAME_{str(mint_pk)[:4]}"; symbol = f"SYM_{str(mint_pk)[-3:]}"; uri = f"http://placeholder.com/{mint_pk}"
            logger.warning(f"Using placeholder metadata extraction for {signature}.")
            # Add actual argument parsing logic here using struct.unpack or borsh

            logger.info(f"Successfully extracted basic info for mint {mint_pk} from tx {signature}")
            block_time = tx.block_time if tx.block_time else time.time()
            return TokenInfo(
                 name=name, symbol=symbol, uri=uri, mint=mint_pk,
                 bonding_curve=bonding_curve_pk, associated_bonding_curve=assoc_bc_pk,
                 user=creator_pk, created_timestamp=block_time
            )
        except Exception as e: # Catch errors during processing *after* getting tx details
            logger.error(f"Unexpected error processing tx details for {signature}: {e}", exc_info=True)
            return None