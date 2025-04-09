# src/trading/base.py
from dataclasses import dataclass, field
from typing import Optional
from solders.pubkey import Pubkey
# Add any other necessary imports

@dataclass
class TokenInfo:
    """Represents information about a token."""
    name: str
    symbol: str
    uri: str
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    user: Pubkey # Creator wallet
    created_timestamp: float # Time detected/created approx

    # Optional metadata fields
    website: Optional[str] = None
    twitter: Optional[str] = None
    telegram: Optional[str] = None
    description: Optional[str] = None
    # Add other fields fetched from URI if needed


@dataclass
class TradeResult:
    """Represents the outcome of a buy or sell operation."""
    success: bool
    tx_signature: Optional[str] = None
    error_message: Optional[str] = None
    price: Optional[float] = None  # Price in SOL per token (approximate if slippage occurs)
    amount: Optional[float] = None # Amount of tokens bought/sold (UI amount)

    # --- ADDED Field ---
    # Bonding curve SOL reserves (lamports) AT TIME OF BUY attempt
    # Useful for post-buy liquidity drop checks
    initial_sol_liquidity: Optional[int] = None
    # --- END ADDED ---

    # Add other relevant fields if needed, e.g., actual lamports used/received