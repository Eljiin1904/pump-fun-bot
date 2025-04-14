# src/core/priority_fee/dynamic_fee.py

import statistics
import asyncio # Add asyncio for potential sleep on error/retry
from typing import Optional, List, Any # Use List and Any

from solders.pubkey import Pubkey
# --- FIX: Import correct client type ---
from solana.rpc.async_api import AsyncClient # Needs the raw client
# --- FIX: Remove incorrect response type import ---
# from solders.rpc.responses import GetRecentPriorityFeesResp # REMOVE
from solana.exceptions import SolanaRpcException # Import for error handling
# --- End Fix ---
from . import PriorityFeePlugin
from ...utils.logger import get_logger

logger = get_logger(__name__)

# Define constants for retry logic
FEE_FETCH_MAX_RETRIES = 3
FEE_FETCH_DELAY = 0.5 # seconds

class DynamicPriorityFee(PriorityFeePlugin):
    """Dynamic priority fee plugin using getRecentPrioritizationFees."""

    def __init__(self, client: AsyncClient):
        """ Initialize the dynamic fee plugin. """
        self.client = client

    async def get_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        """
        Fetch the recent priority fee using getRecentPrioritizationFees with retries.

        Args:
            accounts: List of accounts to consider for the fee calculation.

        Returns:
            Optional[int]: Median priority fee in microlamports, or None if fetching fails.
        """
        last_error: Optional[Exception] = None
        for attempt in range(FEE_FETCH_MAX_RETRIES):
            try:
                account_param: List[str] = [str(account) for account in accounts] if accounts else []
                logger.debug(f"Fetching priority fees (Attempt {attempt+1}/{FEE_FETCH_MAX_RETRIES}). Accounts: {len(account_param)}")

                # --- FIX: Call correct RPC method ---
                # The response object structure is not strictly typed here, rely on attribute access
                response_value: Optional[List[Any]] = await self.client.get_recent_priority_fees(
                    account_param
                )
                # --- End Fix ---

                # Check if the response value (list of fees) exists and is not empty
                if response_value:
                    # Extract fees checking the structure of each item
                    fees = []
                    for fee_info in response_value:
                        if hasattr(fee_info, 'prioritization_fee'):
                            fees.append(fee_info.prioritization_fee)
                        else:
                            logger.warning(f"Fee info object lacks 'prioritization_fee': {fee_info}")

                    if not fees:
                        logger.warning(f"Priority fee response contained no valid fee data on attempt {attempt+1}.")
                        # Optionally retry even if empty, or return None immediately
                        # Let's return None for now if consistently empty
                        return None

                    # Calculate median
                    prior_fee = int(statistics.median(fees))
                    logger.debug(f"Calculated median priority fee: {prior_fee} microLamports")
                    return prior_fee # Success
                else:
                    # Response value itself was None or empty list
                    logger.warning(f"No priority fees returned by RPC on attempt {attempt+1}.")
                    # Continue to retry logic below if attempts remain

            except SolanaRpcException as e_rpc:
                last_error = e_rpc
                logger.error(f"RPC Exception fetching priority fee (Attempt {attempt+1}): {e_rpc}")
                # Check if it's a rate limit error
                if isinstance(e_rpc.__cause__, Exception) and "429" in str(e_rpc.__cause__):
                     logger.warning("Rate limit hit fetching priority fee. Retrying after delay...")
                # else: # Handle other specific RPC errors if needed
            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error fetching priority fee (Attempt {attempt+1}): {e}", exc_info=True)
                # Break on truly unexpected errors? Or retry? Let's retry for now.

            # If we haven't returned success yet, wait before retrying (if attempts left)
            if attempt < FEE_FETCH_MAX_RETRIES - 1:
                await asyncio.sleep(FEE_FETCH_DELAY * (attempt + 1)) # Simple linear backoff for fees
            else:
                logger.error(f"Failed to fetch recent priority fee after {FEE_FETCH_MAX_RETRIES} attempts. Last error: {last_error}")
                return None # Failed all attempts

        return None # Should be unreachable, but ensures None is returned on failure