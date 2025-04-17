# src/core/instruction_builder.py

import struct
import logging # Added for the new method's logger
from typing import List, Optional

from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey
# Import necessary client and SPL token functions for the new method
from solana.rpc.async_api import AsyncClient
from spl.token.instructions import create_associated_token_account, get_associated_token_address


# Import constants using the defined classes/variables
from .pubkeys import PumpAddresses, SolanaProgramAddresses

from ..utils.logger import get_logger

# Use the project's logger setup
logger = get_logger(__name__) # Or use: logging.getLogger(__name__)


class InstructionBuilder:
    """Builds various instructions needed for the bot."""

    @staticmethod
    def get_create_ata_instruction(
        payer_pubkey: Pubkey,
        owner: Pubkey,
        mint: Pubkey,
    ) -> List[Instruction]:
        """
        DEPRECATED (potentially): Returns instruction(s) to create an Associated Token Account (ATA)
        WITHOUT checking if it exists first. Use get_create_ata_instruction_if_needed instead.
        """
        # from spl.token.instructions import create_associated_token_account, get_associated_token_address # Already imported above

        ata_address = get_associated_token_address(owner, mint)
        logger.warning(f"Building create_associated_token_account ix for ATA: {ata_address} WITHOUT existence check. Consider using get_create_ata_instruction_if_needed.")

        payer = payer_pubkey
        instruction = create_associated_token_account(
            payer=payer,
            owner=owner,
            mint=mint
        )
        return [instruction]

    # <<< METHOD ADDED HERE >>>
    @staticmethod
    async def get_create_ata_instruction_if_needed(
        payer_pubkey: Pubkey,
        owner: Pubkey,
        mint: Pubkey,
        client: AsyncClient # Pass the raw AsyncClient here
    ) -> List[Instruction]:
        """
        Checks if an Associated Token Account (ATA) exists and returns the
        creation instruction only if it doesn't.

        Args:
            payer_pubkey: The account that will pay for the ATA creation.
            owner: The owner of the ATA.
            mint: The token mint the ATA is for.
            client: An initialized Solana AsyncClient instance for checking existence.

        Returns:
            A list containing the create_associated_token_account instruction
            if the ATA doesn't exist, otherwise an empty list.
        """
        try:
            # Calculate the expected ATA address
            ata_pubkey = get_associated_token_address(owner, mint)
            logger.debug(f"Checking existence of ATA: {ata_pubkey} for owner {owner} and mint {mint}")

            # Use the passed client to check if the account has data
            # Use confirmed commitment for better reliability on existence checks
            acc_info_resp = await client.get_account_info(ata_pubkey, commitment="confirmed")

            # Check if the account exists (value is not None)
            if acc_info_resp.value is None:
                # Account does not exist, create instruction needed
                logger.info(f"ATA {ata_pubkey} does not exist. Generating create instruction.")
                instruction = create_associated_token_account(
                    payer=payer_pubkey,
                    owner=owner,
                    mint=mint
                )
                return [instruction]
            else:
                # Account exists, no instruction needed
                logger.debug(f"ATA {ata_pubkey} already exists.")
                return []
        except Exception as e:
            # Log error but fallback to creating - might waste gas if it exists but check failed
            logger.error(f"Error checking ATA existence for {ata_pubkey}: {e}. Proceeding with creation instruction as fallback.", exc_info=True)
            instruction = create_associated_token_account(
                payer=payer_pubkey,
                owner=owner,
                mint=mint
            )
            return [instruction]
    # <<< END OF ADDED METHOD >>>


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
            buy_discriminator = bytes.fromhex("6271e96030555a2f")
            packed_args = struct.pack("<QQ", amount_lamports, min_token_output)
            instruction_data = buy_discriminator + packed_args
            accounts = [
                AccountMeta(pubkey=PumpAddresses.GLOBAL_ACCOUNT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=PumpAddresses.FEE_RECIPIENT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True),
                AccountMeta(pubkey=associated_bonding_curve_pubkey, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=SolanaProgramAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SolanaProgramAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SolanaProgramAddresses.RENT_SYSVAR_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PumpAddresses.PROGRAM_ID, is_signer=False, is_writable=False) # Use PROGRAM_ID
            ]
            instruction = Instruction(
                program_id=PumpAddresses.PROGRAM_ID, # Use PROGRAM_ID
                data=instruction_data,
                accounts=accounts
            )
            return instruction
        except Exception as build_exc: logger.error(f"Failed to build pump.fun BUY instruction: {build_exc}", exc_info=True); return None

    @staticmethod
    def get_pump_sell_instruction(
        user_pubkey: Pubkey,
        mint_pubkey: Pubkey,
        bonding_curve_pubkey: Pubkey,
        associated_bonding_curve_pubkey: Pubkey,
        user_token_account: Pubkey,
        token_amount: int,
        min_sol_output: int
    ) -> Optional[Instruction]:
        """ Creates the instruction to sell tokens on pump.fun (Verified). """
        logger.debug(f"Building pump.fun SELL: Mint={mint_pubkey}, Curve={bonding_curve_pubkey}, TokenIn={token_amount}, MinSOLOut={min_sol_output}")
        try:
            sell_discriminator = bytes.fromhex("077572268873695f")
            packed_args = struct.pack("<QQ", token_amount, min_sol_output)
            instruction_data = sell_discriminator + packed_args
            accounts = [
                AccountMeta(pubkey=PumpAddresses.GLOBAL_ACCOUNT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=PumpAddresses.FEE_RECIPIENT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=True),
                AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True),
                AccountMeta(pubkey=associated_bonding_curve_pubkey, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=SolanaProgramAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SolanaProgramAddresses.ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SolanaProgramAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PumpAddresses.PROGRAM_ID, is_signer=False, is_writable=False) # Use PROGRAM_ID
            ]
            instruction = Instruction(
                program_id=PumpAddresses.PROGRAM_ID, # Use PROGRAM_ID
                data=instruction_data,
                accounts=accounts
            )
            return instruction
        except Exception as build_exc: logger.error(f"Failed to build pump.fun SELL instruction: {build_exc}", exc_info=True); return None