# src/core/priority_fee/dynamic_fee.py (No Fallback + Enhanced Logging)

import asyncio
import statistics
import logging
from typing import List, Optional, Any

from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
from solana.exceptions import SolanaRpcException
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

FEE_FETCH_MAX_RETRIES = 3
FEE_FETCH_DELAY = 0.6
DEFAULT_MAX_FEE = 1_000_000

class DynamicPriorityFee:
    """
    Calculates dynamic priority fee using client's method ONLY.
    Falls back gracefully to 0 if the method is unavailable or fails after retries.
    Includes enhanced logging for debugging.
    """
    def __init__(self, client: SolanaPyAsyncClient):
        if not isinstance(client, SolanaPyAsyncClient):
            raise TypeError(f"Requires AsyncClient, not {type(client)}")
        self.client = client
        self.last_dynamic_fee: Optional[int] = None
        self.method_supported: Optional[bool] = None
        # --- Log client details more thoroughly ---
        client_endpoint = "[Unavailable]"
        if hasattr(self.client, '_provider') and hasattr(self.client._provider, 'endpoint_uri'):
             client_endpoint = self.client._provider.endpoint_uri # Use private if needed for logging
        elif hasattr(self.client, 'endpoint_uri'): # Check public attribute
             client_endpoint = self.client.endpoint_uri
        logger.info(f"DynamicPriorityFee initialized. Client Type: {type(self.client)}, Client ID: {id(self.client)}, Endpoint: {client_endpoint}")
        # --- End Logging ---

    async def get_priority_fee(self, accounts: Optional[List[Pubkey]] = None) -> int:
        target_accounts = accounts[:128] if accounts else []
        last_error: Optional[Exception] = None

        # Check method support first
        if self.method_supported is False: return 0
        if not hasattr(self.client, "get_recent_prioritization_fees"):
            if self.method_supported is None: logger.warning("Client lacks 'get_recent_prioritization_fees'. Dynamic fees disabled."); self.method_supported = False
            return 0
        elif self.method_supported is None: self.method_supported = True

        for attempt in range(FEE_FETCH_MAX_RETRIES):
            try:
                # --- Enhanced Logging Before Call ---
                client_endpoint_runtime = "[Unavailable]"
                if hasattr(self.client, '_provider') and hasattr(self.client._provider, 'endpoint_uri'):
                     client_endpoint_runtime = self.client._provider.endpoint_uri
                elif hasattr(self.client, 'endpoint_uri'):
                     client_endpoint_runtime = self.client.endpoint_uri
                logger.debug(f"Attempt {attempt+1}/{FEE_FETCH_MAX_RETRIES} calling get_recent_prioritization_fees. Client ID: {id(self.client)}, Endpoint: {client_endpoint_runtime}")
                # --- End Enhanced Logging ---

                response = await self.client.get_recent_prioritization_fees(target_accounts)
                fees_data = response.value if hasattr(response, 'value') else None

                if fees_data is not None:
                    fees = []
                    for fee_info in fees_data:
                        fee_val = getattr(fee_info, 'prioritization_fee', getattr(fee_info, 'prioritizationFee', None))
                        if fee_val is not None and fee_val > 0:
                            try: fees.append(int(fee_val))
                            except (ValueError, TypeError): logger.warning(f"Invalid fee format: {fee_val}")
                    if not fees: logger.warning(f"No valid positive fees (Attempt {attempt+1})."); last_error = ValueError("No valid fees")
                    else: median_fee = int(statistics.median(fees)); priority_fee = min(median_fee, DEFAULT_MAX_FEE); logger.debug(f"Dynamic fee success: {priority_fee}"); self.last_dynamic_fee = priority_fee; return priority_fee
                else: logger.warning(f"RPC response missing value (Attempt {attempt+1})."); last_error = ValueError("Response missing value")

            # --- Log Exact Exception ---
            except Exception as e:
                logger.warning(f"Fee fetch attempt {attempt+1} FAILED. Error Type: {type(e).__name__}, Error Msg: {e}", exc_info=False) # Log type and message clearly
                last_error = e
                if isinstance(e, AttributeError) and "get_recent_prioritization_fees" in str(e):
                     logger.error("AttributeError indicates method truly missing or client issue.")
                     self.method_supported = False; return 0 # Don't retry attribute errors usually
                # Add other non-retryable errors if needed
                if not isinstance(e, (SolanaRpcException, asyncio.TimeoutError)): # Don't retry totally unexpected errors
                     logger.error("Unexpected non-RPC/Timeout error during fee fetch.", exc_info=True)
                     return self.last_dynamic_fee or 0 # Fail fast
            # --- End Log Exact Exception ---

            # Retry Logic
            if attempt < FEE_FETCH_MAX_RETRIES - 1:
                delay = FEE_FETCH_DELAY * (2 ** attempt) # Exponential backoff
                logger.info(f"Retrying fee fetch after error: {type(last_error).__name__} in {delay:.2f}s...")
                await asyncio.sleep(delay)
            else: break # Exit loop

        logger.error(f"Failed fee fetch after {FEE_FETCH_MAX_RETRIES} attempts. Last error: {last_error}. Returning {self.last_dynamic_fee or 0}.");
        return self.last_dynamic_fee or 0

# --- END OF FILE ---