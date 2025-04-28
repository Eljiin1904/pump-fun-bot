# src/core/instruction_builder.py (Corrected Imports and Usage)

# --- FIX: Corrected Imports ---
import logging # Added for logger setup if not present
from typing import List, Optional
from solders.pubkey import Pubkey
from solders.instruction import AccountMeta, Instruction

# Import the class containing the constants
from .pubkeys import PumpAddresses
# REMOVED: from .pubkeys import SolanaProgramAddresses (Doesn't exist)

from ..utils.logger import get_logger # Adjusted relative import if needed

# --- END FIX ---

# Use the project's logger setup
# logger = get_logger(__name__) # Or use: logging.getLogger(__name__)
logger = logging.getLogger(__name__) # Assuming logger setup is handled elsewhere


class InstructionBuilder:
    """ Helper class to build specific instructions for pump.fun or related programs. """

    @staticmethod
    def build_buy_instruction(
        user: Pubkey,
        mint_pubkey: Pubkey,
        bonding_curve_pubkey: Pubkey,
        associated_bonding_curve_pubkey: Pubkey,
        token_amount_lamports: int, # Amount of pump tokens to receive
        max_sol_cost_lamports: int, # Max SOL user is willing to pay
        discriminator: bytes = bytes.fromhex("f5231e3e54b4cec7") # Buy instruction discriminator
    ) -> Instruction:
        """ Builds the instruction for buying tokens from the pump.fun bonding curve. """
        logger.debug(f"Building buy instruction: User={user}, Mint={mint_pubkey}, Amount={token_amount_lamports}, MaxCost={max_sol_cost_lamports}")

        # --- FIX: Access constants via PumpAddresses class ---
        accounts = [
            AccountMeta(pubkey=PumpAddresses.GLOBAL_STATE, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.FEE_RECIPIENT, is_signer=False, is_writable=True), # Fee recipient is writable
            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True), # Curve state is modified
            AccountMeta(pubkey=associated_bonding_curve_pubkey, is_signer=False, is_writable=True), # Curve's token account changes
            AccountMeta(pubkey=Wallet.get_associated_token_address(user, mint_pubkey), is_signer=False, is_writable=True), # User's token account changes
            AccountMeta(pubkey=user, is_signer=True, is_writable=True), # User pays SOL, is signer
            AccountMeta(pubkey=PumpAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.RENT_SYSVAR, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False), # Needed if ATA is created
             # Add EVENT_AUTHORITY if required by specific instruction variant
             # AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),
        ]
        # --- END FIX ---

        # Pack the arguments (discriminator + token_amount + max_sol_cost)
        # Ensure correct packing format (e.g., little-endian for u64)
        instruction_data = discriminator + token_amount_lamports.to_bytes(8, 'little') + max_sol_cost_lamports.to_bytes(8, 'little')

        # --- FIX: Access program ID via PumpAddresses class ---
        return Instruction(
            program_id=PumpAddresses.PROGRAM, # Use the correct program ID
            accounts=accounts,
            data=instruction_data
        )
        # --- END FIX ---

    @staticmethod
    def build_sell_instruction(
        user: Pubkey,
        mint_pubkey: Pubkey,
        bonding_curve_pubkey: Pubkey,
        associated_bonding_curve_pubkey: Pubkey,
        token_amount_lamports: int, # Amount of pump tokens user is selling
        min_sol_output_lamports: int, # Min SOL user expects back
        discriminator: bytes = bytes.fromhex("346c2384a760e448") # Sell instruction discriminator
    ) -> Instruction:
        """ Builds the instruction for selling tokens back to the pump.fun bonding curve. """
        logger.debug(f"Building sell instruction: User={user}, Mint={mint_pubkey}, Amount={token_amount_lamports}, MinOutput={min_sol_output_lamports}")

        # --- FIX: Access constants via PumpAddresses class ---
        accounts = [
             AccountMeta(pubkey=PumpAddresses.GLOBAL_STATE, is_signer=False, is_writable=False),
             AccountMeta(pubkey=PumpAddresses.FEE_RECIPIENT, is_signer=False, is_writable=True), # Fee recipient is writable
             AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False), # Should not be writable for sell
             AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True), # Curve state is modified
             AccountMeta(pubkey=associated_bonding_curve_pubkey, is_signer=False, is_writable=True), # Curve's token account changes
             AccountMeta(pubkey=Wallet.get_associated_token_address(user, mint_pubkey), is_signer=False, is_writable=True), # User's token account changes
             AccountMeta(pubkey=user, is_signer=True, is_writable=True), # User receives SOL, is signer
             AccountMeta(pubkey=PumpAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
             AccountMeta(pubkey=PumpAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
             # Add EVENT_AUTHORITY if required by specific instruction variant
             # AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),
        ]
        # --- END FIX ---

        # Pack the arguments (discriminator + token_amount + min_sol_output)
        instruction_data = discriminator + token_amount_lamports.to_bytes(8, 'little') + min_sol_output_lamports.to_bytes(8, 'little')

        # --- FIX: Access program ID via PumpAddresses class ---
        return Instruction(
            program_id=PumpAddresses.PROGRAM, # Use the correct program ID
            accounts=accounts,
            data=instruction_data
        )
        # --- END FIX ---

    # Add other instruction builders as needed (e.g., create ATA, close account)

# Need Wallet class imported if get_associated_token_address is used as static method
try:
    from .wallet import Wallet
except ImportError:
    # Define a dummy if Wallet isn't available here, but real import is needed
    logger.error("Wallet class import needed for InstructionBuilder.")
    class Wallet: # Dummy
        @staticmethod
        def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
            # This won't work without the real method, needs proper import or refactor
            logger.warning("Using dummy Wallet.get_associated_token_address")
            pda, _ = Pubkey.find_program_address(
                 [bytes(owner), bytes(PumpAddresses.TOKEN_PROGRAM_ID), bytes(mint)],
                 PumpAddresses.ASSOCIATED_TOKEN_PROGRAM_ID
             )
            return pda