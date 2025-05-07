# src/core/pubkeys.py
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID_SOLDERS # Renamed to avoid conflict
from spl.token.constants import TOKEN_PROGRAM_ID as TOKEN_PROGRAM_ID_SPL # Renamed to avoid conflict

class PumpAddresses:
    # (1) The on-chain Pump.fun program ID
    PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

    # (2) Global state PDA (seed = b"global")
    # This needs to be calculated at runtime or defined if the bump is known and static.
    # For simplicity in definition here, we calculate it.
    # If this is computationally intensive to do on every import, consider defining it once.
    _global_state_pda, _global_state_bump = Pubkey.find_program_address(
        [b"global"],
        PROGRAM_ID
    )
    GLOBAL_STATE: Pubkey = _global_state_pda

    # (3) Fee recipient: the on-chain treasury account that collects fees
    FEE_RECIPIENT: Pubkey = Pubkey.from_string("DYw8jG1p4XyGZc2Qj3R4J6YwL8N1QZz3QkX7rE9SgP5")

    # (4) Event authority PDA (seed = b"eventAuthority")
    _event_authority_pda, _event_authority_bump = Pubkey.find_program_address(
        [b"eventAuthority"],
        PROGRAM_ID
    )
    EVENT_AUTHORITY: Pubkey = _event_authority_pda

class SolanaProgramAddresses:
    SYSTEM_PROGRAM_ID: Pubkey = SYSTEM_PROGRAM_ID_SOLDERS # Using the one from solders.system_program
    TOKEN_PROGRAM_ID: Pubkey = TOKEN_PROGRAM_ID_SPL      # Using the one from spl.token.constants
    ASSOCIATED_TOKEN_ACCOUNT_PROGRAM_ID: Pubkey = Pubkey.from_string(
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
    )
    RENT_SYSVAR_PUBKEY: Pubkey = Pubkey.from_string(
        "SysvarRent111111111111111111111111111111111"
    )
    COMPUTE_BUDGET_PROGRAM_ID: Pubkey = Pubkey.from_string( # Standard Compute Budget Program ID
        "ComputeBudget111111111111111111111111111111"
    )

# Expose bump seeds if needed elsewhere, though typically not for instruction building directly
# PUMP_GLOBAL_STATE_BUMP = _global_state_bump
# PUMP_EVENT_AUTHORITY_BUMP = _event_authority_bump