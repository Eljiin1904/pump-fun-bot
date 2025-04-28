# src/core/pubkeys.py (Consolidated and Corrected)

from solders.pubkey import Pubkey

class PumpAddresses:
    # Bonding Curve Program ID (pump.fun)
    PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

    # Global State Address (derived from "global")
    GLOBAL_STATE = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQgFhewuKV2ecMTjgW5")

    # Fee Recipient Address
    FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1S77jyRhGiKu7nJhpb")

    # SPL Associated Token Account Program ID
    ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

    # SPL Token Program ID
    TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

    # System Program ID
    SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

    # Rent Sysvar ID
    RENT_SYSVAR = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

    # Event Authority Address (derived from "event_authority")
    EVENT_AUTHORITY = Pubkey.from_string("Ce6cVmMZmRBgvFjP9fBJgoSPHYVHCbT1pMhp2ng5Rb1y")

    # Raydium Liquidity Pool V4 Program ID
    RAYDIUM_LIQUIDITY_POOL_V4 = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")

    # Serum OpenBook Dex Program ID (often needed by Raydium)
    OPENBOOK_DEX_PROGRAM_ID = Pubkey.from_string("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")

    # Wrapped SOL Mint Address
    SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

    # USDC Mint Address (example)
    USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

# --- END OF FILE ---