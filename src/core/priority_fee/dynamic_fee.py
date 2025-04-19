import asyncio
import statistics
import logging
from typing import List, Optional, Any
from httpx import AsyncClient

logger = logging.getLogger(__name__)

FEE_FETCH_MAX_RETRIES = 3
FEE_FETCH_DELAY = 0.5  # seconds

class DynamicPriorityFee:
    """Dynamic priority fee plugin using get_recent_prioritization_fees."""
    def __init__(self, client):
        """ Initialize the dynamic fee plugin. """
        self.client = client  # Expecting the raw solana-py AsyncClient
        self.last_dynamic_fee: Optional[int] = None

    async def get_priority_fee(self, accounts: Optional[List] = None) -> Optional[int]:
        """Fetch dynamic priority fee. Returns median fee or last known fee if errors."""
        account_list: List[str] = [str(acc) for acc in accounts[:128]] if accounts else []
        last_error: Optional[Exception] = None
        for attempt in range(FEE_FETCH_MAX_RETRIES):
            try:
                logger.debug(f"Fetching priority fees (Attempt {attempt+1}/{FEE_FETCH_MAX_RETRIES}). Accounts: {account_list}")
                # Attempt to call solana-py AsyncClient method if available
                if hasattr(self.client, "get_recent_prioritization_fees"):
                    response = await self.client.get_recent_prioritization_fees(account_list)
                    fees_data = response.value if hasattr(response, 'value') else response  # solders response might have .value
                else:
                    # Fallback: direct RPC request via HTTP
                    body = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getRecentPrioritizationFees",
                        "params": [account_list]
                    }
                    async with AsyncClient(timeout=5.0) as http_client:
                        resp = await http_client.post(self.client._provider.endpoint_uri, json=body)
                        resp.raise_for_status()
                        json_data = resp.json()
                    result = json_data.get("result")
                    if result is None:
                        raise Exception(f"RPC returned no result: {json_data}")
                    fees_data = result
                # Process fees_data (expected list of fee info dicts or objects)
                if not fees_data:
                    logger.warning(f"No prioritization fee data returned on attempt {attempt+1}.")
                    if attempt < FEE_FETCH_MAX_RETRIES - 1:
                        await asyncio.sleep(FEE_FETCH_DELAY * (attempt + 1))
                        continue
                    else:
                        return self.last_dynamic_fee or 0
                fees = []
                for fee_info in fees_data:
                    # fee_info could be dict or object
                    if isinstance(fee_info, dict):
                        fee_val = fee_info.get("prioritizationFee")
                    else:
                        fee_val = getattr(fee_info, 'prioritization_fee', None) or getattr(fee_info, 'prioritizationFee', None)
                    if fee_val is not None:
                        try:
                            fees.append(int(fee_val))
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid fee value format: {fee_val}")
                if not fees:
                    logger.warning(f"No valid fee values on attempt {attempt+1}.")
                    if attempt < FEE_FETCH_MAX_RETRIES - 1:
                        await asyncio.sleep(FEE_FETCH_DELAY * (attempt + 1))
                        continue
                    else:
                        return self.last_dynamic_fee or 0
                # Take median of collected fees
                priority_fee = int(statistics.median(fees))
                logger.debug(f"Calculated median priority fee: {priority_fee} microLamports from {len(fees)} samples.")
                self.last_dynamic_fee = priority_fee
                return priority_fee
            except Exception as e:
                last_error = e
                # If method is unsupported, break and fallback to 0
                if isinstance(e, AttributeError) and "get_recent_prioritization_fees" in str(e):
                    logger.error("AsyncClient has no get_recent_prioritization_fees method. Falling back to 0 priority fee.")
                    break
                logger.error(f"Error fetching priority fee (Attempt {attempt+1}): {e}")
                if attempt < FEE_FETCH_MAX_RETRIES - 1:
                    await asyncio.sleep(FEE_FETCH_DELAY * (attempt + 1))
        logger.error(f"Failed to fetch priority fee after {FEE_FETCH_MAX_RETRIES} attempts. Last error: {last_error}")
        return self.last_dynamic_fee or 0
