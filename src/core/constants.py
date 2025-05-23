from solders.pubkey import Pubkey

# Pump.fun specific program ID
#PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8hdqvucDLmohtGcPxbyjNzervCP")

# Default discriminator for the "Create" event log on pump.fun if not in config.
DEFAULT_CREATE_EVENT_DISCRIMINATOR = "1b72a94ddeeb6376" # From your trader.py config example

# Solana-wide constants
SOL_MINT_ADDRESS = Pubkey.from_string("So11111111111111111111111111111111111111112")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")