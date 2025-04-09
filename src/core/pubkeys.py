# src/core/pubkeys.py

from solders.pubkey import Pubkey

class SolanaProgramAddresses:
    """ Standard Solana Program IDs """
    SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
    TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
    RENT_SYSVAR_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
    # Add others like CLOCK_SYSVAR_ID if needed later

class PumpAddresses:
    """ Addresses specific to the pump.fun program """
    # --- VERIFIED Addresses (from Solscan analysis) ---
    PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
    GLOBAL_ACCOUNT = Pubkey.from_string("58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2")
    FEE_RECIPIENT = Pubkey.from_string("32D4zPeaRRysFK1NKSXCcCVQmSBiQWfC61pF4DCgPW1Q") # Often called feeVault
    EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasMosup7FXqsN")
    # --- Add other pump.fun related constants if discovered ---
    # e.g., if there's a specific MINT_AUTHORITY PDA, etc.

# Convenience re-exports if desired, or import directly via classes
SYSTEM_PROGRAM_ID = SolanaProgramAddresses.SYSTEM_PROGRAM_ID
TOKEN_PROGRAM_ID = SolanaProgramAddresses.TOKEN_PROGRAM_ID
ASSOCIATED_TOKEN_PROGRAM_ID = SolanaProgramAddresses.ASSOCIATED_TOKEN_PROGRAM_ID
RENT_SYSVAR_ID = SolanaProgramAddresses.RENT_SYSVAR_ID
PUMP_FUN_PROGRAM_ID = PumpAddresses.PROGRAM