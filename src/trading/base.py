# src/trading/base.py
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from solders.pubkey import Pubkey
from enum import Enum  # Import Enum
from datetime import datetime, timezone  # Keep datetime imports


# --- NEW TokenState Enum ---
class TokenState(Enum):
    NEW = "NEW"  # Discovered, not yet processed
    PRE_BUY_CHECK = "PRE_BUY_CHECK"  # Undergoing pre-buy checks (age, creator holding)
    BUYING = "BUYING"  # Buy transaction initiated
    ON_BONDING_CURVE = "ON_BONDING_CURVE"  # Buy successful, monitoring on curve
    SELLING_ON_CURVE = "SELLING_ON_CURVE"  # Sell transaction initiated on curve
    SOLD_ON_CURVE = "SOLD_ON_CURVE"  # Sell successful on curve
    RUGGED = "RUGGED"  # Failed a rug check (creator holding, price/liq drop)
    BUY_FAILED = "BUY_FAILED"  # Buy transaction failed
    SELL_FAILED_ON_CURVE = "SELL_FAILED_ON_CURVE"  # Sell transaction failed on curve
    MONITORING_ERROR = "MONITORING_ERROR"  # Unexpected error during handling/monitoring

    # Add v3 states later:
    # TRANSITIONING_TO_RAYDIUM = "TRANSITIONING_TO_RAYDIUM"
    # ON_RAYDIUM = "ON_RAYDIUM"
    # SELLING_ON_RAYDIUM = "SELLING_ON_RAYDIUM"
    # SOLD_ON_RAYDIUM = "SOLD_ON_RAYDIUM"
    # SELL_FAILED_ON_RAYDIUM = "SELL_FAILED_ON_RAYDIUM"


# --- END NEW TokenState Enum ---


@dataclass
class TokenInfo:
    # --- Using the structure from logs_event_processor update ---
    mint: Pubkey
    name: str
    symbol: str
    uri: str
    creator: Pubkey
    bonding_curve_address: Pubkey
    associated_bonding_curve_address: Pubkey
    decimals: int
    token_program_id: Pubkey = field(
        default_factory=lambda: Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))
    creation_timestamp: Optional[int] = None
    extra_data: Dict[str, Any] = field(default_factory=dict)

    # Add state as an optional attribute if managed directly on the object
    # current_state: Optional[TokenState] = None

    @property
    def mint_str(self) -> str:
        return str(self.mint)

    # ... (hash, eq methods remain the same) ...
    def __hash__(self): return hash(self.mint_str)

    def __eq__(self, other):
        if not isinstance(other, TokenInfo): return NotImplemented
        return self.mint == other.mint


@dataclass
class TradeResult:
    # --- Using the structure from buyer/seller updates ---
    token_info: Optional[TokenInfo] = None
    signature: Optional[str] = None
    success: bool = False
    error: Optional[str] = None
    error_type: Optional[str] = None  # For build/send/confirm/tx errors
    raw_error: Optional[Any] = None  # Store raw exception if needed

    # Fields relevant to buy
    initial_sol_liquidity: Optional[int] = None
    spent_lamports: Optional[int] = None
    acquired_tokens: Optional[int] = None

    # Fields relevant to sell
    acquired_sol: Optional[int] = None
    sold_tokens: Optional[int] = None

    timestamp: int = field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))
    current_price_usd: Optional[float] = None
    current_market_cap_usd: Optional[float] = None 