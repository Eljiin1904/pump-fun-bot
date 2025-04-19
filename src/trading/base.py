# src/trading/base.py
from dataclasses import dataclass
from typing import Optional
from solders.pubkey import Pubkey

@dataclass
class TokenInfo:
    """Represents information about a token detected for trading."""
    mint: Pubkey
    symbol: str
    name: Optional[str] = None
    creator: Optional[Pubkey] = None        # Token creator (for rug checks)
    bonding_curve: Optional[Pubkey] = None  # Bonding curve state account (if applicable)
    initial_sol_liquidity: Optional[int] = None  # SOL (lamports) in curve at buy time

@dataclass
class TradeResult:
    """Result of a token buy or sell operation."""
    success: bool
    tx_signature: Optional[str] = None
    price: Optional[float] = None         # Price (in SOL per token) for this trade
    amount: Optional[float] = None        # Amount of tokens bought/sold (in token units)
    initial_sol_liquidity: Optional[int] = None  # Initial SOL liquidity (for buy operations)
    error_message: Optional[str] = None

class TokenState:
    """Enumeration of token states during the trading lifecycle."""
    DETECTED = "DETECTED"
    BUYING = "BUYING"
    BUY_FAILED = "BUY_FAILED"
    ON_BONDING_CURVE = "ON_BONDING_CURVE"
    SELLING_CURVE = "SELLING_CURVE"
    ON_RAYDIUM = "ON_RAYDIUM"
    SELLING_RAYDIUM = "SELLING_RAYDIUM"
    SOLD_CURVE = "SOLD_CURVE"
    SOLD_RAYDIUM = "SOLD_RAYDIUM"
    RUGGED = "RUGGED"
