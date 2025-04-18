# src/core/client.py

import asyncio
import logging
from typing import Optional, Any

from solana.rpc.api import Client as SolanaSyncClient
from solana.rpc.async_api import AsyncClient as SolanaAsyncClient
# Import CommitmentLevel and CommitmentConfig
from solders.commitment_config import CommitmentLevel, CommitmentConfig
# Keep Commitment for type hinting if used elsewhere
from solana.rpc.commitment import Commitment
from solana.rpc.types import TxOpts

from solders.rpc.responses import (
    GetTokenAccountBalanceResp, GetTokenSupplyResp, GetAccountInfoResp,
    GetLatestBlockhashResp, SimulateTransactionResp
)
from solana.exceptions import SolanaRpcException
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.transaction_status import (
    TransactionConfirmationStatus,
    TransactionStatus
)

from ..utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_COMMITMENT = CommitmentLevel.Confirmed  # Use CommitmentLevel

# Helper to consistently get CommitmentLevel enum
def _to_commitment_level(commitment: Any) -> CommitmentLevel:
    """Converts various representations to CommitmentLevel."""
    if isinstance(commitment, CommitmentLevel):
        return commitment
    if isinstance(commitment, CommitmentConfig):
        return commitment.commitment  # Extract level
    # Handle string representations
    name = str(commitment).lower().replace("-", "_")
    if name == "processed":
        return CommitmentLevel.Processed
    if name == "confirmed":
        return CommitmentLevel.Confirmed
    if name == "finalized":
        return CommitmentLevel.Finalized
    # Handle TransactionConfirmationStatus
    if hasattr(commitment, "name"):
        name = commitment.name.lower()
        if name == "processed":
            return CommitmentLevel.Processed
        if name == "confirmed":
            return CommitmentLevel.Confirmed
        if name == "finalized":
            return CommitmentLevel.Finalized
    logger.warning(f"Could not map commitment '{commitment}' to CommitmentLevel, using default: {DEFAULT_COMMITMENT}")
    return DEFAULT_COMMITMENT

# Helper to convert CommitmentLevel to TransactionConfirmationStatus
def _level_to_status(level: CommitmentLevel) -> TransactionConfirmationStatus:
    if level == CommitmentLevel.Processed:
        return TransactionConfirmationStatus.Processed
    if level == CommitmentLevel.Confirmed:
        return TransactionConfirmationStatus.Confirmed
    if level == CommitmentLevel.Finalized:
        return TransactionConfirmationStatus.Finalized
    return TransactionConfirmationStatus.Confirmed


class SolanaClient:
    """ Wraps sync and async Solana clients with improved error handling. """

    def __init__(self, rpc_endpoint: str, wss_endpoint: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        logger.info(f"Initializing SolanaClient with RPC: {rpc_endpoint}")
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        self._timeout = timeout
        self.default_commitment_level = DEFAULT_COMMITMENT
        # Initialize underlying clients with CommitmentConfig
        default_cfg = CommitmentConfig(self.default_commitment_level)
        self._sync_client = SolanaSyncClient(rpc_endpoint, commitment=default_cfg)
        self._async_client = SolanaAsyncClient(rpc_endpoint, commitment=default_cfg)

    def get_async_client(self) -> SolanaAsyncClient:
        return self._async_client

    def get_sync_client(self) -> SolanaSyncClient:
        return self._sync_client

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        logger.debug("Closing async Solana client connection...")
        close_method = getattr(self._async_client, 'aclose', getattr(self._async_client, 'close', None))
        if close_method and asyncio.iscoroutinefunction(close_method):
            try:
                await close_method()
            except Exception as e:
                logger.error(f"Error closing async client: {e}")
        elif close_method:
            try:
                close_method()
            except Exception as e:
                logger.error(f"Error closing async client (sync close): {e}")
        logger.debug("Async Solana client connection closed.")

    # --- Methods requiring CommitmentConfig passed to underlying client ---

    async def get_account_info(self, pubkey: Pubkey, commitment: Optional[Any] = None, encoding: str = "base64") -> Optional[Any]:
        method_name = "get_account_info"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level)
            commit_cfg = CommitmentConfig(level)
            logger.debug(f"{method_name}({pubkey}) using commitment config: {commit_cfg}")
            resp: GetAccountInfoResp = await self._async_client.get_account_info(
                pubkey,
                commitment=commit_cfg,
                encoding=encoding
            )
            return resp.value if hasattr(resp, 'value') else None
        except KeyError as e:
            logger.error(f"Internal KeyError in {method_name} ({pubkey}): {e}.", exc_info=True)
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
        except Exception as e:
            logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True)
        return None

    async def get_token_account_balance(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[Any]:
        method_name = "get_token_account_balance"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level)
            commit_cfg = CommitmentConfig(level)
            logger.debug(f"{method_name}({pubkey}) using commitment config: {commit_cfg}")
            resp: GetTokenAccountBalanceResp = await self._async_client.get_token_account_balance(
                pubkey,
                commitment=commit_cfg
            )
            return resp.value if hasattr(resp, 'value') else None
        except KeyError as e:
            logger.error(f"Internal KeyError in {method_name} ({pubkey}): {e}.", exc_info=True)
        except SolanaRpcException as e_rpc:
            if "could not find account" in str(e_rpc).lower():
                logger.debug(f"Token account {pubkey} not found via RPC.")
                return None
            logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
        except Exception as e:
            logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True)
        return None

    async def get_token_supply(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[Any]:
        method_name = "get_token_supply"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level)
            commit_cfg = CommitmentConfig(level)
            logger.debug(f"{method_name}({pubkey}) using commitment config: {commit_cfg}")
            resp: GetTokenSupplyResp = await self._async_client.get_token_supply(
                pubkey,
                commitment=commit_cfg
            )
            return resp.value if hasattr(resp, 'value') else None
        except KeyError as e:
            logger.error(f"Internal KeyError in {method_name} ({pubkey}): {e}.", exc_info=True)
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
        except Exception as e:
            logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True)
        return None

    async def get_latest_blockhash(self, commitment: Optional[Any] = None) -> Optional[GetLatestBlockhashResp]:
        method_name = "get_latest_blockhash"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level)
            commit_cfg = CommitmentConfig(level)
            logger.debug(f"{method_name} using commitment config: {commit_cfg}")
            resp: GetLatestBlockhashResp = await self._async_client.get_latest_blockhash(
                commitment=commit_cfg
            )
            return resp if hasattr(resp, 'value') else None
        except KeyError as e:
            logger.error(f"Internal KeyError in {method_name}: {e}.", exc_info=True)
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name}: {e_rpc}")
        except Exception as e:
            logger.error(f"Unexpected Error {method_name}: {e}", exc_info=True)
        return None

    # --- Methods where TxOpts and CommitmentConfig are handled ---

    async def send_transaction(self, tx: VersionedTransaction, opts: TxOpts) -> Optional[Signature]:
        method_name = "send_transaction"
        try:
            signature_resp = await self._async_client.send_transaction(tx, opts=opts)
            return signature_resp.signature
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name}: {e_rpc}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected Error {method_name}: {e}", exc_info=True)
        return None

    async def confirm_transaction(self, signature: Signature, commitment: Optional[Any] = None,
                                  timeout_secs: int = 60, sleep_secs: float = 1.0) -> Optional[TransactionConfirmationStatus]:
        level = _to_commitment_level(commitment or self.default_commitment_level)
        target_status = _level_to_status(level)
        commit_cfg = CommitmentConfig(level)

        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout_secs:
            try:
                status_resp = await self._async_client.get_signature_statuses(
                    [signature], commitment=commit_cfg
                )
                if status_resp and status_resp.value and status_resp.value[0] is not None:
                    ts: TransactionStatus = status_resp.value[0]
                    if ts.err:
                        logger.warning(f"Transaction {signature} failed: {ts.err}")
                        return None
                    if ts.confirmation_status and ts.confirmation_status >= target_status:
                        return ts.confirmation_status
            except Exception:
                pass
            await asyncio.sleep(sleep_secs)
        logger.warning(f"Confirm timeout for {signature}")
        return None

    def get_default_send_options(self, skip_preflight: bool = False) -> TxOpts:
        return TxOpts(
            skip_preflight=skip_preflight,
            preflight_commitment=CommitmentConfig(self.default_commitment_level)
        )

    # Dependent convenience methods
    async def get_token_balance_lamports(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[int]:
        bal = await self.get_token_account_balance(pubkey, commitment)
        if bal and hasattr(bal, 'amount'):
            try:
                return int(bal.amount)
            except Exception:
                logger.error(f"Could not parse balance {bal.amount}")
        return None

    async def get_token_supply_lamports(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[int]:
        sup = await self.get_token_supply(pubkey, commitment)
        if sup and hasattr(sup, 'amount'):
            try:
                return int(sup.amount)
            except Exception:
                logger.error(f"Could not parse supply {sup.amount}")
        return None

    async def does_token_account_exist(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> bool:
        info = await self.get_account_info(pubkey, commitment)
        return info is not None
