# src/core/client.py

import asyncio
import logging
from typing import Optional, Any, List, Sequence

from solana.rpc.api import Client as SolanaSyncClient
from solana.rpc.async_api import AsyncClient as SolanaAsyncClient
from solders.commitment_config import CommitmentLevel, CommitmentConfig
from solana.rpc.commitment import Commitment, Processed, Confirmed, Finalized
# --- FIX: Correct import and usage for AccountEncoding ---
from solana.rpc.types import TxOpts, AccountEncoding # AccountEncoding is here
# --- END FIX ---

from solders.rpc.responses import (
    GetTokenAccountBalanceResp, GetTokenSupplyResp, GetAccountInfoResp,
    GetLatestBlockhashResp, SimulateTransactionResp, GetSignatureStatusesResp,
    GetTransactionResp
)

from solana.exceptions import SolanaRpcException
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.transaction_status import (
    TransactionConfirmationStatus,
    TransactionStatus
)
from solders.hash import Hash as Blockhash

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_SOLDERS_COMMITMENT_LEVEL = CommitmentLevel.Confirmed
DEFAULT_RPC_COMMITMENT = Confirmed
# --- FIX: Use the correct string literal for Base64 encoding ---
DEFAULT_ENCODING = "base64" # Use the string directly
# --- END FIX ---


# Helper functions remain the same
def _to_commitment_level(commitment: Any) -> CommitmentLevel:
    # ... (implementation as before) ...
    if isinstance(commitment, CommitmentLevel): return commitment
    if isinstance(commitment, CommitmentConfig): return commitment.commitment
    if hasattr(commitment, "name"): name = commitment.name.lower()
    elif isinstance(commitment, str): name = commitment.lower().replace("-", "_").replace(" ", "_")
    else: logger.warning(f"Could not determine commitment level from type {type(commitment)}, using default: {DEFAULT_SOLDERS_COMMITMENT_LEVEL}"); return DEFAULT_SOLDERS_COMMITMENT_LEVEL
    if name in ("processed", "recent"): return CommitmentLevel.Processed
    if name in ("confirmed", "single", "single_gossip"): return CommitmentLevel.Confirmed
    if name in ("finalized", "rooted", "max"): return CommitmentLevel.Finalized
    logger.warning(f"Could not map commitment name '{name}', using default: {DEFAULT_SOLDERS_COMMITMENT_LEVEL}"); return DEFAULT_SOLDERS_COMMITMENT_LEVEL

def _level_to_rpc_commitment(level: CommitmentLevel) -> Commitment:
    # ... (implementation as before) ...
    if level == CommitmentLevel.Processed: return Processed
    if level == CommitmentLevel.Confirmed: return Confirmed
    if level == CommitmentLevel.Finalized: return Finalized
    logger.warning(f"Unexpected CommitmentLevel {level}, defaulting to Confirmed."); return Confirmed

def _level_to_status(level: CommitmentLevel) -> TransactionConfirmationStatus:
    # ... (implementation as before) ...
    if level == CommitmentLevel.Processed: return TransactionConfirmationStatus.Processed
    if level == CommitmentLevel.Confirmed: return TransactionConfirmationStatus.Confirmed
    if level == CommitmentLevel.Finalized: return TransactionConfirmationStatus.Finalized
    logger.warning(f"Unexpected CommitmentLevel {level}, defaulting to Confirmed."); return TransactionConfirmationStatus.Confirmed


class SolanaClient:
    # __init__ and other methods remain the same
    def __init__(self, rpc_endpoint: str, wss_endpoint: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        logger.info(f"Initializing SolanaClient with RPC: {rpc_endpoint}")
        self.rpc_endpoint = rpc_endpoint; self.wss_endpoint = wss_endpoint; self._timeout = timeout
        self.default_commitment_level: CommitmentLevel = DEFAULT_SOLDERS_COMMITMENT_LEVEL
        self.default_commitment_config: CommitmentConfig = CommitmentConfig(self.default_commitment_level)
        self.default_rpc_commitment: Commitment = _level_to_rpc_commitment(self.default_commitment_level)
        self._sync_client = SolanaSyncClient(rpc_endpoint, commitment=self.default_commitment_config)
        self._async_client = SolanaAsyncClient(rpc_endpoint, commitment=self.default_commitment_config)
        logger.debug(f"Clients initialized with default commitment config: {self.default_commitment_config}")

    def get_async_client(self) -> SolanaAsyncClient: return self._async_client
    def get_sync_client(self) -> SolanaSyncClient: return self._sync_client
    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc_val, exc_tb): await self.close()
    async def close(self):
        logger.debug("Closing async Solana client connection...")
        close_method = getattr(self._async_client, 'aclose', getattr(self._async_client, 'close', None))
        if close_method and asyncio.iscoroutinefunction(close_method):
            try: await close_method()
            except Exception as e: logger.error(f"Error closing async client (awaitable): {e}")
        elif close_method:
            try: close_method()
            except Exception as e: logger.error(f"Error closing async client (sync close): {e}")
        else: logger.warning("Could not find a close method on the async client.")
        logger.debug("Async Solana client connection closed.")


    async def get_recent_prioritization_fees( self, pubkeys: Optional[List[Pubkey]] = None, commitment: Optional[Any] = None ) -> Optional[List[Any]]:
        method_name = "get_recent_prioritization_fees"
        try:
            accounts_list = pubkeys if pubkeys else []
            logger.debug(f"{method_name}({[str(pk) for pk in accounts_list] or 'Global'}) calling...")
            response = await self._async_client.get_recent_prioritization_fees( locked_writable_accounts=accounts_list )
            if response and hasattr(response, 'value'):
                 fee_data_list = response.value
                 if isinstance(fee_data_list, list):
                     if all(hasattr(item, 'slot') and hasattr(item, 'prioritization_fee') for item in fee_data_list): logger.debug(f"{method_name} received {len(fee_data_list)} fee samples."); return fee_data_list
                     elif not fee_data_list: logger.debug(f"{method_name} received empty list."); return []
                     else: logger.warning(f"{method_name} received list, elements lack attributes."); return None
                 elif fee_data_list is None: logger.debug(f"{method_name} returned null value."); return []
                 else: logger.error(f"{method_name} returned unexpected 'value' type: {type(fee_data_list)}"); return None
            else: logger.error(f"{method_name} returned unexpected response structure: {response}"); return None
        except AttributeError as e: logger.critical(f"AttributeError {method_name}: {e}. Update solana-py?", exc_info=True)
        except SolanaRpcException as e_rpc: logger.error(f"RPC Exception {method_name}: {e_rpc}")
        except Exception as e: logger.error(f"Unexpected Error {method_name}: {e}", exc_info=True)
        return None

    async def get_account_info(self, pubkey: Pubkey, commitment: Optional[Any] = None, encoding: AccountEncoding = DEFAULT_ENCODING) -> Optional[Any]:
        method_name = "get_account_info"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level); rpc_commitment = _level_to_rpc_commitment(level)
            # Ensure encoding uses the (now correct) string default
            logger.debug(f"{method_name}({pubkey}) using RPC commitment: {rpc_commitment}, encoding: {encoding}")
            resp: GetAccountInfoResp = await self._async_client.get_account_info(pubkey, commitment=rpc_commitment, encoding=encoding)
            return resp.value if resp and hasattr(resp, 'value') else None
        except KeyError as e: logger.error(f"KeyError {method_name} ({pubkey}): {e}.", exc_info=True)
        except SolanaRpcException as e_rpc: logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
        except Exception as e: logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True)
        return None

    async def get_token_account_balance(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[Any]:
        method_name = "get_token_account_balance"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level); rpc_commitment = _level_to_rpc_commitment(level)
            logger.debug(f"{method_name}({pubkey}) using RPC commitment: {rpc_commitment}")
            resp: GetTokenAccountBalanceResp = await self._async_client.get_token_account_balance(pubkey, commitment=rpc_commitment)
            return resp.value if resp and hasattr(resp, 'value') else None
        except KeyError as e: logger.error(f"KeyError {method_name} ({pubkey}): {e}.", exc_info=True)
        except SolanaRpcException as e_rpc:
            error_str = str(e_rpc).lower();
            if "could not find account" in error_str or "accountnotfound" in error_str: logger.debug(f"Token account {pubkey} not found via RPC ({method_name})."); return None
            else: logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
        except Exception as e: logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True)
        return None

    async def get_token_supply(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[Any]:
        method_name = "get_token_supply"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level); rpc_commitment = _level_to_rpc_commitment(level)
            logger.debug(f"{method_name}({pubkey}) using RPC commitment: {rpc_commitment}")
            resp: GetTokenSupplyResp = await self._async_client.get_token_supply(pubkey, commitment=rpc_commitment)
            return resp.value if resp and hasattr(resp, 'value') else None
        except KeyError as e: logger.error(f"KeyError {method_name} ({pubkey}): {e}.", exc_info=True)
        except SolanaRpcException as e_rpc:
            error_str = str(e_rpc).lower();
            if "could not find account" in error_str or "accountnotfound" in error_str: logger.debug(f"Token mint account {pubkey} not found via RPC ({method_name})."); return None
            else: logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
        except Exception as e: logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True)
        return None

    async def get_latest_blockhash(self, commitment: Optional[Any] = None) -> Optional[Blockhash]:
        method_name = "get_latest_blockhash"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level); rpc_commitment = _level_to_rpc_commitment(level)
            logger.debug(f"{method_name} using RPC commitment: {rpc_commitment}")
            resp: GetLatestBlockhashResp = await self._async_client.get_latest_blockhash(commitment=rpc_commitment)
            return resp.value.blockhash if resp and hasattr(resp, 'value') and hasattr(resp.value, 'blockhash') else None
        except KeyError as e: logger.error(f"KeyError {method_name}: {e}.", exc_info=True)
        except SolanaRpcException as e_rpc: logger.error(f"RPC Exception {method_name}: {e_rpc}")
        except Exception as e: logger.error(f"Unexpected Error {method_name}: {e}", exc_info=True)
        return None

    async def send_transaction(self, tx: VersionedTransaction, opts: TxOpts) -> Optional[Signature]:
        method_name = "send_transaction"
        try:
            logger.debug(f"{method_name} using opts: {opts}")
            resp = await self._async_client.send_transaction(tx, opts=opts)
            sig = getattr(resp, 'signature', None);
            if sig is None and hasattr(resp, 'value'): sig = getattr(resp.value, 'signature', None)
            if isinstance(sig, Signature): return sig;
            else: logger.error(f"{method_name} did not return valid Signature. Response: {resp}"); return None
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name}: {e_rpc}")
            error_data = getattr(e_rpc, 'error_data', None)
            if error_data: logger.error(f"RPC Error Data: {error_data}")
            else: logger.error(f"Transaction Error: {getattr(e_rpc, 'error_msg', str(e_rpc))}")
        except Exception as e: logger.error(f"Unexpected Error {method_name}: {e}", exc_info=True)
        return None

    async def simulate_transaction(self, tx: VersionedTransaction, commitment: Optional[Any] = None, encoding: AccountEncoding = DEFAULT_ENCODING) -> Optional[SimulateTransactionResp]:
        method_name = "simulate_transaction"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level); commit_cfg = CommitmentConfig(level)
            # Ensure encoding uses the (now correct) string default
            logger.debug(f"{method_name} using commitment: {commit_cfg}, encoding: {encoding}")
            resp: SimulateTransactionResp = await self._async_client.simulate_transaction(tx, commitment=commit_cfg, encoding=encoding, sig_verify=False, replace_recent_blockhash=True)
            return resp
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name}: {e_rpc}")
            error_data = getattr(e_rpc, 'error_data', None)
            if error_data: logger.error(f"Simulation Error Data: {error_data}")
            else: logger.error(f"Simulation Error: {getattr(e_rpc, 'error_msg', str(e_rpc))}")
        except Exception as e: logger.error(f"Unexpected Error {method_name}: {e}", exc_info=True)
        return None

    async def confirm_transaction(self, signature: Signature, commitment: Optional[Any] = None, last_valid_block_height: Optional[int] = None, timeout_secs: int = 60, sleep_secs: float = 1.0 ) -> Optional[TransactionStatus]:
        method_name = "confirm_transaction"
        try:
            level = _to_commitment_level(commitment or self.default_commitment_level); target_status = _level_to_status(level); commit_cfg = CommitmentConfig(level)
            logger.debug(f"Confirming {signature} target {target_status} (config {commit_cfg})"); start_time = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - start_time) < timeout_secs:
                try: # Inner try
                    resp: GetSignatureStatusesResp = await self._async_client.get_signature_statuses([signature], search_transaction_history=True)
                    if resp and resp.value and resp.value[0] is not None:
                        tx_status: TransactionStatus = resp.value[0]; logger.debug(f"Status check {signature}: {tx_status.confirmation_status}");
                        if tx_status.err: logger.warning(f"Tx {signature} confirmed error: {tx_status.err}"); return tx_status
                        current_status = tx_status.confirmation_status;
                        if current_status and current_status >= target_status: logger.debug(f"Tx {signature} reached target status {target_status}."); return tx_status
                except SolanaRpcException as e_rpc: logger.warning(f"RPC Error status check {signature}: {e_rpc}")
                except Exception as e: logger.error(f"Unexpected error status check {signature}: {e}", exc_info=True)
                await asyncio.sleep(sleep_secs) # Wait before next check
            logger.warning(f"Confirmation timeout {signature} after {timeout_secs}s.")
        except Exception as e: logger.error(f"Unexpected Error {method_name} for {signature}: {e}", exc_info=True)
        return None