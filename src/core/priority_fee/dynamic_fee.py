import asyncio
import statistics
from typing import Optional, List, Any, Sequence, Dict

from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment, Confirmed
from solana.exceptions import SolanaRpcException
from . import PriorityFeePlugin
from ...utils.logger import get_logger  # Adjust path if needed

logger = get_logger(__name__)

PRIORITY_FEE_FETCH_MAX_RETRIES = 3
PRIORITY_FEE_FETCH_DELAY = 0.5
DEFAULT_PRIORITY_FEE_PERCENTILE = 75


class DynamicPriorityFee(PriorityFeePlugin):
    """ Dynamic priority fee plugin using getRecentPrioritizationFees RPC. """

    def __init__(
        self,
        client: AsyncClient,
        target_accounts: Optional[Sequence[Pubkey]] = None,
        commitment: Commitment = Confirmed,
        percentile: int = DEFAULT_PRIORITY_FEE_PERCENTILE,
    ):
        if not 0 <= percentile <= 100:
            raise ValueError("Percentile must be between 0 and 100")

        self.client = client
        self.default_target_accounts = list(target_accounts) if target_accounts else []
        self.commitment = commitment
        self.percentile = percentile
        self.last_dynamic_fee: Optional[int] = None

    async def get_priority_fee(
        self,
        accounts: Optional[Sequence[Pubkey]] = None,
    ) -> Optional[int]:
        """Fetches recent prioritization fees and calculates a dynamic fee."""
        accounts_to_query = (
            [str(a) for a in accounts]
            if accounts is not None
            else [str(a) for a in self.default_target_accounts]
        )
        desc = "passed-in" if accounts is not None else "default"

        for attempt in range(1, PRIORITY_FEE_FETCH_MAX_RETRIES + 1):
            try:
                logger.debug(
                    f"Fetching prioritization fees (Attempt {attempt}/{PRIORITY_FEE_FETCH_MAX_RETRIES}), "
                    f"{desc} accounts: {accounts_to_query or ['Global']}, commitment: {self.commitment}"
                )

                # High-level method if implemented
                if hasattr(self.client, 'get_recent_prioritization_fees'):
                    resp = await self.client.get_recent_prioritization_fees(
                        locked_writable_accounts=accounts_to_query,
                        commitment=self.commitment,
                    )
                    raw = getattr(resp, 'value', None)
                else:
                    # Raw RPC fallback
                    params: Any = [accounts_to_query]
                    params.append({"commitment": self.commitment})
                    raw_resp: Dict[str, Any] = await self.client._provider.make_request(
                        "getRecentPrioritizationFees", params
                    )
                    result = raw_resp.get('result', raw_resp)
                    raw = result.get('value', result)

                if not isinstance(raw, list):
                    raise ValueError(f"Unexpected fees payload: {raw!r}")

                # Parse fees
                fees = [int(getattr(entry, 'prioritization_fee', entry)) for entry in raw if entry is not None]
                fees.sort()
                if not fees:
                    raise ValueError("No valid fees after parsing")

                # Select percentile index
                idx = min(int(len(fees) * self.percentile / 100), len(fees) - 1)
                fee_value = fees[idx]

                logger.debug(
                    f"Fees samples={len(fees)} min={fees[0]} max={fees[-1]} median={statistics.median(fees)}"
                )
                logger.info(
                    f"Calculated dynamic priority fee ({self.percentile}th percentile): {fee_value}"
                )

                self.last_dynamic_fee = fee_value
                return fee_value

            except (AttributeError, ValueError) as err:
                logger.error(
                    f"Error fetching prioritization fees (Attempt {attempt}): {err}",
                    exc_info=True
                )
                return self.last_dynamic_fee

            except SolanaRpcException as rpc_err:
                logger.error(
                    f"RPC exception fetching prioritization fees (Attempt {attempt}): {rpc_err}"
                )

            except Exception as e:
                logger.error(
                    f"Unexpected error (Attempt {attempt}): {e}",
                    exc_info=True
                )

            # Delay before retry
            if attempt < PRIORITY_FEE_FETCH_MAX_RETRIES:
                await asyncio.sleep(PRIORITY_FEE_FETCH_DELAY)

        logger.error(
            f"Failed to fetch priority fees after {PRIORITY_FEE_FETCH_MAX_RETRIES} attempts, "
            f"returning last known fee: {self.last_dynamic_fee}"
        )
        return self.last_dynamic_fee
