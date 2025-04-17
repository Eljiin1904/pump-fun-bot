# src/core/client.py

import asyncio
import logging
from typing import Optional, Any, List, Union, Tuple

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
DEFAULT_COMMITMENT = CommitmentLevel.Confirmed # Use CommitmentLevel

# Helper to consistently get CommitmentLevel enum
def _to_commitment_level(commitment: Any) -> CommitmentLevel:
    """Converts various representations to CommitmentLevel."""
    if isinstance(commitment, CommitmentLevel): return commitment
    if isinstance(commitment, CommitmentConfig): return commitment.commitment # Extract level
    # Handle string representations
    name = str(commitment).lower().replace("-", "_")
    if name == "processed": return CommitmentLevel.Processed
    if name == "confirmed": return CommitmentLevel.Confirmed
    if name == "finalized": return CommitmentLevel.Finalized
    # Handle old TransactionConfirmationStatus enums if necessary
    if hasattr(commitment, "name"):
        name = commitment.name.lower()
        if name == "processed": return CommitmentLevel.Processed
        if name == "confirmed": return CommitmentLevel.Confirmed
        if name == "finalized": return CommitmentLevel.Finalized
    logger.warning(f"Could not map commitment '{commitment}' to CommitmentLevel, using default: {DEFAULT_COMMITMENT}")
    return DEFAULT_COMMITMENT

# Helper to convert CommitmentLevel to TransactionConfirmationStatus for comparison
def _level_to_status(level: CommitmentLevel) -> TransactionConfirmationStatus:
    if level == CommitmentLevel.Processed: return TransactionConfirmationStatus.Processed
    if level == CommitmentLevel.Confirmed: return TransactionConfirmationStatus.Confirmed
    if level == CommitmentLevel.Finalized: return TransactionConfirmationStatus.Finalized
    return TransactionConfirmationStatus.Confirmed # Default fallback


class SolanaClient:
    """ Wraps sync and async Solana clients with improved error handling. """

    def __init__(self, rpc_endpoint: str, wss_endpoint: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        logger.info(f"Initializing SolanaClient with RPC: {rpc_endpoint}")
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        self._timeout = timeout
        # Store the default CommitmentLevel
        self.default_commitment_level = DEFAULT_COMMITMENT
        # Initialize underlying clients with CommitmentConfig
        self._sync_client = SolanaSyncClient(rpc_endpoint, commitment=CommitmentConfig(self.default_commitment_level))
        self._async_client = SolanaAsyncClient(rpc_endpoint, commitment=CommitmentConfig(self.default_commitment_level))

    def get_async_client(self) -> SolanaAsyncClient: return self._async_client
    def get_sync_client(self) -> SolanaSyncClient: return self._sync_client

    # ... __aenter__, __aexit__, close ...
    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc_val, exc_tb): await self.close()
    async def close(self):
        logger.debug("Closing async Solana client connection...")
        close_method = getattr(self._async_client, 'aclose', getattr(self._async_client, 'close', None))
        if close_method and asyncio.iscoroutinefunction(close_method):
            try: await close_method()
            except Exception as e: logger.error(f"Error closing async client: {e}")
        elif close_method:
            try: close_method()
            except Exception as e: logger.error(f"Error closing async client (sync close): {e}")
        logger.debug("Async Solana client connection closed.")

    # --- Methods requiring CommitmentLevel passed to underlying client ---

    async def get_account_info(self, pubkey: Pubkey, commitment: Optional[Any] = None, encoding: str = "base64") -> Optional[Any]:
        method_name = "get_account_info"
        try:
            # <<< FIX: Convert input commitment to CommitmentLevel enum for the call >>>
            level_to_use = _to_commitment_level(commitment or self.default_commitment_level)
            logger.debug(f"{method_name} using commitment level: {level_to_use}")
            resp: GetAccountInfoResp = await self._async_client.get_account_info(
                pubkey,
                commitment=level_to_use, # Pass the CommitmentLevel enum
                encoding=encoding
            )
            if hasattr(resp, 'value'): return resp.value
            else: logger.error(f"{method_name} response for {pubkey} lacks 'value'. Response: {resp}"); return None
        except KeyError as e: # Catch potential KeyErrors from internal commitment mapping
             logger.error(f"Internal KeyError in {method_name} ({pubkey}) with commitment '{commitment}': {e}. Check solana-py version/compatibility.", exc_info=True); return None
        except SolanaRpcException as e_rpc: logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}"); return None
        except Exception as e: logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True); return None

    async def get_token_account_balance(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[Any]:
        method_name = "get_token_account_balance"
        try:
            # <<< FIX: Convert input commitment to CommitmentLevel enum for the call >>>
            level_to_use = _to_commitment_level(commitment or self.default_commitment_level)
            logger.debug(f"{method_name} using commitment level: {level_to_use}")
            resp: GetTokenAccountBalanceResp = await self._async_client.get_token_account_balance(
                pubkey,
                commitment=level_to_use # Pass the CommitmentLevel enum
            )
            if hasattr(resp, 'value'): return resp.value
            elif resp and hasattr(resp, 'message') and "could not find account" in resp.message.lower(): logger.debug(f"Token account {pubkey} not found."); return None
            else: logger.error(f"{method_name} response for {pubkey} lacks 'value' or structure. Response: {resp}"); return None
        except KeyError as e: logger.error(f"Internal KeyError in {method_name} ({pubkey}) with commitment '{commitment}': {e}.", exc_info=True); return None
        except SolanaRpcException as e_rpc:
            if "could not find account" in str(e_rpc).lower(): logger.debug(f"Token account {pubkey} not found via RPC Exception."); return None
            logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}"); return None
        except Exception as e: logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True); return None

    async def get_token_supply(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[Any]:
        method_name = "get_token_supply"
        try:
            # <<< FIX: Convert input commitment to CommitmentLevel enum for the call >>>
            level_to_use = _to_commitment_level(commitment or self.default_commitment_level)
            logger.debug(f"{method_name} using commitment level: {level_to_use}")
            resp: GetTokenSupplyResp = await self._async_client.get_token_supply(
                pubkey,
                commitment=level_to_use # Pass the CommitmentLevel enum
            )
            if hasattr(resp, 'value'): return resp.value
            else: logger.error(f"{method_name} response for {pubkey} lacks 'value'. Response: {resp}"); return None
        except KeyError as e: logger.error(f"Internal KeyError in {method_name} ({pubkey}) with commitment '{commitment}': {e}.", exc_info=True); return None
        except SolanaRpcException as e_rpc: logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}"); return None
        except Exception as e: logger.error(f"Unexpected Error {method_name} {pubkey}: {e}", exc_info=True); return None

    async def get_latest_blockhash(self, commitment: Optional[Any] = None) -> Optional[GetLatestBlockhashResp]:
        method_name = "get_latest_blockhash"
        try:
             # <<< FIX: Convert input commitment to CommitmentLevel enum for the call >>>
            level_to_use = _to_commitment_level(commitment or self.default_commitment_level)
            logger.debug(f"{method_name} using commitment level: {level_to_use}")
            resp: GetLatestBlockhashResp = await self._async_client.get_latest_blockhash(
                commitment=level_to_use # Pass the CommitmentLevel enum
            )
            if hasattr(resp, 'value'): return resp
            else: logger.error(f"{method_name} response lacks 'value'. Response: {resp}"); return None
        except KeyError as e: logger.error(f"Internal KeyError in {method_name} with commitment '{commitment}': {e}.", exc_info=True); return None
        except SolanaRpcException as e_rpc: logger.error(f"RPC Exception {method_name}: {e_rpc}"); return None
        except Exception as e: logger.error(f"Unexpected Error {method_name}: {e}", exc_info=True); return None

    # --- Methods where CommitmentLevel seems handled correctly by library ---

    async def send_transaction(self, tx: VersionedTransaction, opts: TxOpts) -> Optional[Signature]:
        method_name = "send_transaction"
        try:
            # TxOpts uses CommitmentLevel directly, which seems correct based on previous fix.
            # Ensure get_default_send_options provides the correct CommitmentLevel in opts.
            logger.debug(f"Sending transaction with options: SkipPreflight={opts.skip_preflight}, PreflightCommitment={opts.preflight_commitment}")
            signature_resp = await self._async_client.send_transaction(tx, opts=opts)
            signature = signature_resp.signature
            logger.debug(f"Transaction sent. Signature: {signature}")
            return signature
        except SolanaRpcException as e_rpc: logger.error(f"RPC Exception {method_name}: {e_rpc}", exc_info=True); return None
        except Exception as e: logger.error(f"Unexpected Error {method_name}: {e}", exc_info=True); return None

    async def confirm_transaction(self, signature: Signature, commitment: Optional[Any] = None, timeout_secs: int = 60, sleep_secs: float = 1.0) -> Optional[TransactionConfirmationStatus]:
        # Convert target to CommitmentLevel first
        target_commitment_level = _to_commitment_level(commitment or self.default_commitment_level)
        # Convert to TransactionConfirmationStatus for comparison >=
        target_conf_status = _level_to_status(target_commitment_level)

        logger.debug(f"Confirming {signature} with target status {target_conf_status} (derived from {target_commitment_level}, timeout {timeout_secs}s)")
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout_secs:
            try:
                # <<< FIX: Pass CommitmentLevel to get_signature_statuses >>>
                # Note: get_signature_statuses expects CommitmentConfig, so wrap the level
                status_resp = await self._async_client.get_signature_statuses(
                    [signature],
                    commitment=CommitmentConfig(target_commitment_level) # Wrap level here
                )
                if status_resp and status_resp.value and status_resp.value[0] is not None:
                    tx_status: TransactionStatus = status_resp.value[0]
                    current_conf_status = getattr(tx_status, 'confirmation_status', None)
                    tx_err = getattr(tx_status, 'err', None)
                    if tx_err: logger.warning(f"Transaction {signature} confirmed but FAILED: {tx_err}"); return None
                    # Compare using TransactionConfirmationStatus enums
                    if current_conf_status and current_conf_status >= target_conf_status:
                        logger.info(f"Transaction {signature} reached commitment {current_conf_status} (>= target {target_conf_status}).")
                        return current_conf_status
                # else: logger.debug(f"Polling {signature}: Status not yet available or None.")
            except SolanaRpcException as e_rpc: logger.error(f"RPC Exception polling status {signature}: {e_rpc}")
            except Exception as e_stat: logger.error(f"Unexpected Error fetching status {signature}: {e_stat}", exc_info=False)
            await asyncio.sleep(sleep_secs)
        logger.warning(f"Confirm timeout for {signature} after {timeout_secs}s.")
        return None

    def get_default_send_options(self, skip_preflight: bool = False) -> TxOpts:
        # This already uses CommitmentLevel correctly
        client_commitment_config = self._async_client.commitment
        default_level = client_commitment_config.commitment if client_commitment_config else self.default_commitment_level
        logger.debug(f"Using preflight commitment: {default_level}")
        return TxOpts(
            skip_preflight=skip_preflight,
            preflight_commitment=default_level # Pass the CommitmentLevel enum
        )

    # Dependent methods - should now work if the underlying get_ methods are fixed
    async def get_token_balance_lamports(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[int]:
        balance_value = await self.get_token_account_balance(pubkey, commitment) # Relies on fixed get_token_account_balance
        if balance_value and hasattr(balance_value, 'amount'):
            try: return int(balance_value.amount)
            except (ValueError, TypeError) as e: logger.error(f"Could not parse token balance amount '{balance_value.amount}' for {pubkey}: {e}"); return None
        elif balance_value is None: logger.debug(f"get_token_balance_lamports: Account {pubkey} not found or error fetching.")
        else: logger.warning(f"get_token_balance_lamports: Token balance response value for {pubkey} missing 'amount': {balance_value}")
        return None

    async def get_token_supply_lamports(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> Optional[int]:
        supply_value = await self.get_token_supply(pubkey, commitment) # Relies on fixed get_token_supply
        if supply_value and hasattr(supply_value, 'amount'):
            try: return int(supply_value.amount)
            except (ValueError, TypeError) as e: logger.error(f"Could not parse token supply amount '{supply_value.amount}' for {pubkey}: {e}"); return None
        elif supply_value is None: logger.debug(f"get_token_supply_lamports: Error fetching supply for {pubkey}.")
        else: logger.warning(f"get_token_supply_lamports: Token supply response value for {pubkey} missing 'amount': {supply_value}")
        return None

    async def does_token_account_exist(self, pubkey: Pubkey, commitment: Optional[Any] = None) -> bool:
        acc_info_value = await self.get_account_info(pubkey, commitment=commitment, encoding="base64") # Relies on fixed get_account_info
        exists = acc_info_value is not None
        logger.debug(f"Token account {pubkey} exists check: {exists}")
        return exists