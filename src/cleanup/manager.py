# src/cleanup/manager.py
import asyncio
from typing import Optional

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction
from solana.rpc.commitment import Confirmed  # Need Confirmed for checks

# --- Corrected Imports (Use Absolute from src) ---
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.instruction_builder import InstructionBuilder
from src.core.transactions import build_and_send_transaction, TransactionSendResult  # Use correct result name
from src.core.priority_fee.manager import PriorityFeeManager
from src.core.pubkeys import SolanaProgramAddresses
from src.utils.logger import get_logger

# --- End Corrected Imports ---

logger = get_logger(__name__)


class AccountCleanupManager:
    """Manages closing of Associated Token Accounts (ATAs)."""

    def __init__(self, client: SolanaClient, wallet: Wallet, fee_manager: PriorityFeeManager):
        """
        Initializes the cleanup manager.

        Args:
            client: The SolanaClient wrapper instance.
            wallet: The Wallet instance containing the user's keypair.
            fee_manager: The PriorityFeeManager instance.
        """
        self.client = client
        self.wallet = wallet
        self.fee_manager = fee_manager
        # Use the imported constant correctly
        self.token_program_id = SolanaProgramAddresses.TOKEN_PROGRAM_ID
        logger.debug("AccountCleanupManager initialized.")

    async def cleanup_ata(
            self,
            mint: Pubkey,
            use_priority_fee: bool = False,
            force_close: bool = False,
            cu_limit: int = 50_000,
            cu_price: Optional[int] = None
    ):
        """
        Closes the user's ATA for the given mint if its balance is zero.

        Args:
            mint: The Pubkey of the token mint whose ATA should be cleaned up.
            use_priority_fee: Whether to add priority fee instructions to the cleanup transaction.
            force_close: If True, attempts to close even if balance isn't confirmed zero (likely to fail).
            cu_limit: Compute unit limit to set if using priority fees.
            cu_price: Specific compute unit price (microlamports) if using fixed priority fee.
        """
        owner_pubkey = self.wallet.pubkey
        ata_to_close = InstructionBuilder.get_associated_token_address(owner_pubkey, mint)
        logger.info(f"Cleanup: Checking ATA {ata_to_close} for mint {mint}...")

        try:
            # Check if ATA exists using Confirmed commitment for safety before closing
            ata_info = await self.client.get_account_info(ata_to_close, commitment=Confirmed)
            if ata_info is None or ata_info.value is None:
                logger.info(f"Cleanup: ATA {ata_to_close} does not exist. No action needed.")
                return

            # Check balance using Confirmed commitment
            balance_resp = await self.client.get_token_account_balance(ata_to_close, commitment=Confirmed)
            balance_lamports = int(balance_resp.value.amount) if balance_resp and balance_resp.value else -1

            if balance_lamports == 0:
                logger.info(f"Cleanup: ATA {ata_to_close} exists and balance is zero. Preparing close instruction.")

                instructions = []
                # Add priority fee if requested
                if use_priority_fee:
                    # Use a simplified account list for fee calculation for cleanup
                    fee_details = await self.fee_manager.get_priority_fee([ata_to_close, owner_pubkey])
                    price = cu_price if cu_price is not None else (fee_details.fee if fee_details else 1)
                    # Prepend fee instructions
                    if price: instructions.append(InstructionBuilder.set_compute_unit_price(price))
                    instructions.append(InstructionBuilder.set_compute_unit_limit(cu_limit))
                    logger.debug(f"Cleanup: Using priority fee: Limit={cu_limit}, Price={price}")

                # Add close instruction
                instructions.append(
                    InstructionBuilder.close_account_instruction(
                        account_to_close=ata_to_close,
                        destination_wallet=owner_pubkey,  # Send rent SOL back to owner
                        owner=owner_pubkey
                    )
                )

                # Send transaction
                cleanup_result: TransactionSendResult = await build_and_send_transaction(
                    client=self.client,
                    payer=self.wallet.keypair,
                    instructions=instructions,
                    signers=[self.wallet.keypair],  # Owner must sign to close
                    label=f"Cleanup_{str(mint)[:5]}",
                    confirm=True,  # Confirm cleanup transactions
                    confirm_commitment_str="confirmed"  # Use confirmed for cleanup
                )

                if cleanup_result.success:
                    logger.info(f"Cleanup: Successfully closed ATA {ata_to_close}. Tx: {cleanup_result.signature}")
                else:
                    logger.error(
                        f"Cleanup: Failed to close ATA {ata_to_close}: {cleanup_result.error_message} (Type: {cleanup_result.error_type})")

            elif balance_lamports > 0 and not force_close:
                logger.warning(
                    f"Cleanup: Cannot close ATA {ata_to_close}, balance is {balance_lamports}. Set force_close=True to attempt anyway (requires specific delegate/authority).")
            elif balance_lamports > 0 and force_close:
                logger.warning(
                    f"Cleanup: Force close requested for ATA {ata_to_close} with non-zero balance ({balance_lamports}). This standard close instruction will likely fail.")
                # Standard close instruction only works for zero balance unless owner==payer and it's native SOL account (not applicable here)
                # Force closing with balance usually requires specific program logic or close authority delegate.
            else:  # balance_lamports < 0 indicates an error fetching balance
                logger.warning(f"Cleanup: Could not verify balance for ATA {ata_to_close}. Skipping close.")

        except Exception as e:
            logger.error(f"Cleanup: Error during cleanup check/execution for ATA {ata_to_close}: {e}", exc_info=True)