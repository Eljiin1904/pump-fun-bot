# src/trading/base.py
"""
Base interfaces for trading operations.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass  # Ensure field is imported if needed later
from typing import Optional  # Ensure Optional is imported

from solders.pubkey import Pubkey


# Assuming PumpAddresses might be needed here or defined elsewhere
# from ..core.pubkeys import PumpAddresses
# If not directly needed, remove the import

@dataclass
class TokenInfo:
    """Token information."""
    # Existing fields...
    name: str
    symbol: str
    uri: str # Keep if used, often derived or less critical for trading
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    user: Pubkey # Represents the creator address

    # --- Add timestamp if available from listener ---
    created_timestamp: Optional[float] = None # UNIX timestamp

    # --- Add optional fields if fetched ---
    website: Optional[str] = None
    twitter: Optional[str] = None
    telegram: Optional[str] = None
    description: Optional[str] = None

    # --- Remove Class methods if creation happens elsewhere ---
    # These might belong in the listener/parser that creates TokenInfo
    # @classmethod
    # def from_dict(cls, data: dict[str, Any]) -> "TokenInfo": ...
    # def to_dict(self) -> dict[str, str]: ...

@dataclass
class TradeResult:
    """Result of a trading operation."""
    success: bool
    tx_signature: Optional[str] = None
    error_message: Optional[str] = None
    amount: Optional[float] = None  # Token amount bought/sold (in token units)
    price: Optional[float] = None  # Price per token in SOL

    # --- Add field for debugging balance check ---
    final_token_balance_raw: Optional[int] = None

    # --- Add field for initial liquidity check ---
    initial_sol_liquidity: Optional[int] = None # SOL lamports in curve before buy


class Trader(ABC):
    """Base interface for trading operations."""

    @abstractmethod
    async def execute(self, *args, **kwargs) -> TradeResult:
        """Execute trading operation.

        Returns:
            TradeResult with operation outcome
        """
        pass

    # --- Remove _get_relevant_accounts if not used ---
    # If priority fee calculation is done elsewhere based on actual instruction accounts
    # def _get_relevant_accounts(self, token_info: TokenInfo) -> list[Pubkey]: ...