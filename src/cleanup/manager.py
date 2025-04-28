# src/cleanup/manager.py (Corrected Imports)

import asyncio
from typing import List, Optional

# Use absolute imports
try:
    # --- FIX: Import PumpAddresses class ---
    from ..core.pubkeys import PumpAddresses
    # REMOVED: from ..core.pubkeys import TOKEN_PROGRAM_ID (direct import)
    # --- END FIX ---
    from ..core.client import SolanaClient # Client is now essential
    from ..core.wallet import Wallet # Wallet is likely needed to sign
    from ..core.transactions import build_and_send_transaction # Assuming this helper exists
    from ..core.priority_fee.manager import PriorityFeeManager # Needed for fees
    from ..utils.logger import get_logger

    from solders.pubkey import Pubkey
    from solders.instruction import Instruction
    # --- FIX: Import spl_token instructions correctly ---
    # Import the close_account instruction function
    from spl.token.instructions import close_account, CloseAccountParams
    # --- END FIX ---

except ImportError as e:
     print(f"ERROR importing in cleanup.manager: {e}")
     # Define dummies if necessary
     SolanaClient=object; Wallet=object; build_and_send_transaction=object; PriorityFeeManager=object; get_logger=lambda x: type('dummy', (object,), {'info':print, 'error':print, 'warning':print})(); Pubkey=object; Instruction=object; PumpAddresses=object; close_account=None; CloseAccountParams=None;

logger = get_logger(__name__)

class AccountCleanupManager:
    """ Manages closing associated token accounts. """

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        fee_manager: PriorityFeeManager
        ):
        self.client = client
        self.wallet = wallet
        self.fee_manager = fee_manager

    async def close_token_account(
        self,
        account_to_close: Pubkey,
        destination_wallet: Optional[Pubkey] = None, # Where SOL rent goes
        force: bool = False, # Whether to close even if balance > 0 (usually false)
        use_priority_fee: bool = True
        ) -> Optional[str]: # Returns Tx signature or None
        """ Builds and sends transaction to close a token account. """

        owner = self.wallet.pubkey
        destination = destination_wallet or owner # Default SOL destination to owner

        logger.info(f"Attempting to close token account: {account_to_close} for owner {owner}")

        # Basic check: Ensure it's not the owner's main SOL account
        if account_to_close == owner:
            logger.error("Cannot close the main wallet account.")
            return None

        # Optional: Check balance before closing if force=False
        if not force:
            try:
                balance_resp = await self.client.get_token_account_balance(account_to_close)
                if balance_resp and balance_resp.amount and int(balance_resp.amount) > 0:
                    logger.warning(f"Token account {account_to_close} has balance ({balance_resp.ui_amount_string}). Skipping close unless force=True.")
                    return None
            except Exception as e:
                 logger.warning(f"Could not verify token balance for {account_to_close} before closing: {e}")
                 # Proceed cautiously or return None depending on desired safety

        try:
            # --- FIX: Use constant from PumpAddresses and correct spl_token usage ---
            if close_account is None: # Check if import failed
                 logger.error("spl-token close_account instruction not available.")
                 return None

            ix = close_account(
                 CloseAccountParams(
                     program_id=PumpAddresses.TOKEN_PROGRAM_ID, # Use constant from class
                     account=account_to_close,
                     dest=destination,
                     owner=owner,
                     signers=[] # Owner signature added by build_and_send
                 )
            )
            # --- END FIX ---

            # Use the transaction helper
            tx_result = await build_and_send_transaction(
                client=self.client,
                wallet=self.wallet,
                instructions=[ix],
                fee_manager=self.fee_manager,
                use_priority_fee=use_priority_fee,
                skip_preflight=True # Often needed for closeAccount if balance isn't exactly 0
            )

            if tx_result.success:
                logger.info(f"Successfully closed ATA {account_to_close}. Transaction: {tx_result.signature}")
                return tx_result.signature
            else:
                logger.error(f"Failed to close ATA {account_to_close}: {tx_result.error_message}")
                return None

        except Exception as e:
            logger.error(f"Error building/sending close account tx for {account_to_close}: {e}", exc_info=True)
            return None


# Example Usage (Conceptual - Usually called from Wallet class or Trader)
# async def example():
#     # Assume client, wallet, fee_manager are initialized
#     cleanup_manager = AccountCleanupManager(client, wallet, fee_manager)
#     account_pubkey = Pubkey.from_string("...") # Pubkey of the ATA to close
#     await cleanup_manager.close_token_account(account_pubkey)