# src/core/raydium_amm_v4.py

from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from construct import Struct as BorshStruct, Bytes, Int64ul, Int64sl, Int8ul  # Use construct directly if needed
from borsh_construct import U64  # Use borsh_construct if preferred/consistent

from typing import NamedTuple  # For structuring accounts if needed

from src.core.pubkeys import SolanaProgramAddresses
from src.utils.logger import get_logger

logger = get_logger(__name__)

# --- Raydium Liquidity Pool V4 Program ID ---
PROGRAM_ID_V4 = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")

# --- Instruction Data Layout (Adapted from raydium-py) ---
# Raydium V4 swap instruction discriminator is \x09
# Followed by amount_in (u64) and min_amount_out (u64)
# Using borsh_construct for consistency if used elsewhere
SWAP_LAYOUT = BorshStruct(
    # "discriminator" / Bytes(1), # Discriminator is often hardcoded as \x09
    "amount_in" / U64,
    "min_amount_out" / U64
)


# --- Account Structures (Define locally for clarity) ---
# Mirroring the accounts needed by the make_swap_instruction logic
# Order is critical and based on analysis of Raydium program / reliable sources.
class SwapAccounts(NamedTuple):
    token_program: Pubkey
    amm: Pubkey
    amm_authority: Pubkey
    amm_open_orders: Pubkey
    amm_target_orders: Pubkey
    pool_coin_token_account: Pubkey  # Base vault
    pool_pc_token_account: Pubkey  # Quote vault
    serum_program: Pubkey
    serum_market: Pubkey
    serum_bids: Pubkey
    serum_asks: Pubkey
    serum_event_queue: Pubkey
    serum_coin_vault_account: Pubkey
    serum_pc_vault_account: Pubkey
    serum_vault_signer: Pubkey
    user_source_token_account: Pubkey
    user_destination_token_account: Pubkey
    user_source_owner: Pubkey  # Must be signer


# Define args structure locally
class SwapArgs(NamedTuple):
    amount_in: int
    min_amount_out: int


# --- Instruction Builder Function (Adapted from raydium-py logic) ---
def make_swap_instruction(
        accounts: SwapAccounts,
        args: SwapArgs,
        program_id: Pubkey = PROGRAM_ID_V4  # Default to V4 program ID
) -> Instruction:
    """
    Builds the Raydium V4 swap instruction.
    Account order and data layout are critical and based on Raydium V4 standard.
    """

    # 1. Serialize arguments using the defined layout
    try:
        data_payload = SWAP_LAYOUT.build({
            "amount_in": args.amount_in,
            "min_amount_out": args.min_amount_out
        })
        # Prepend the correct discriminator for Raydium V4 swap (\x09)
        instruction_data = b'\x09' + data_payload
    except Exception as e:
        logger.error(f"Failed to serialize swap instruction data: {e}", exc_info=True)
        raise ValueError("Failed to build swap instruction data") from e

    # 2. Construct AccountMeta list in the specific order required by Raydium V4
    account_metas = [
        AccountMeta(pubkey=accounts.token_program, is_signer=False, is_writable=False),  # 0
        AccountMeta(pubkey=accounts.amm, is_signer=False, is_writable=True),  # 1
        AccountMeta(pubkey=accounts.amm_authority, is_signer=False, is_writable=False),  # 2
        AccountMeta(pubkey=accounts.amm_open_orders, is_signer=False, is_writable=True),  # 3
        AccountMeta(pubkey=accounts.amm_target_orders, is_signer=False, is_writable=True),
        # 4 Use target_orders from pool info
        AccountMeta(pubkey=accounts.pool_coin_token_account, is_signer=False, is_writable=True),  # 5 Base vault
        AccountMeta(pubkey=accounts.pool_pc_token_account, is_signer=False, is_writable=True),  # 6 Quote vault
        AccountMeta(pubkey=accounts.serum_program, is_signer=False, is_writable=False),  # 7
        AccountMeta(pubkey=accounts.serum_market, is_signer=False, is_writable=True),  # 8
        AccountMeta(pubkey=accounts.serum_bids, is_signer=False, is_writable=True),  # 9
        AccountMeta(pubkey=accounts.serum_asks, is_signer=False, is_writable=True),  # 10
        AccountMeta(pubkey=accounts.serum_event_queue, is_signer=False, is_writable=True),  # 11
        AccountMeta(pubkey=accounts.serum_coin_vault_account, is_signer=False, is_writable=True),  # 12
        AccountMeta(pubkey=accounts.serum_pc_vault_account, is_signer=False, is_writable=True),  # 13
        AccountMeta(pubkey=accounts.serum_vault_signer, is_signer=False, is_writable=False),  # 14
        AccountMeta(pubkey=accounts.user_source_token_account, is_signer=False, is_writable=True),  # 15
        AccountMeta(pubkey=accounts.user_destination_token_account, is_signer=False, is_writable=True),  # 16
        AccountMeta(pubkey=accounts.user_source_owner, is_signer=True, is_writable=False),  # 17 User must sign
    ]

    # 3. Create the Instruction object
    instruction = Instruction(
        program_id=program_id,  # Use the passed or default Raydium V4 program ID
        accounts=account_metas,
        data=instruction_data
    )
    logger.debug("Successfully built Raydium V4 swap instruction.")
    return instruction