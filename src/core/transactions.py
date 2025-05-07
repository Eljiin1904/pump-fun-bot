# src/core/transactions.py

import asyncio
from dataclasses import dataclass
from typing import List, Optional, Sequence, Any

from solders.hash import Hash as Blockhash  # Keep for type hint Blockhash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature  # Keep for type hint Signature
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus, TransactionStatus, \
    UiTransactionEncoding  # Added UiTransactionEncoding
from solders.commitment_config import CommitmentLevel  # Keep for type hint CommitmentLevel

from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed, Processed, Finalized
from solana.exceptions import SolanaRpcException

from .client import SolanaClient  # Assuming _to_commitment_level, etc. are part of SolanaClient or helpers
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TransactionSendResult:  # Renamed from TransactionResult to avoid conflict if Solana has one
    success: bool
    signature: Optional[str] = None
    error_message: Optional[str] = None
    error_type: Optional[str] = None  # e.g., BuildError, SendError, ConfirmTimeout, TxError
    # Add raw_error if you want to store the exception object
    raw_error: Optional[Any] = None


async def get_latest_blockhash_from_client(client: SolanaClient) -> Optional[Blockhash]:  # Renamed for clarity
    """Fetches the latest blockhash using the provided SolanaClient."""
    try:
        # logger.debug("Fetching latest blockhash from client...")
        # Use Confirmed for blockhash as it needs to be recent and recognized by the cluster
        blockhash_resp = await client.get_latest_blockhash(commitment=Confirmed)
        if blockhash_resp and blockhash_resp.value:  # Check .value for the RpcResponseContext
            # logger.debug(f"Got blockhash: {blockhash_resp.value.blockhash}")
            return blockhash_resp.value.blockhash
        else:
            logger.error("Client returned None or no value for latest blockhash.")
            return None
    except Exception as e:
        logger.error(f"Error fetching latest blockhash via client: {e}", exc_info=True)
        return None


# --- NEW UTILITY ---
async def get_transaction_fee(client: SolanaClient, signature_str: str) -> Optional[int]:
    """
    Fetches the fee for a confirmed transaction by its signature string.
    Returns fee in lamports, or None if not found or error.
    """
    logger.debug(f"Fetching transaction details for fee: {signature_str}")
    try:
        # Convert string signature to solders.Signature if your client.get_transaction expects it.
        # Most modern solana-py/solders methods can handle string signatures directly.
        # tx_signature = Signature.from_string(signature_str) # If conversion is needed

        tx_details_resp = await client.get_transaction(
            signature_str,  # Pass as string
            encoding=UiTransactionEncoding.JSON,  # JSON is sufficient for meta.fee
            max_supported_transaction_version=0  # Use 0 for wide compatibility
        )

        if tx_details_resp and \
                hasattr(tx_details_resp, 'meta') and tx_details_resp.meta and \
                hasattr(tx_details_resp.meta, 'fee') and tx_details_resp.meta.fee is not None:
            logger.debug(f"Fee for tx {signature_str}: {tx_details_resp.meta.fee} lamports")
            return tx_details_resp.meta.fee
        else:
            logger.warning(f"Could not find fee information in transaction meta for {signature_str}.")
            if tx_details_resp and hasattr(tx_details_resp, 'meta'):
                logger.debug(f"Transaction meta content for {signature_str}: {tx_details_resp.meta}")
            elif tx_details_resp:
                logger.debug(f"Transaction response for {signature_str} (no meta): {tx_details_resp}")
            else:
                logger.debug(f"No transaction response for {signature_str}")
            return None
    except SolanaRpcException as rpc_e:
        # It's possible the transaction is not found or RPC has issues
        logger.warning(f"SolanaRpcException fetching transaction fee for {signature_str}: {rpc_e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching transaction fee for {signature_str}: {e}", exc_info=True)
        return None


# --- END NEW UTILITY ---


async def build_and_send_transaction(
        client: SolanaClient,
        payer: Keypair,
        instructions: Sequence[Instruction],
        signers: Optional[List[Keypair]] = None,
        label: str = "Transaction",
        confirm: bool = True,
        confirm_timeout_secs: int = 60,
        confirm_commitment_str: str = "confirmed",  # Changed name for clarity
        skip_preflight: bool = False,
        max_send_retries: int = 3,  # Renamed from max_retries_sending
        # priority_fee_manager and compute_units are not used in this version of the function
        # but kept as comments if you plan to integrate them directly here later.
        # priority_fee_manager: Optional[Any] = None,
        # compute_units: Optional[int] = None
) -> TransactionSendResult:  # Use the renamed dataclass
    """ Builds, signs, sends, and optionally confirms a Versioned Transaction with retries. """
    if signers is None:
        signers_to_use = [payer]
    else:
        # Ensure payer is the first signer and no duplicates
        signers_to_use = [payer]
        seen_pubkeys = {payer.pubkey()}
        for s in signers:
            if s.pubkey() not in seen_pubkeys:
                signers_to_use.append(s)
                seen_pubkeys.add(s.pubkey())

    tx_signature_str: Optional[str] = None
    last_error_for_result: Optional[Any] = None  # Store the actual exception object

    for attempt in range(max_send_retries):
        logger.info(f"{label}: Build/Send attempt {attempt + 1}/{max_send_retries}...")
        try:
            latest_blockhash = await get_latest_blockhash_from_client(client)
            if not latest_blockhash:
                last_error_for_result = ValueError("Failed to get blockhash.")
                logger.warning(f"{label}: {last_error_for_result} on attempt {attempt + 1}.")
                if attempt < max_send_retries - 1:
                    await asyncio.sleep(0.5 + attempt * 0.5)
                    continue
                else:
                    logger.error(f"{label}: Failed to get blockhash after final attempt.")
                    break

            tx_instructions = list(instructions)  # Ensure it's a mutable list

            # Dynamic priority fees should be added to tx_instructions *before* this point
            # by the caller (e.g., TokenBuyer/TokenSeller) if using a fee_manager.

            compiled_message = MessageV0.try_compile(
                payer=payer.pubkey(),
                instructions=tx_instructions,
                address_lookup_table_accounts=[],  # Not using LUTs in this version
                recent_blockhash=latest_blockhash
            )
            if compiled_message is None:  # Should ideally not happen with valid inputs
                last_error_for_result = ValueError(
                    "Failed to compile transaction message (MessageV0.try_compile returned None).")
                logger.error(f"{label}: {last_error_for_result}")
                if attempt < max_send_retries - 1:
                    continue
                else:
                    break

            tx = VersionedTransaction(compiled_message, signers_to_use)

            # Prepare Send Options from solana.rpc.types.TxOpts
            # The commitment levels for preflight are different from confirmation status.
            # Typically, preflight uses 'confirmed' or 'processed'.
            preflight_commitment_level: CommitmentLevel
            if confirm_commitment_str.lower() == "processed":
                preflight_commitment_level = Processed
            elif confirm_commitment_str.lower() == "finalized":
                preflight_commitment_level = Finalized
            else:
                preflight_commitment_level = Confirmed  # Default

            opts = TxOpts(
                skip_preflight=skip_preflight,
                preflight_commitment=preflight_commitment_level,  # Use CommitmentLevel enum
                max_retries=0  # We handle retries in this loop
            )

            # logger.debug(f"{label}: Sending tx (attempt {attempt + 1}) with opts: skip_preflight={opts.skip_preflight}, preflight_commitment={opts.preflight_commitment.name if opts.preflight_commitment else 'Default'}")
            signature_obj: Optional[Signature] = await client.send_transaction_logic(tx,
                                                                                     opts=opts)  # Assuming send_transaction_logic is the robust method in your client

            if signature_obj is None:
                # Error should have been logged within client.send_transaction_logic
                # This indicates a failure at the RPC send stage (e.g. network, RPC node error)
                last_error_for_result = RuntimeError(
                    f"Send tx attempt {attempt + 1} returned no signature (RPC send error).")
                logger.warning(f"{label}: {last_error_for_result}")
                # Let the loop retry if attempts remain
                if attempt < max_send_retries - 1:
                    continue
                else:
                    break  # Exit loop if send fails on last attempt

            tx_signature_str = str(signature_obj)
            logger.info(f"{label}: Sent successfully. Signature: {tx_signature_str}")

            if not confirm:
                return TransactionSendResult(success=True, signature=tx_signature_str)

            # Confirmation Logic
            logger.debug(
                f"{label}: Confirming {tx_signature_str} (timeout={confirm_timeout_secs}s, commitment='{confirm_commitment_str}')...")

            # Convert string commitment to CommitmentLevel for client.confirm_transaction
            try:
                target_confirm_commitment_level: CommitmentLevel = CommitmentLevel[confirm_commitment_str.capitalize()]
            except KeyError:
                logger.warning(f"Invalid confirm_commitment_str '{confirm_commitment_str}'. Defaulting to Confirmed.")
                target_confirm_commitment_level = Confirmed

            # Assuming client.confirm_transaction_logic is the robust method
            confirmation_status_obj: Optional[TransactionStatus] = await client.confirm_transaction_logic(
                signature_obj,
                commitment=target_confirm_commitment_level,
                timeout_secs=confirm_timeout_secs,
                sleep_secs=1.0  # Polling interval
            )

            if confirmation_status_obj and confirmation_status_obj.err is None:
                # Check if confirmation_status matches or exceeds target_confirm_commitment_level
                # The TransactionStatus object contains confirmation_status: Optional[TransactionConfirmationStatus]
                current_rpc_status = confirmation_status_obj.confirmation_status
                status_name = current_rpc_status.name if current_rpc_status else "StatusNotAvailable"

                # Comparing CommitmentLevel enums directly
                if current_rpc_status and current_rpc_status >= target_confirm_commitment_level:
                    logger.info(
                        f"{label}: CONFIRMED. Sig: {tx_signature_str}, Status: {status_name} (target: {target_confirm_commitment_level.name})")
                    return TransactionSendResult(success=True, signature=tx_signature_str)
                else:
                    err_msg = f"Tx {tx_signature_str} confirmation status '{status_name}' did not reach target '{target_confirm_commitment_level.name}' within timeout."
                    logger.warning(f"{label}: {err_msg}")
                    # Return success=False but with signature, as it was sent but not confirmed to desired level
                    return TransactionSendResult(success=False, signature=tx_signature_str, error_message=err_msg,
                                                 error_type="ConfirmTimeout")
            elif confirmation_status_obj and confirmation_status_obj.err:
                err_msg = f"Tx {tx_signature_str} confirmed WITH ON-CHAIN ERROR: {confirmation_status_obj.err}"
                logger.error(f"{label}: {err_msg}")
                return TransactionSendResult(success=False, signature=tx_signature_str,
                                             error_message=str(confirmation_status_obj.err), error_type="TxError",
                                             raw_error=confirmation_status_obj.err)
            else:  # confirmation_status_obj is None (e.g., client.confirm_transaction_logic indicated timeout before any status)
                err_msg = f"Tx {tx_signature_str} confirmation timed out (no status object returned)."
                logger.warning(f"{label}: {err_msg}")
                return TransactionSendResult(success=False, signature=tx_signature_str, error_message=err_msg,
                                             error_type="ConfirmTimeout")

        except SolanaRpcException as e_rpc:  # Catch specific RPC errors during send/compile
            last_error_for_result = e_rpc
            logger.warning(f"{label}: SolanaRpcException during attempt {attempt + 1}: {e_rpc}")
            # Check for blockhash not found specifically
            if "blockhash not found" in str(e_rpc).lower() or \
                    (hasattr(e_rpc, 'args') and e_rpc.args and "Blockhash not found" in str(e_rpc.args[0])):
                logger.info(f"{label}: Blockhash error detected. Will retry if attempts remain.")
            # Other RPC errors might also be retryable
        except Exception as e_general:  # Catch other unexpected errors
            last_error_for_result = e_general
            logger.error(
                f"{label}: Unexpected error during attempt {attempt + 1}: {type(e_general).__name__} - {e_general}",
                exc_info=True)

        # If we are here, an error occurred in the try block for this attempt
        if attempt < max_send_retries - 1:
            delay = 0.75 * (1.5 ** attempt)  # Exponential backoff
            logger.info(
                f"{label}: Retrying after error in {delay:.2f}s... (Last error type: {type(last_error_for_result).__name__})")
            await asyncio.sleep(delay)
        else:  # Max retries reached for this attempt loop
            logger.error(
                f"{label}: Failed on final build/send attempt ({attempt + 1}). Last error type: {type(last_error_for_result).__name__}")
            break  # Exit the loop

    # After all attempts, if we haven't returned success:
    final_error_msg_str = f"Failed after {max_send_retries} attempts."
    error_type_str = "UnknownSendError"
    if last_error_for_result:
        final_error_msg_str += f" Last error: ({type(last_error_for_result).__name__}) {str(last_error_for_result)}"
        # More specific error typing based on the exception
        if isinstance(last_error_for_result, ValueError) and (
                "blockhash" in str(last_error_for_result).lower() or "compile" in str(last_error_for_result).lower()):
            error_type_str = "BuildError"
        elif isinstance(last_error_for_result, SolanaRpcException):
            error_type_str = "RpcSendError"
        elif isinstance(last_error_for_result, RuntimeError) and "Send tx" in str(last_error_for_result):
            error_type_str = "SendError"
        else:
            error_type_str = type(last_error_for_result).__name__

    logger.error(f"{label}: {final_error_msg_str}")
    return TransactionSendResult(
        success=False,
        signature=tx_signature_str,  # Signature might exist if send was successful but confirm failed
        error_message=final_error_msg_str,
        error_type=error_type_str,
        raw_error=last_error_for_result
    )