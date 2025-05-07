# src/core/instruction_builder.py
from typing import List, Optional
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from src.core.pubkeys import PumpAddresses, SolanaProgramAddresses  # Use our finalized pubkeys

# --- Instruction Discriminators (from provided IDL info) ---
BUY_DISCRIMINATOR = bytes.fromhex("66063d1201daebea")
SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")


class InstructionBuilder:
    @staticmethod
    def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
        """Calculates the Associated Token Account address for a given owner and mint."""
        pda, _bump_seed = Pubkey.find_program_address(
            [bytes(owner), bytes(SolanaProgramAddresses.TOKEN_PROGRAM_ID), bytes(mint)],
            SolanaProgramAddresses.ASSOCIATED_TOKEN_ACCOUNT_PROGRAM_ID
        )
        return pda

    @staticmethod
    def get_create_ata_instruction(
            payer: Pubkey,
            owner: Pubkey,
            mint: Pubkey,
            ata_pubkey: Optional[Pubkey] = None
    ) -> Instruction:
        """
        Generates the instruction to create an Associated Token Account.
        The caller (Buyer/Seller) is responsible for checking if the ATA already exists.
        """
        associated_token_address = ata_pubkey or InstructionBuilder.get_associated_token_address(owner, mint)

        return Instruction(
            program_id=SolanaProgramAddresses.ASSOCIATED_TOKEN_ACCOUNT_PROGRAM_ID,
            accounts=[
                AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
                AccountMeta(pubkey=associated_token_address, is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SolanaProgramAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SolanaProgramAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SolanaProgramAddresses.RENT_SYSVAR_PUBKEY, is_signer=False, is_writable=False),
            ],
            data=b''
        )

    @staticmethod
    def set_compute_unit_limit(units: int) -> Instruction:
        """Creates an instruction to set the compute unit limit for the transaction."""
        # Instruction data: 8-bit instruction discriminator (2 for set_compute_unit_limit), 32-bit units
        data = b'\x02' + units.to_bytes(4, 'little')
        return Instruction(
            program_id=SolanaProgramAddresses.COMPUTE_BUDGET_PROGRAM_ID,
            accounts=[],  # No accounts needed for this instruction
            data=data
        )

    @staticmethod
    def set_compute_unit_price(micro_lamports: int) -> Instruction:
        """Creates an instruction to set the compute unit price (priority fee) for the transaction."""
        # Instruction data: 8-bit instruction discriminator (3 for set_compute_unit_price), 64-bit micro_lamports
        data = b'\x03' + micro_lamports.to_bytes(8, 'little')
        return Instruction(
            program_id=SolanaProgramAddresses.COMPUTE_BUDGET_PROGRAM_ID,
            accounts=[],  # No accounts needed for this instruction
            data=data
        )

    @staticmethod
    def build_pump_fun_buy_instruction(
            user_wallet_pubkey: Pubkey,
            mint_pubkey: Pubkey,
            bonding_curve_pubkey: Pubkey,
            assoc_bonding_curve_token_account_pubkey: Pubkey,
            sol_amount_in_lamports: int,
            min_token_output_lamports: int  # Semantically 'min_acceptable_tokens_out'
    ) -> Instruction:
        """Builds the pump.fun 'buy' instruction using IDL-verified layout."""
        user_ata_pubkey = InstructionBuilder.get_associated_token_address(user_wallet_pubkey, mint_pubkey)

        instruction_data = (
                BUY_DISCRIMINATOR +
                sol_amount_in_lamports.to_bytes(8, 'little') +
                min_token_output_lamports.to_bytes(8, 'little')
        )

        accounts = [
            AccountMeta(pubkey=PumpAddresses.GLOBAL_STATE, is_signer=False, is_writable=False),  # 0. global
            AccountMeta(pubkey=PumpAddresses.FEE_RECIPIENT, is_signer=False, is_writable=True),  # 1. feeRecipient
            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),  # 2. mint
            AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True),  # 3. bondingCurve
            AccountMeta(pubkey=assoc_bonding_curve_token_account_pubkey, is_signer=False, is_writable=True),
            # 4. associatedBondingCurve
            AccountMeta(pubkey=user_ata_pubkey, is_signer=False, is_writable=True),  # 5. associatedUser
            AccountMeta(pubkey=user_wallet_pubkey, is_signer=True, is_writable=True),  # 6. user
            AccountMeta(pubkey=SolanaProgramAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            # 7. systemProgram
            AccountMeta(pubkey=SolanaProgramAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            # 8. tokenProgram
            AccountMeta(pubkey=SolanaProgramAddresses.RENT_SYSVAR_PUBKEY, is_signer=False, is_writable=False),
            # 9. rent
            AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),  # 10. eventAuthority
            AccountMeta(pubkey=PumpAddresses.PROGRAM_ID, is_signer=False, is_writable=False),
            # 11. program (pump.fun program itself)
        ]

        return Instruction(
            program_id=PumpAddresses.PROGRAM_ID,
            accounts=accounts,
            data=instruction_data
        )

    @staticmethod
    def build_pump_fun_sell_instruction(
            user_wallet_pubkey: Pubkey,
            mint_pubkey: Pubkey,
            bonding_curve_pubkey: Pubkey,
            assoc_bonding_curve_token_account_pubkey: Pubkey,
            token_amount_in_lamports: int,
            min_sol_output_lamports: int
    ) -> Instruction:
        """Builds the pump.fun 'sell' instruction using IDL-verified layout."""
        user_ata_pubkey = InstructionBuilder.get_associated_token_address(user_wallet_pubkey, mint_pubkey)

        instruction_data = (
                SELL_DISCRIMINATOR +
                token_amount_in_lamports.to_bytes(8, 'little') +
                min_sol_output_lamports.to_bytes(8, 'little')
        )

        accounts = [
            AccountMeta(pubkey=PumpAddresses.GLOBAL_STATE, is_signer=False, is_writable=False),  # 0. global
            AccountMeta(pubkey=PumpAddresses.FEE_RECIPIENT, is_signer=False, is_writable=True),  # 1. feeRecipient
            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),  # 2. mint
            AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True),  # 3. bondingCurve
            AccountMeta(pubkey=assoc_bonding_curve_token_account_pubkey, is_signer=False, is_writable=True),
            # 4. associatedBondingCurve
            AccountMeta(pubkey=user_ata_pubkey, is_signer=False, is_writable=True),  # 5. associatedUser
            AccountMeta(pubkey=user_wallet_pubkey, is_signer=True, is_writable=True),  # 6. user
            AccountMeta(pubkey=SolanaProgramAddresses.SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            # 7. systemProgram
            AccountMeta(pubkey=SolanaProgramAddresses.ASSOCIATED_TOKEN_ACCOUNT_PROGRAM_ID, is_signer=False,
                        is_writable=False),  # 8. associatedTokenProgram
            AccountMeta(pubkey=SolanaProgramAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            # 9. tokenProgram
            AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),  # 10. eventAuthority
            AccountMeta(pubkey=PumpAddresses.PROGRAM_ID, is_signer=False, is_writable=False),
            # 11. program (pump.fun program itself)
        ]

        return Instruction(
            program_id=PumpAddresses.PROGRAM_ID,
            accounts=accounts,
            data=instruction_data
        )

    @staticmethod
    def close_account_instruction(
            account_to_close: Pubkey,
            destination_wallet: Pubkey,
            owner: Pubkey
    ) -> Instruction:
        """Creates an instruction to close a token account and send its SOL to the destination."""
        return Instruction(
            program_id=SolanaProgramAddresses.TOKEN_PROGRAM_ID,
            accounts=[
                AccountMeta(pubkey=account_to_close, is_signer=False, is_writable=True),
                AccountMeta(pubkey=destination_wallet, is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
            ],
            data=b'\x09'  # spl-token CloseAccount instruction discriminator
        )