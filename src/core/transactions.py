# src/core/transactions.py

import asyncio
from dataclasses import dataclass
from typing import List, Optional, Sequence, Any

from solders.hash import Hash as Blockhash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus, TransactionStatus
from solders.commitment_config import CommitmentLevel, CommitmentConfig

from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed, Processed, Finalized # Keep specific levels if needed for TxOpts
from solana.exceptions import SolanaRpcException

from .client import SolanaClient, _to_commitment_level, _level_to_rpc_commitment, _level_to_status
from ..utils.logger import get_logger

logger = get_logger(__name__)

@dataclass
class TransactionResult:
    success: bool
    signature: Optional[str] = None
    error_message: Optional[str] = None
    error_type: Optional[str] = None

async def get_latest_blockhash(client: SolanaClient) -> Optional[Blockhash]:
    try:
        logger.debug("Fetching latest blockhash from client...")
        blockhash_obj: Optional[Blockhash] = await client.get_latest_blockhash(commitment=Confirmed) # Use Confirmed for blockhash
        if blockhash_obj: logger.debug(f"Got blockhash: {blockhash_obj}"); return blockhash_obj
        else: logger.error("Client returned None for latest blockhash."); return None
    except Exception as e: logger.error(f"Error fetching latest blockhash via client: {e}", exc_info=True); return None


async def build_and_send_transaction(
    client: SolanaClient,
    payer: Keypair,
    instructions: Sequence[Instruction],
    signers: Optional[List[Keypair]] = None,
    label: str = "Transaction",
    confirm: bool = True,
    confirm_timeout_secs: int = 60,
    confirm_commitment: str = "confirmed", # e.g., "processed", "confirmed", "finalized"
    skip_preflight: bool = False,
    max_retries_sending: int = 3, # Total attempts
    priority_fee_manager: Optional[Any] = None, # Keep for potential future use
    compute_units: Optional[int] = None # Keep for potential future use
) -> TransactionResult:
    """ Builds, signs, sends, and optionally confirms a Versioned Transaction with retries. """
    if signers is None: signers = []
    all_signers_set = {payer.pubkey()}; all_signers = [payer]
    all_signers.extend(s for s in signers if s.pubkey() not in all_signers_set and not all_signers_set.add(s.pubkey()))

    tx_signature_str: Optional[str] = None
    last_error: Optional[Exception] = None # Keep track of the last error

    for attempt in range(max_retries_sending):
        logger.info(f"{label}: Starting attempt {attempt + 1}/{max_retries_sending}...")
        try:
            # 1. Get Latest Blockhash (INSIDE loop)
            logger.debug(f"{label}: Getting blockhash for attempt {attempt + 1}...")
            latest_blockhash = await get_latest_blockhash(client)
            if not latest_blockhash:
                last_error = ValueError("Failed to get blockhash.") # Store error
                logger.warning(f"{label}: Failed to get blockhash on attempt {attempt + 1}.")
                if attempt < max_retries_sending - 1:
                    await asyncio.sleep(0.5 + attempt * 0.5) # Delay before retrying blockhash fetch
                    continue # Go to next iteration
                else:
                    logger.error(f"{label}: Failed to get blockhash after final attempt.")
                    break # Exit loop, will return failure outside

            # 2. Build Transaction
            tx_instructions = list(instructions)
            # Add priority/CU instructions here if needed dynamically
            msg = MessageV0.try_compile(payer=payer.pubkey(), instructions=tx_instructions, address_lookup_table_accounts=[], recent_blockhash=latest_blockhash)
            if msg is None:
                last_error = ValueError("Failed to compile tx message.")
                logger.error(f"{label}: {last_error}")
                # This is likely permanent, maybe don't retry? For now, let it try again.
                if attempt < max_retries_sending - 1: continue
                else: break # Exit loop if compile fails on last attempt

            tx = VersionedTransaction(msg, all_signers)

            # 3. Prepare Send Options
            try:
                 preflight_commitment_rpc = Confirmed
                 # --- FIX: Corrected e_commit reference ---
                 if confirm_commitment.lower() == "processed": preflight_commitment_rpc = Processed
                 elif confirm_commitment.lower() == "finalized": preflight_commitment_rpc = Finalized
                 # Set max_retries=0 as we handle retries here
                 opts = TxOpts(skip_preflight=skip_preflight, preflight_commitment=preflight_commitment_rpc, max_retries=0)
            except Exception as e_opts: # Use a different variable name for the exception
                 logger.error(f"Error creating TxOpts: {e_opts}. Using defaults."); opts = TxOpts(skip_preflight=skip_preflight, max_retries=0)

            # 4. Send Transaction
            logger.debug(f"{label}: Sending tx (attempt {attempt + 1})...")
            signature_obj: Optional[Signature] = await client.send_transaction(tx, opts=opts)

            if signature_obj is None:
                 # Error logged in client.send_transaction
                 # Raise an error to be caught by the outer except block for consistent retry logic
                 raise RuntimeError(f"Send tx attempt {attempt + 1} failed (RPC error or client issue).")

            # Send successful if we got here
            tx_signature_str = str(signature_obj)
            logger.info(f"{label}: Sent. Sig: {tx_signature_str}")

            # 5. Confirmation (Optional)
            if confirm:
                logger.debug(f"{label}: Confirming {tx_signature_str} (timeout={confirm_timeout_secs}s, commitment={confirm_commitment})...")
                try:
                    confirm_commitment_level = _to_commitment_level(confirm_commitment)
                except Exception as e_commit_level: # Catch specific error converting commitment
                    logger.error(f"Error processing confirm commitment '{confirm_commitment}': {e_commit_level}. Using Confirmed."); confirm_commitment_level = CommitmentLevel.Confirmed

                confirmation_result: Optional[TransactionStatus] = await client.confirm_transaction(signature_obj, commitment=confirm_commitment_level, timeout_secs=confirm_timeout_secs, sleep_secs=1.0)

                if confirmation_result and confirmation_result.err is None:
                     target_status_enum = _level_to_status(confirm_commitment_level)
                     current_status_enum = confirmation_result.confirmation_status
                     status_name = current_status_enum.name if current_status_enum else "Unknown"
                     if current_status_enum and current_status_enum >= target_status_enum:
                          logger.info(f"{label}: CONFIRMED. Sig: {tx_signature_str}, Status: {status_name}")
                          return TransactionResult(success=True, signature=tx_signature_str) # <<< SUCCESS
                     else:
                         # Reached timeout without desired status
                         err_msg = f"Tx confirmation status '{status_name}' did not reach target '{target_status_enum.name}' within timeout."
                         logger.warning(f"{label}: {err_msg}")
                         return TransactionResult(success=False, signature=tx_signature_str, error_message=err_msg, error_type="ConfirmTimeout")
                elif confirmation_result and confirmation_result.err:
                     err_msg = f"Tx {tx_signature_str} confirmed with error: {confirmation_result.err}"
                     logger.error(f"{label}: {err_msg}")
                     return TransactionResult(success=False, signature=tx_signature_str, error_message=err_msg, error_type="TxError")
                else: # Confirmation timed out in client.confirm_transaction
                    err_msg = "Confirmation timed out."
                    logger.warning(f"{label}: {err_msg}")
                    return TransactionResult(success=False, signature=tx_signature_str, error_message=err_msg, error_type="ConfirmTimeout")
            else: # Not confirming, send was successful
                return TransactionResult(success=True, signature=tx_signature_str) # <<< SUCCESS

        # --- Corrected except block ---
        except Exception as e:
            last_error = e # Store the actual exception
            logger.warning(f"{label}: Error during attempt {attempt + 1}: {type(e).__name__} - {e}")
            # --- Check if retries remain BEFORE sleeping ---
            if attempt < max_retries_sending - 1:
                # Check for specific retryable errors if needed (e.g., blockhash)
                error_str = str(e).lower()
                is_blockhash_error = "blockhash" in error_str and "not found" in error_str

                # Use exponential backoff for retries
                delay = 0.5 * (1.5 ** attempt)
                log_msg = "Blockhash error, retrying" if is_blockhash_error else f"Retrying {label} after error"
                logger.info(f"{log_msg} in {delay:.2f}s...")
                await asyncio.sleep(delay)
                # continue is implicit due to loop structure
            else:
                # Max retries reached after an error in this attempt
                logger.error(f"{label}: Failed on final attempt ({attempt + 1}) due to error: {last_error}", exc_info=isinstance(last_error, Exception))
                # Break the loop, failure details will be handled outside
                break
        # --- End corrected except block ---

    # --- Code after the loop finishes (only reached if all attempts failed or break occurred) ---
    final_error_msg = f"Failed after {max_retries_sending} attempts."
    error_type = "UnknownError" # Default
    if last_error:
        final_error_msg += f" Last error: ({type(last_error).__name__}) {str(last_error)}"
        # Basic error type classification based on last error
        if isinstance(last_error, ValueError) and ("blockhash" in str(last_error).lower() or "compile" in str(last_error).lower()): error_type = "BuildError"
        elif isinstance(last_error, RuntimeError) and "Send tx" in str(last_error): error_type = "SendError"
        elif isinstance(last_error, SolanaRpcException): error_type = "SendError" # RPC errors during send
        elif isinstance(last_error, TimeoutError): error_type = "ConfirmTimeout" # Should have returned inside, but catch just in case
        elif tx_signature_str: error_type = "SendConfirmError" # If sig exists but failed later (unlikely with current structure)
        else: error_type = "SendError" # Default if error occurred before sig obtained

    logger.error(f"{label}: {final_error_msg}")
    return TransactionResult(success=False, signature=tx_signature_str, error_message=final_error_msg, error_type=error_type)