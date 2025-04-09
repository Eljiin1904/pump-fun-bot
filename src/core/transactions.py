# src/core/transactions.py

import asyncio
# import time # Unused
from dataclasses import dataclass
from typing import List, Optional, Sequence

# --- Imports for Compute Budget Functions ---
# No need for ComputeBudgetInstruction type hint if functions are used directly
# from solders.compute_budget import ComputeBudgetInstruction

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0 # Keep MessageV0, remove Message if unused elsewhere

# from solders.rpc.errors import SendTransactionError # Removed, using general Exception
# --- Corrected RPC Imports ---

# RpcResult is too generic, remove if not used
# from solders.rpc.responses import RpcResult
# --- Corrected Transaction Imports ---
from solders.transaction import VersionedTransaction # Keep VersionedTransaction
# Transaction removed as likely unused if always using VersionedTransaction
# from solders.transaction import Transaction
# --- Corrected Transaction Status Imports ---
# Import Commitment enum type
from solders.commitment_config import CommitmentLevel, CommitmentConfig # Use CommitmentLevel enum
from solders.transaction_status import TransactionConfirmationStatus # Keep Confirmation Status enum
# TransactionStatus removed as likely unused
# from solders.transaction_status import TransactionStatus

from .client import SolanaClient # Your client wrapper
from ..utils.logger import get_logger

logger = get_logger(__name__)

@dataclass
class TransactionResult:
    """Represents the outcome of sending a transaction."""
    success: bool
    signature: Optional[str] = None
    error_message: Optional[str] = None
    error_type: Optional[str] = None

async def get_latest_blockhash(client: SolanaClient) -> Optional[Hash]:
    """Fetches the latest blockhash using the client wrapper."""
    try:
        # Assuming client wrapper has get_latest_blockhash returning expected type
        resp = await client.get_latest_blockhash() # Call wrapper method
        if resp and resp.value and hasattr(resp.value, 'blockhash'):
            return resp.value.blockhash
        logger.error("Failed to get latest blockhash from client, response invalid.")
        return None
    except Exception as e:
        logger.error(f"Error fetching latest blockhash via client: {e}", exc_info=True)
        return None

async def build_and_send_transaction(
    client: SolanaClient, # Expecting your wrapper class instance
    payer: Keypair,
    instructions: Sequence[Instruction],
    signers: Optional[List[Keypair]] = None,
    label: str = "Transaction",
    confirm: bool = True,
    confirm_timeout_secs: int = 60,
    confirm_commitment: str = "confirmed", # Accept string input
    skip_preflight: bool = False,
    max_retries_sending: int = 3
) -> TransactionResult:
    """ Builds, signs, sends, and optionally confirms a Versioned Transaction. """
    if signers is None: signers = []
    all_signers = [payer] + [s for s in signers if s.pubkey() != payer.pubkey()]

    latest_blockhash = None
    for attempt in range(max_retries_sending):
        try:
            # 1. Get Latest Blockhash via wrapper
            latest_blockhash = await get_latest_blockhash(client)
            if not latest_blockhash:
                if attempt < max_retries_sending - 1: await asyncio.sleep(1); continue
                else: return TransactionResult(success=False, error_message="Failed to get blockhash", error_type="Build")

            # 2. Build Message
            msg = MessageV0.try_compile(
                payer=payer.pubkey(), instructions=instructions,
                address_lookup_table_accounts=[], recent_blockhash=latest_blockhash,
            )

            # 3. Create and Sign Transaction
            tx = VersionedTransaction(msg, all_signers)

            # 4. Send Transaction via wrapper
            logger.debug(f"{label}: Sending tx (attempt {attempt + 1}/{max_retries_sending})...")
            options = client.get_default_send_options(skip_preflight=skip_preflight)
            # Assuming client wrapper has send_transaction returning expected type
            send_resp = await client.send_transaction(tx, opts=options) # Call wrapper method

            if send_resp is None or not hasattr(send_resp, 'value') or send_resp.value is None:
                 error_message = f"Send tx attempt {attempt + 1} invalid response."; logger.warning(f"{label}: {error_message}")
                 if attempt < max_retries_sending - 1: await asyncio.sleep(1 + attempt); continue
                 else: return TransactionResult(success=False, error_message=error_message, error_type="SendError")

            signature_obj = send_resp.value; signature = str(signature_obj)
            logger.info(f"{label}: Sent. Sig: {signature}")

            # 5. Confirmation (Optional)
            if confirm:
                logger.debug(f"{label}: Confirming {signature} (timeout={confirm_timeout_secs}s, commitment={confirm_commitment})...")

                # --- Convert commitment string to CommitmentLevel enum ---
                try:
                    # Map common strings to the enum expected by solders/solana-py confirm
                    commitment_map = {
                        "processed": CommitmentLevel.Processed,
                        "confirmed": CommitmentLevel.Confirmed,
                        "finalized": CommitmentLevel.Finalized,
                        # Add other mappings if necessary (e.g., recent, single, max)
                    }
                    confirm_commitment_level: Optional[CommitmentLevel] = commitment_map.get(confirm_commitment.lower())
                    if confirm_commitment_level is None:
                         logger.warning(f"Invalid confirm_commitment string '{confirm_commitment}'. Using default.")
                         confirm_commitment_level = CommitmentLevel.Confirmed # Fallback default
                except Exception as e_commit:
                     logger.error(f"Error processing commitment level '{confirm_commitment}': {e_commit}. Using default.")
                     confirm_commitment_level = CommitmentLevel.Confirmed
                # --- End Conversion ---

                # Call wrapper confirm method
                confirmation_status = await client.confirm_transaction(
                    signature_obj, # Pass Signature object
                    commitment=confirm_commitment_level, # Pass CommitmentLevel enum
                    timeout_secs=confirm_timeout_secs,
                    # last_valid_block_height= # Get from blockhash response if needed
                )

                # --- Use str() for status name ---
                status_name = str(confirmation_status) if confirmation_status else "Unknown/Timeout"
                # --- End Use str() ---

                if confirmation_status in [TransactionConfirmationStatus.Confirmed, TransactionConfirmationStatus.Finalized]:
                    logger.info(f"{label}: CONFIRMED. Sig: {signature}, Status: {status_name}")
                    return TransactionResult(success=True, signature=signature)
                else:
                    error_msg = f"Tx {signature} sent but confirmation status is {status_name}."
                    logger.warning(f"{label}: {error_msg}")
                    return TransactionResult(success=False, signature=signature, error_message=error_msg, error_type="ConfirmTimeout")
            else:
                return TransactionResult(success=True, signature=signature)

        except Exception as e:
            error_message = f"Error build/send {label} attempt {attempt + 1}: {e}"
            logger.warning(f"{label}: {error_message}") # Log warning for retries
            error_str = str(e).lower()
            is_blockhash_error = "blockhash" in error_str or "expired" in error_str or "not found" in error_str
            if is_blockhash_error and attempt < max_retries_sending - 1: await asyncio.sleep(0.5); continue
            elif attempt < max_retries_sending - 1: await asyncio.sleep(1 + attempt); continue
            else:
                logger.error(f"{label}: Failed after {max_retries_sending} attempts: {e}", exc_info=True)
                error_type = "SendError" if 'signature' in locals() else "BuildError"
                return TransactionResult(success=False, error_message=str(e), error_type=error_type)

    logger.error(f"{label}: Fell through loop after {max_retries_sending} attempts.")
    return TransactionResult(success=False, error_message="Max retries reached.", error_type="SendError")