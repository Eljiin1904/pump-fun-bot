# src/cleanup/manager.py

import asyncio
from typing import List, Optional
from solders.pubkey import Pubkey
from solders.instruction import Instruction

# --- Corrected Import ---
# Import the class containing standard Solana addresses
from ..core.pubkeys import SolanaProgramAddresses
# --- End Correction ---

from ..core.client import SolanaClient
from ..core.wallet import Wallet
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..utils.logger import get_logger
from spl.token.instructions import close_account, CloseAccountParams

logger = get_logger(__name__)

class AccountCleanupManager:
    """Manages closing associated token accounts (ATAs)."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager
    ):
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        logger.info("AccountCleanupManager initialized.")

    async def close_accounts(
        self,
        mints_to_close: List[Pubkey],
        force_close: bool = False,
        use_priority_fee: bool = True
        ) -> List[TransactionResult]:
        """
        Attempts to close ATAs for the given mints owned by the wallet.

        Args:
            mints_to_close: A list of mint Pubkeys for which to close the ATA.
            force_close: If True, attempts closure even if balance might not be zero.
                         (Use with caution, SOL could be lost if balance > 0).
            use_priority_fee: Whether to add priority fee instructions.

        Returns:
            A list of TransactionResult objects, one for each attempt.
        """
        results: List[TransactionResult] = []
        logger.info(f"Attempting to close ATAs for {len(mints_to_close)} mint(s)...")

        for mint_pk in mints_to_close:
            ata_to_close = Wallet.get_associated_token_address(self.wallet.pubkey, mint_pk)
            logger.debug(f"Processing closure for Mint: {mint_pk}, ATA: {ata_to_close}")

            if not force_close:
                # Check balance before closing unless forced
                try:
                    balance_res = await self.client.get_token_account_balance(ata_to_close, commitment="confirmed")
                    if balance_res and hasattr(balance_res, 'amount') and int(balance_res.amount) > 0:
                        logger.warning(f"Skipping ATA closure for {mint_pk} ({ata_to_close}): Non-zero balance ({balance_res.ui_amount}) and force_close is False.")
                        results.append(TransactionResult(success=False, error_message="Skipped: Non-zero balance", error_type="Skip"))
                        continue
                    elif balance_res is None:
                         logger.warning(f"Could not confirm zero balance for ATA {ata_to_close}. Skipping closure as force_close is False.")
                         results.append(TransactionResult(success=False, error_message="Skipped: Balance check failed", error_type="Skip"))
                         continue
                    logger.debug(f"ATA {ata_to_close} confirmed zero balance or balance check skipped.")
                except Exception as e:
                    logger.error(f"Error checking balance for ATA {ata_to_close}: {e}. Skipping closure.")
                    results.append(TransactionResult(success=False, error_message=f"Balance check error: {e}", error_type="BalanceCheckError"))
                    continue

            # Build Close Instruction
            try:
                close_ix = close_account(
                    CloseAccountParams(
                        account=ata_to_close,
                        dest=self.wallet.pubkey, # Send reclaimed SOL to wallet owner
                        owner=self.wallet.pubkey,
                        program_id=SolanaProgramAddresses.TOKEN_PROGRAM_ID # Use constant
                    )
                )

                instructions = [close_ix]

                # Add priority fees if requested
                if use_priority_fee:
                     compute_unit_price = await self.priority_fee_manager.get_priority_fee()
                     # Placeholder limit for close account - usually low
                     compute_limit = 50_000
                     from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
                     instructions.insert(0, set_compute_unit_price(compute_unit_price))
                     instructions.insert(0, set_compute_unit_limit(compute_limit))


                # Send transaction
                logger.info(f"Sending transaction to close ATA: {ata_to_close} for mint {mint_pk}")
                tx_result = await build_and_send_transaction(
                    client=self.client,
                    payer=self.wallet.payer,
                    instructions=instructions,
                    label=f"CloseATA_{str(mint_pk)[:5]}"
                    # confirm=True, # build_and_send handles confirmation by default
                    # confirm_timeout_secs=60 # Pass if needed
                )
                results.append(tx_result)
                if tx_result.success:
                    logger.info(f"Successfully closed ATA {ata_to_close}. Tx: {tx_result.signature}")
                else:
                    logger.error(f"Failed to close ATA {ata_to_close}. Error: {tx_result.error_message}")

            except Exception as e:
                 logger.error(f"Unexpected error building/sending close instruction for ATA {ata_to_close}: {e}", exc_info=True)
                 results.append(TransactionResult(success=False, error_message=f"Build/Send error: {e}", error_type="BuildSendError"))

            # Add a small delay between closure attempts to avoid RPC limits
            await asyncio.sleep(0.5)

        return results