# src/core/pubkeys.py
from solders.pubkey import Pubkey

# --- Define Pump.fun Program ID as a module-level constant ---
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

class SolanaProgramAddresses:
    """ Standard Solana Program IDs """
    SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
    TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    # --- FIX: Use the CORRECT Associated Token Program ID string ---
    ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLpFBQk")
    # --- End Fix ---
    RENT_SYSVAR_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
    # Add others like CLOCK_SYSVAR if needed later

class PumpAddresses:
    """ Addresses specific to the pump.fun program """
    PROGRAM_ID = PUMP_FUN_PROGRAM_ID
    # Double-check these addresses against a reliable source too if you encounter issues
    GLOBAL_ACCOUNT = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
    FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4f57oe")
    EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsUPsTYfcmtdctqbsgqETuaWNhGP4")


# --- Convenience re-exports ---
SYSTEM_PROGRAM_ID = SolanaProgramAddresses.SYSTEM_PROGRAM_ID
TOKEN_PROGRAM_ID = SolanaProgramAddresses.TOKEN_PROGRAM_ID
ASSOCIATED_TOKEN_PROGRAM_ID = SolanaProgramAddresses.ASSOCIATED_TOKEN_PROGRAM_ID
# --- End Re-exports ---