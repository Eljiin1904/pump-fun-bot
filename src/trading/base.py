# src/trading/base.py

from dataclasses import dataclass, field
from typing import Optional
from solders.pubkey import Pubkey

@dataclass
class TokenInfo:
    """Represents information about a token."""
    name: str
    symbol: str
    uri: str
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    user: Pubkey  # Creator wallet
    created_timestamp: float  # Approximate time detected/created
    website: Optional[str] = None
    twitter: Optional[str] = None
    telegram: Optional[str] = None
    description: Optional[str] = None

@dataclass
class TradeResult:
    """Represents the outcome of a buy or sell operation."""
    success: bool
    tx_signature: Optional[str] = None
    error_message: Optional[str] = None
    price: Optional[float] = None   # Price in SOL per token
    amount: Optional[float] = None  # UI amount bought/sold
    initial_sol_liquidity: Optional[int] = None
