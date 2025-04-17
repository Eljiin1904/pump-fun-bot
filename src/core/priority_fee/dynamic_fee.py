# src/core/priority_fee/dynamic_fee.py

import asyncio
import statistics
from typing import Optional, List, Any, Sequence

from solders.pubkey import Pubkey
# Import the raw AsyncClient type for hinting
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment, Confirmed
from solana.exceptions import SolanaRpcException
from . import PriorityFeePlugin
from ...utils.logger import get_logger # Adjust path if needed

logger = get_logger(__name__)

PRIORITY_FEE_FETCH_MAX_RETRIES = 3
PRIORITY_FEE_FETCH_DELAY = 0.5
DEFAULT_PRIORITY_FEE_PERCENTILE = 75

class DynamicPriorityFee(PriorityFeePlugin):
    """ Dynamic priority fee plugin using get_recent_prioritization_fees. """

    def __init__(self,
                 # Client here is the raw AsyncClient based on the error
                 client: AsyncClient, # Hint with the actual type received
                 target_accounts: Optional[Sequence[Pubkey]] = None,
                 commitment: Commitment = Confirmed,
                 percentile: int = DEFAULT_PRIORITY_FEE_PERCENTILE
                ):
        if not 0 <= percentile <= 100:
            raise ValueError("Percentile must be between 0 and 100")

        self.client = client # This IS the raw AsyncClient instance
        self.default_target_accounts = list(target_accounts) if target_accounts else []
        self.commitment = commitment # Use the commitment passed during init
        self.percentile = percentile
        self.last_dynamic_fee: Optional[int] = None

    async def get_priority_fee(self, accounts: Optional[Sequence[Pubkey]] = None) -> Optional[int]:
        """ Fetches recent prioritization fees and calculates a dynamic fee. """
        accounts_to_query = list(accounts) if accounts is not None else self.default_target_accounts
        accounts_to_query = accounts_to_query or []
        log_target_desc = "passed-in" if accounts is not None else "default"
        log_accounts_list_str = str([str(a) for a in accounts_to_query]) if accounts_to_query else "Global"

        # Note: Using self.commitment passed during __init__, not the client's default
        rpc_commitment = self.commitment

        for attempt in range(PRIORITY_FEE_FETCH_MAX_RETRIES):
            try:
                logger.debug(f"Fetching recent prioritization fees (Attempt {attempt + 1}/{PRIORITY_FEE_FETCH_MAX_RETRIES}). Using {log_target_desc} accounts: {log_accounts_list_str}")

                # <<< FIX: Call directly on self.client (which is the raw AsyncClient) >>>
                # Ensure the passed AsyncClient instance actually has this method (check solana-py version if fails)
                response = await self.client.get_recent_prioritization_fees(
                    locked_writable_accounts=accounts_to_query,
                    commitment=rpc_commitment # Use the commitment set for the plugin
                )
                # <<< END FIX >>>

                # ... (rest of the fee processing logic remains the same) ...
                fees_data = response.value
                if not fees_data:
                    logger.warning(f"get_recent_prioritization_fees returned no data for accounts: {log_accounts_list_str}.")
                    return self.last_dynamic_fee

                recent_fees = sorted([fee.prioritization_fee for fee in fees_data])

                if not recent_fees:
                     logger.warning(f"No valid prioritization fees found in response for accounts: {log_accounts_list_str}.")
                     return self.last_dynamic_fee

                index = int(len(recent_fees) * (self.percentile / 100.0))
                index = min(index, len(recent_fees) - 1)
                calculated_fee = int(recent_fees[index])

                logger.debug(f"Recent fees ({len(recent_fees)} samples, Accounts: {log_accounts_list_str}): Min={min(recent_fees)}, Max={max(recent_fees)}, Median={statistics.median(recent_fees)}")
                logger.info(f"Calculated dynamic priority fee ({self.percentile}th percentile, Accounts: {log_accounts_list_str}): {calculated_fee} microLamports")

                self.last_dynamic_fee = calculated_fee
                return calculated_fee

            except AttributeError as attr_err:
                 # This error now likely means the solana-py version is too old or the passed client is wrong upstream
                 logger.error(f"AttributeError calling get_recent_prioritization_fees on the provided client (Attempt {attempt + 1}): {attr_err}. Check client init and solana-py version.", exc_info=True)
                 return self.last_dynamic_fee # Fallback
            except SolanaRpcException as rpc_e:
                logger.error(f"RPC error fetching prioritization fees (Attempt {attempt + 1}, Accounts: {log_accounts_list_str}): {rpc_e}")
            except Exception as e:
                logger.error(f"Unexpected error fetching prioritization fees (Attempt {attempt + 1}, Accounts: {log_accounts_list_str}): {e}", exc_info=True)

            if attempt < PRIORITY_FEE_FETCH_MAX_RETRIES - 1:
                await asyncio.sleep(PRIORITY_FEE_FETCH_DELAY)

        logger.error(f"Failed to fetch priority fees after {PRIORITY_FEE_FETCH_MAX_RETRIES} attempts (Accounts: {log_accounts_list_str}). Returning last known fee: {self.last_dynamic_fee}")
        return self.last_dynamic_fee