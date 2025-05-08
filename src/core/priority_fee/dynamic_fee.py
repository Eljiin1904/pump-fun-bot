# src/core/priority_fee/dynamic_fee.py
import asyncio
import time
import statistics
from typing import Optional, List  # Keep List
from solders.pubkey import Pubkey  # Keep Pubkey
from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient

# --- Corrected Imports ---
from src.utils.logger import get_logger
# Import the existing base class from __init__.py
from . import PriorityFeePlugin  # Use PriorityFeePlugin from __init__.py

# --- End Corrected Imports ---

logger = get_logger(__name__)


class DynamicPriorityFeePlugin(PriorityFeePlugin):  # Inherit from PriorityFeePlugin
    """Calculates priority fee based on recent fees observed on the network."""

    def __init__(self,
                 client: SolanaPyAsyncClient,  # Needs the client at init time now
                 lookback_slots: int = 20,
                 percentile: int = 50,
                 adjustment_factor: float = 1.0,
                 cache_duration: int = 5):
        if not isinstance(client, SolanaPyAsyncClient):
            raise TypeError("DynamicPriorityFeePlugin requires a raw solana-py AsyncClient during initialization")

        if not 0 <= percentile <= 100:
            raise ValueError("Percentile must be between 0 and 100")

        self.client = client  # Store the client
        self.lookback_slots = max(1, lookback_slots)
        self.percentile = percentile
        self.adjustment_factor = adjustment_factor
        self.cache_duration = max(0, cache_duration)

        self._cached_fee: Optional[int] = None
        self._last_fetch_time: float = 0
        # Store accounts to check if needed frequently, or expect them via a different mechanism
        self._accounts_to_check: Optional[List[Pubkey]] = None
        logger.info(
            f"DynamicPriorityFeePlugin initialized: Lookback={self.lookback_slots}, Perc={self.percentile}, Factor={self.adjustment_factor}, Cache={self.cache_duration}s")

    # Method to update accounts if needed before calculation
    def set_accounts_for_check(self, accounts: Optional[List[Pubkey]]):
        self._accounts_to_check = accounts

    async def get_priority_fee(self) -> int | None:  # Match base class signature
        """
        Fetches recent fees and calculates the fee based on the configured percentile.
        Uses the client provided during initialization and accounts set via set_accounts_for_check.
        """
        now = time.time()
        # Check cache
        if self.cache_duration > 0 and self._cached_fee is not None and (
                now - self._last_fetch_time) < self.cache_duration:
            logger.debug(f"Using cached dynamic priority fee: {self._cached_fee}")
            return self._cached_fee if self._cached_fee > 0 else None

        logger.debug(f"Fetching recent prioritization fees (slots={self.lookback_slots})...")
        calculated_fee = 0
        try:
            # Use the stored client and accounts
            response = await self.client.get_recent_prioritization_fees(
                locked_writable_accounts=self._accounts_to_check  # Use stored accounts
            )

            if not response or not response.value:
                logger.warning("get_recent_prioritization_fees returned no data.")
                self._cached_fee = 0;
                self._last_fetch_time = now
                return None

            # --- Fee Calculation Logic (same as before) ---
            fees = sorted([item.prioritization_fee for item in response.value if item.prioritization_fee > 0])
            if not fees:
                logger.info("No non-zero priority fees found in lookback window.")
                calculated_fee = 0
            else:
                index = min(len(fees) - 1, (len(fees) * self.percentile) // 100)
                percentile_fee = fees[index]
                adjusted_fee = int(percentile_fee * self.adjustment_factor)
                calculated_fee = max(0, adjusted_fee)
                logger.debug(
                    f"Dynamic fee calc: Found {len(fees)} fees. {self.percentile}th PercFee={percentile_fee}, AdjustedFee={calculated_fee}")
            # --- End Fee Calculation ---

            self._cached_fee = calculated_fee
            self._last_fetch_time = now
            return calculated_fee if calculated_fee > 0 else None

        except AttributeError as e_attr:
            logger.error(
                f"AttributeError calling get_recent_prioritization_fees: {e_attr}. Ensure solana-py version >= 0.30.0.")
            self._cached_fee = 0;
            self._last_fetch_time = now
            return None
        except Exception as e:
            logger.error(f"Error fetching/processing dynamic priority fees: {e}", exc_info=True)
            self._cached_fee = 0;
            self._last_fetch_time = now
            return None