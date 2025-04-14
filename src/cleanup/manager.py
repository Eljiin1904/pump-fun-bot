# src/cleanup/manager.py

import asyncio
from typing import List, Optional, Any # Use Any for balance_response type hint

from solders.pubkey import Pubkey
# Removed incorrect TokenAmount import
from solders.signature import Signature
from solana.rpc.commitment import Confirmed
from spl.token.instructions import close_account, CloseAccountParams # Import SPL instructions here
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit # Import compute budget

from ..core.client import SolanaClient
from ..core.wallet import Wallet
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..core.priority_fee.manager import PriorityFeeManager
from ..utils.logger import get_logger
from ..core.pubkeys import TOKEN_PROGRAM_ID # Import constant

logger = get_logger(__name__)

class AccountCleanupManager:
    """ Manages closing associated token accounts. """

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        force_close: bool = False,
        use_priority_fee: bool = True
    ):
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.force_close = force_close
        self.use_priority_fee = use_priority_fee
        logger.info(f"AccountCleanupManager Init: ForceClose={self.force_close}, UsePrioFee={self.use_priority_fee}")

    async def close_ata_if_empty(self, mint_pubkey: Pubkey) -> bool:
        """ Closes the user's Associated Token Account (ATA) for a given mint if it's empty or forced. """
        user_ata = Wallet.get_associated_token_address(owner=self.wallet.pubkey, mint=mint_pubkey)
        logger.info(f"Attempting cleanup for ATA: {user_ata} (Mint: {mint_pubkey})")
        try:
            # 1. Check if ATA exists
            # Assuming get_account_info returns None if not found
            account_info = await self.client.get_account_info(user_ata, commitment="confirmed")
            if account_info is None:
                logger.info(f"ATA {user_ata} does not exist. No cleanup needed.")
                return True

            # 2. Check ATA balance
            # The client method returns the value object, which might be None or have attributes
            balance_response: Optional[Any] = await self.client.get_token_account_balance(user_ata, commitment="confirmed")
            balance = 0
            # --- FIX: Check attributes instead of type ---
            if balance_response and hasattr(balance_response, 'amount') and hasattr(balance_response, 'ui_amount_string'):
                # Attempt to parse the string amount
                try:
                    balance = int(balance_response.amount)
                except (ValueError, TypeError):
                    logger.warning(f"Cleanup: Could not parse balance amount '{balance_response.amount}' for {user_ata}. Assuming 0.")
                    balance = 0
            elif balance_response is not None: # Response received but didn't have expected attrs
                 logger.warning(f"Cleanup: Unexpected balance response structure for {user_ata}: {balance_response}")
            # If balance_response is None, balance remains 0

            logger.info(f"ATA {user_ata} balance: {balance}")
            if balance > 0 and not self.force_close:
                logger.warning(f"ATA {user_ata} non-zero balance ({balance}), force_close=False. Skipping.")
                return False

            # 3. Build close instruction
            logger.info(f"Proceeding with closing ATA {user_ata}.")

            close_ix = close_account(
                CloseAccountParams(
                    program_id=TOKEN_PROGRAM_ID,
                    account=user_ata,
                    dest=self.wallet.pubkey, # Funds returned to owner
                    owner=self.wallet.pubkey
                )
            )

            instructions = []
            if self.use_priority_fee:
                 try:
                     # Use the get_fee method as defined in PriorityFeeManager
                     fee = await self.priority_fee_manager.get_fee()
                     if fee is None: fee = 0 # Default to 0 if fee calculation fails
                     logger.debug(f"Cleanup using priority fee: {fee} microLamports")
                     instructions.append(set_compute_unit_price(int(fee)))
                     # Set a reasonable limit for closing ATA
                     instructions.append(set_compute_unit_limit(80_000)) # Increased limit slightly
                 except Exception as fee_e:
                     logger.error(f"Failed to get/set priority fee for cleanup: {fee_e}. Proceeding without.")

            instructions.append(close_ix)

            # 4. Send transaction
            label = f"CloseATA_{str(mint_pubkey)[:5]}"
            tx_result: TransactionResult = await build_and_send_transaction(
                client=self.client, payer=self.wallet.payer, instructions=instructions, label=label
            )

            if tx_result.success and tx_result.signature:
                logger.info(f"Close ATA transaction sent: {tx_result.signature}. Confirming...")
                signature_obj = Signature.from_string(tx_result.signature)
                confirmed_status = await self.client.confirm_transaction(
                    signature_obj, commitment=Confirmed, timeout_secs=60
                )
                if confirmed_status and confirmed_status >= Confirmed:
                    logger.info(f"Close ATA transaction CONFIRMED: {tx_result.signature}")
                    return True
                else:
                    logger.warning(f"Close ATA tx confirmation timed out/failed: {tx_result.signature}, Status: {confirmed_status}")
                    return False
            else:
                logger.error(f"Failed to send Close ATA transaction: {tx_result.error_message or 'Unknown error'}")
                return False
        except Exception as e:
            logger.error(f"Error during ATA cleanup for mint {mint_pubkey} (ATA: {user_ata}): {e}", exc_info=True)
            return False

    async def cleanup_multiple_accounts(self, mint_pubkeys: List[Pubkey]):
        """ Attempts to clean up ATAs for a list of mints. """
        # ... (Implementation remains the same) ...
        if not mint_pubkeys: logger.info("No accounts provided for cleanup."); return
        logger.info(f"Starting cleanup for {len(mint_pubkeys)} potential accounts.")
        results = await asyncio.gather(*[self.close_ata_if_empty(mint) for mint in mint_pubkeys], return_exceptions=True)
        success_count = sum(1 for r in results if r is True)
        fail_count = sum(1 for r in results if r is False)
        exception_count = sum(1 for r in results if isinstance(r, Exception))
        logger.info(f"Cleanup finished. Success: {success_count}, Failed/Skipped: {fail_count}, Exceptions: {exception_count}")
        for i, result in enumerate(results):
            if isinstance(result, Exception): logger.error(f"Exception during cleanup for mint {mint_pubkeys[i]}: {result}")