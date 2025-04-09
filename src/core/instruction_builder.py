# src/core/instruction_builder.py

import struct
from typing import List, Optional

from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey
# Unused import removed: from solders.keypair import Keypair

# --- Import Constants from pubkeys.py ---
# Remove the try/except fallback - If this fails, it's a fundamental project setup error.
from .pubkeys import PumpAddresses, SolanaProgramAddresses
# --- End Import ---

# Import standard SPL/System program IDs directly for clarity if preferred,
# or rely on the SolanaProgramAddresses class. Let's use the class for consistency.
# from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
# from solders.system_program import ID as SYSTEM_PROGRAM_ID
# from solders.sysvar import ID as RENT_SYSVAR_ID # Correct import for Rent Sysvar if needed

from ..utils.logger import get_logger

logger = get_logger(__name__)


class InstructionBuilder:
    """Builds various instructions needed for the bot."""

    @staticmethod
    def get_create_ata_instruction(
        payer_pubkey: Pubkey,
        owner: Pubkey,
        mint: Pubkey,
    ) -> List[Instruction]:
        """ Returns instruction(s) to create an Associated Token Account (ATA). """
        from spl.token.instructions import create_associated_token_account, get_associated_token_address # Keep local import

        ata_address = get_associated_token_address(owner, mint)
        logger.debug(f"Building create_associated_token_account ix for ATA: {ata_address}, Owner: {owner}, Mint: {mint}")

        instruction = create_associated_token_account(
            payer=payer_pubkey,
            owner=owner,
            mint=mint
        )
        return [instruction]

    @staticmethod
    def get_pump_buy_instruction(
        user_pubkey: Pubkey,
        mint_pubkey: Pubkey,
        bonding_curve_pubkey: Pubkey,
        associated_bonding_curve_pubkey: Pubkey, # Account #4 - SOL vault
        user_token_account: Pubkey,
        amount_lamports: int,
        min_token_output: int
    ) -> Optional[Instruction]:
        """ Creates the instruction to buy tokens from pump.fun (Verified). """
        logger.debug(f"Building pump.fun BUY: Mint={mint_pubkey}, Curve={bonding_curve_pubkey}, SOLIn={amount_lamports}, MinTokenOut={min_token_output}")

        try:
            # Use lowercase for local variable (PEP 8)
            buy_discriminator = bytes([0x62, 0x71, 0xe9, 0x60, 0x30, 0x55, 0x5a, 0x2f]) # Verified
            packed_args = struct.pack("<QQ", amount_lamports, min_token_output) # Verified
            instruction_data = buy_discriminator + packed_args

            # Verified Account List for BUY
            accounts = [
                AccountMeta(pubkey=PumpAddresses.GLOBAL_ACCOUNT, is_signer=False, is_writable=True),          # 0: global (w)
                AccountMeta(pubkey=PumpAddresses.FEE_RECIPIENT, is_signer=False, is_writable=True),          # 1: fee_recipient (w)
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),                         # 2: mint (r)
                AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True),                  # 3: bonding_curve state PDA (w)
                AccountMeta(pubkey=associated_bonding_curve_pubkey, is_signer=False, is_writable=True),      # 4: bonding_curve SOL vault (w)
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),                    # 5: user's token account (w)
                AccountMeta(pubkey=user_pubkey, is_signer=True, is_writable=True),                            # 6: user (signer, w)
                AccountMeta(pubkey=SolanaProgramAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False), # 7: system_program (r)
                AccountMeta(pubkey=SolanaProgramAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),    # 8: token_program (r)
                AccountMeta(pubkey=SolanaProgramAddresses.RENT_SYSVAR_ID, is_signer=False, is_writable=False),      # 9: rent sysvar (r)
                AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),       # 10: event_authority (r)
                AccountMeta(pubkey=PumpAddresses.PROGRAM, is_signer=False, is_writable=False)                   # 11: pump.fun program (r)
            ]

            instruction = Instruction(
                program_id=PumpAddresses.PROGRAM,
                data=instruction_data,
                accounts=accounts
            )
            return instruction

        # Use different exception variable name
        except Exception as build_exc:
            logger.error(f"Failed to build pump.fun BUY instruction: {build_exc}", exc_info=True)
            return None

    @staticmethod
    def get_pump_sell_instruction(
        user_pubkey: Pubkey,
        mint_pubkey: Pubkey,
        bonding_curve_pubkey: Pubkey,
        associated_bonding_curve_pubkey: Pubkey, # Account #4 - SOL vault
        user_token_account: Pubkey,
        token_amount: int,
        min_sol_output: int
    ) -> Optional[Instruction]:
        """ Creates the instruction to sell tokens on pump.fun (Verified). """
        logger.debug(f"Building pump.fun SELL: Mint={mint_pubkey}, Curve={bonding_curve_pubkey}, TokenIn={token_amount}, MinSOLOut={min_sol_output}")

        try:
            # Use lowercase for local variable (PEP 8)
            sell_discriminator = bytes([0x07, 0x75, 0x72, 0x26, 0x88, 0x73, 0x69, 0x5f]) # Verified
            packed_args = struct.pack("<QQ", token_amount, min_sol_output) # Verified
            instruction_data = sell_discriminator + packed_args

            # Verified Account List for SELL
            accounts = [
                AccountMeta(pubkey=PumpAddresses.GLOBAL_ACCOUNT, is_signer=False, is_writable=True),         # 0: global (w)
                AccountMeta(pubkey=PumpAddresses.FEE_RECIPIENT, is_signer=False, is_writable=True),         # 1: fee_recipient (w)
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=True),                          # 2: mint (w)
                AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True),                 # 3: bonding_curve state PDA (w)
                AccountMeta(pubkey=associated_bonding_curve_pubkey, is_signer=False, is_writable=True),     # 4: bonding_curve SOL vault (w)
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),                   # 5: user's token account (w)
                AccountMeta(pubkey=user_pubkey, is_signer=True, is_writable=True),                           # 6: user (signer, w)
                AccountMeta(pubkey=SolanaProgramAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),                  # 7: system_program (r)
                AccountMeta(pubkey=SolanaProgramAddresses.ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),        # 8: associated_token_program (r)
                AccountMeta(pubkey=SolanaProgramAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),                   # 9: token_program (r)
                AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),      # 10: event_authority (r)
                AccountMeta(pubkey=PumpAddresses.PROGRAM, is_signer=False, is_writable=False)                  # 11: pump.fun program (r)
            ]

            instruction = Instruction(
                program_id=PumpAddresses.PROGRAM,
                data=instruction_data,
                accounts=accounts
            )
            return instruction

        # Use different exception variable name
        except Exception as build_exc:
            logger.error(f"Failed to build pump.fun SELL instruction: {build_exc}", exc_info=True)
            return None