# src/core/priority_fee/dynamic_fee.py

import asyncio
import statistics
from typing import Optional, List, Any, Sequence, Dict

from solders.pubkey import Pubkey
# --- FIX START: Remove incorrect import ---
# from solders.rpc.types import PrioritizationFee # Removed this
# --- FIX END ---
from ..client import SolanaClient # Use your client wrapper
from solana.rpc.commitment import Commitment, Confirmed # Keep solana-py type for input flexibility
from solana.exceptions import SolanaRpcException
from ...utils.logger import get_logger

logger = get_logger(__name__)

PRIORITY_FEE_FETCH_MAX_RETRIES = 3
PRIORITY_FEE_FETCH_DELAY = 0.75
PRIORITY_FEE_BACKOFF_FACTOR = 1.5
DEFAULT_PRIORITY_FEE_PERCENTILE = 75
MIN_PRIORITY_FEE = 1
MAX_PRIORITY_FEE = 500000

class DynamicPriorityFee:
    def __init__(
        self, client: SolanaClient, target_accounts: Optional[Sequence[Pubkey]] = None,
        commitment: Any = Confirmed, percentile: int = DEFAULT_PRIORITY_FEE_PERCENTILE,
        min_fee: int = MIN_PRIORITY_FEE, max_fee: int = MAX_PRIORITY_FEE,
    ):
        if not 0 <= percentile <= 100: raise ValueError("Percentile must be between 0 and 100")
        self.client = client; self.default_target_accounts = list(target_accounts) if target_accounts else []
        self.commitment = commitment; self.percentile = percentile; self.min_fee = min_fee
        self.max_fee = max_fee; self.last_dynamic_fee: Optional[int] = None

    async def get_priority_fee( self, accounts: Optional[Sequence[Pubkey]] = None ) -> Optional[int]:
        target_accounts = accounts if accounts is not None else self.default_target_accounts
        accounts_to_query = list(target_accounts)
        desc = "passed-in" if accounts is not None else "default"
        last_error: Optional[Exception] = None

        for attempt in range(1, PRIORITY_FEE_FETCH_MAX_RETRIES + 1):
            try:
                logger.debug(f"Fetching prioritization fees (Attempt {attempt}/{PRIORITY_FEE_FETCH_MAX_RETRIES}), {desc} accounts: {[str(a) for a in accounts_to_query] or ['Global']}")

                # --- FIX START: Call client wrapper, expect List[Any] ---
                # The objects in the list should have .slot and .prioritization_fee
                fee_data_list: Optional[List[Any]] = await self.client.get_recent_prioritization_fees(
                    pubkeys=accounts_to_query
                    # Pass commitment if needed by wrapper: commitment=self.commitment
                )
                # --- FIX END ---

                if fee_data_list is not None: # Check if list was returned (even empty is ok)
                    # Extract fees using attribute access (duck typing)
                    all_fees = []
                    for fee_sample in fee_data_list:
                        # Safely access the attribute
                        fee_val = getattr(fee_sample, 'prioritization_fee', None)
                        if fee_val is not None and fee_val > 0:
                             all_fees.append(fee_val)

                    if not all_fees:
                        logger.warning(f"No non-zero priority fees found in samples (attempt {attempt}).")
                        last_error = ValueError("No non-zero fees found in response samples.")
                    else:
                        all_fees.sort()
                        index = min(int(len(all_fees) * self.percentile / 100), len(all_fees) - 1)
                        fee_value = all_fees[index]
                        logger.debug(f"Fees samples={len(all_fees)} min={all_fees[0]} max={all_fees[-1]} median={statistics.median(all_fees)} {self.percentile}th_perc={fee_value}")
                        clamped_fee = max(self.min_fee, min(fee_value, self.max_fee))
                        self.last_dynamic_fee = clamped_fee
                        logger.info(f"Calculated dynamic priority fee ({self.percentile}th percentile): {self.last_dynamic_fee} micro-lamports (clamped)")
                        return self.last_dynamic_fee # Success!
                else: # Client returned None, indicating an error during the call
                     logger.warning(f"Client failed to return fee data (attempt {attempt}). Check client logs.")
                     last_error = RuntimeError("Client error fetching prioritization fees.")

            except (AttributeError, ValueError, RuntimeError) as err:
                logger.error(f"Error processing prioritization fees (Attempt {attempt}): {err}")
                last_error = err
            except SolanaRpcException as rpc_err:
                logger.error(f"RPC exception fetching prioritization fees (Attempt {attempt}): {rpc_err}")
                last_error = rpc_err
            except Exception as e:
                logger.error(f"Unexpected error fetching/processing fees (Attempt {attempt}): {e}", exc_info=True)
                last_error = e

            if attempt < PRIORITY_FEE_FETCH_MAX_RETRIES:
                delay = PRIORITY_FEE_FETCH_DELAY * (PRIORITY_FEE_BACKOFF_FACTOR ** (attempt - 1))
                logger.info(f"Retrying priority fee fetch in {delay:.2f}s...")
                await asyncio.sleep(delay)

        logger.error( f"Failed to fetch priority fees after {PRIORITY_FEE_FETCH_MAX_RETRIES} attempts. Last error: {last_error}. Returning fallback.")
        return self.last_dynamic_fee if self.last_dynamic_fee is not None else self.min_fee