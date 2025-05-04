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
    # Ensure decimals is present if used elsewhere
    decimals: Optional[int] = None


@dataclass
class TradeResult:
    """Represents the outcome of a buy or sell operation."""
    success: bool
    tx_signature: Optional[str] = None
    error_message: Optional[str] = None
    price: Optional[float] = None   # Price in SOL per token
    amount: Optional[float] = None  # UI amount bought/sold (tokens for buy, SOL for sell)
    initial_sol_liquidity: Optional[int] = None # Lamports in curve at time of buy prefetch
    # --- FIX: Added error_type field ---
    error_type: Optional[str] = None # e.g., BuildError, SendError, TxError, BalanceError, PrefetchError, MaxRetriesReached
    # --- END FIX ---