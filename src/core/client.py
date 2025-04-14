# src/core/client.py

import asyncio
from typing import Optional, Any, List

from solana.rpc.api import Client as SolanaSyncClient
from solana.rpc.async_api import AsyncClient as SolanaAsyncClient
from solana.rpc.commitment import Commitment, Confirmed
from solana.rpc.types import TxOpts
# Correct import for response type classes used by the underlying client
from solders.rpc.responses import GetTokenAccountBalanceResp, GetTokenSupplyResp, GetAccountInfoResp, GetLatestBlockhashResp
# Import the main RPC exception type
from solana.exceptions import SolanaRpcException
# InvalidParamsMessage cannot be caught directly as it doesn't inherit BaseException
# from solders.rpc.errors import InvalidParamsMessage # REMOVED
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction_status import TransactionConfirmationStatus, TransactionStatus

# Assuming logger is correctly set up in this path
from ..utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 30

class SolanaClient:
    """ Wraps sync and async Solana clients with improved error handling. """

    def __init__(self, rpc_endpoint: str, wss_endpoint: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        """ Initializes the Solana client wrapper. """
        logger.info(f"Initializing SolanaClient with RPC: {rpc_endpoint}")
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        self._timeout = timeout
        # Pass commitment and timeout if supported by your SyncClient version
        self._sync_client = SolanaSyncClient(rpc_endpoint, commitment=Confirmed)
        self._async_client = SolanaAsyncClient(rpc_endpoint, commitment=Confirmed)
        self._is_connected = False # For async context manager tracking

    def get_async_client(self) -> SolanaAsyncClient:
        """Returns the initialized async client instance."""
        return self._async_client

    def get_sync_client(self) -> SolanaSyncClient:
         """Returns the initialized sync client instance."""
         return self._sync_client

    async def __aenter__(self):
        """Enter asynchronous context."""
        self._is_connected = True
        logger.debug(f"Async Solana client context potentially managed for {self.rpc_endpoint}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit asynchronous context and close connections."""
        logger.debug("Closing async Solana client connection (if applicable)...")
        # Use robust closing logic
        close_method = getattr(self._async_client, 'aclose', getattr(self._async_client, 'close', None))
        if close_method and asyncio.iscoroutinefunction(close_method):
            try: await close_method()
            except Exception as e: logger.error(f"Error closing async client: {e}")
        elif close_method:
             try: close_method()
             except Exception as e: logger.error(f"Error closing async client (sync close): {e}")
        self._is_connected = False
        logger.debug("Async Solana client connection closed.")

    async def get_account_info( self, pubkey: Pubkey, commitment: Optional[Commitment] = None, encoding: str = "base64" ) -> Optional[Any]:
        """ Fetches account info, returning the 'value' part or None on error. """
        method_name = "get_account_info" # For logging
        try:
            used_commitment = commitment or self._async_client.commitment
            resp: GetAccountInfoResp = await self._async_client.get_account_info( pubkey, commitment=used_commitment, encoding=encoding )
            # Check hasattr before returning value
            if hasattr(resp, 'value'):
                return resp.value
            else:
                logger.error(f"{method_name} response for {pubkey} lacks 'value' attribute. Response: {resp}")
                return None
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
            if isinstance(e_rpc.__cause__, Exception): logger.error(f"  Underlying cause: {type(e_rpc.__cause__).__name__}: {e_rpc.__cause__}")
            return None
        except Exception as e:
            logger.error(f"Unexpected Error {method_name} {pubkey}: Type={type(e)}, Error={e}", exc_info=True)
            return None

    async def get_token_account_balance( self, pubkey: Pubkey, commitment: Optional[Commitment] = None ) -> Optional[Any]:
        """
        Fetches token balance response. Returns the 'value' part of the response
        (which contains amount, decimals, uiAmountString) or None on error.
        """
        method_name = "get_token_account_balance"
        try:
            used_commitment = commitment or self._async_client.commitment
            resp: GetTokenAccountBalanceResp = await self._async_client.get_token_account_balance( pubkey, commitment=used_commitment )
            # Check hasattr before returning value
            if hasattr(resp, 'value'):
                return resp.value
            else:
                logger.error(f"{method_name} response for {pubkey} lacks 'value' attribute. Response: {resp}")
                return None
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
            if isinstance(e_rpc.__cause__, Exception): logger.error(f"  Underlying cause: {type(e_rpc.__cause__).__name__}: {e_rpc.__cause__}")
            return None
        except Exception as e:
            logger.error(f"Unexpected Error {method_name} {pubkey}: Type={type(e)}, Error={e}", exc_info=True)
            return None

    async def get_token_supply( self, pubkey: Pubkey, commitment: Optional[Commitment] = None ) -> Optional[Any]:
        """
        Fetches token supply response. Returns the 'value' part of the response
        (which contains amount, decimals, uiAmountString) or None on error.
        """
        method_name = "get_token_supply"
        try:
            used_commitment = commitment or self._async_client.commitment
            resp: GetTokenSupplyResp = await self._async_client.get_token_supply( pubkey, commitment=used_commitment )
            # Check hasattr before returning value
            if hasattr(resp, 'value'):
                return resp.value
            else:
                logger.error(f"{method_name} response for {pubkey} lacks 'value' attribute. Response: {resp}")
                return None
        except SolanaRpcException as e_rpc:
            logger.error(f"RPC Exception {method_name} {pubkey}: {e_rpc}")
            if isinstance(e_rpc.__cause__, Exception): logger.error(f"  Underlying cause: {type(e_rpc.__cause__).__name__}: {e_rpc.__cause__}")
            return None
        except Exception as e:
            logger.error(f"Unexpected Error {method_name} {pubkey}: Type={type(e)}, Error={e}", exc_info=True)
            return None

    async def get_latest_blockhash( self, commitment: Optional[Commitment] = None ) -> Optional[GetLatestBlockhashResp]:
        """ Fetches latest blockhash response, returning response object or None on error. """
        method_name = "get_latest_blockhash"
        try:
            used_commitment = commitment or self._async_client.commitment or Confirmed
            resp: GetLatestBlockhashResp = await self._async_client.get_latest_blockhash(commitment=used_commitment)
            # Return the whole response object if value exists
            if hasattr(resp, 'value'):
                 return resp
            else:
                 logger.error(f"{method_name} response lacks 'value' attribute. Response: {resp}")
                 return None
        except SolanaRpcException as e_rpc:
             logger.error(f"RPC Exception {method_name}: {e_rpc}")
             if isinstance(e_rpc.__cause__, Exception): logger.error(f"  Underlying cause: {type(e_rpc.__cause__).__name__}: {e_rpc.__cause__}")
             return None
        except Exception as e:
            logger.error(f"Unexpected Error {method_name}: Type={type(e)}, Error={e}", exc_info=True)
            return None

    async def send_transaction( self, tx: Any, opts: TxOpts ) -> Optional[Signature]:
        """ Sends a transaction, returning the signature or raises on error. """
        method_name = "send_transaction"
        try:
            signature: Signature = await self._async_client.send_transaction(tx, opts=opts)
            return signature
        except SolanaRpcException as e_rpc:
             logger.error(f"RPC Exception {method_name}: {e_rpc}", exc_info=True)
             raise # Re-raise send errors
        except Exception as e:
            logger.error(f"Unexpected Error {method_name}: Type={type(e)}, Error={e}", exc_info=True)
            raise

    async def confirm_transaction( self, signature: Signature, commitment: Optional[Commitment] = None, timeout_secs: int = 60, sleep_secs: float = 1.0 ) -> Optional[TransactionConfirmationStatus]:
        """ Polls for transaction confirmation status using get_signature_statuses. """
        used_commitment = commitment or self._async_client.commitment or Confirmed; logger.debug(f"Confirming {signature} with {used_commitment} (timeout {timeout_secs}s)"); start_time = asyncio.get_event_loop().time();
        while (asyncio.get_event_loop().time() - start_time) < timeout_secs:
            try:
                status_resp: List[Optional[TransactionStatus]] = await self._async_client.get_signature_statuses([signature])
                if status_resp and status_resp[0] is not None:
                    tx_status: TransactionStatus = status_resp[0]; current_conf_status = getattr(tx_status, 'confirmation_status', None); tx_err = getattr(tx_status, 'err', None); logger.debug(f"Sig {signature} status: {current_conf_status}, Err: {tx_err}");
                    if tx_err: logger.warning(f"Transaction {signature} failed confirmation with error: {tx_err}"); return None
                    if current_conf_status and current_conf_status >= used_commitment: logger.info(f"Transaction {signature} reached commitment {used_commitment}."); return current_conf_status
                elif status_resp and status_resp[0] is None: logger.debug(f"Signature {signature} not found yet. Polling...")
                else: logger.error(f"Unexpected status response structure for {signature}: {status_resp}")
            except SolanaRpcException as e_rpc: logger.error(f"RPC Exception polling status {signature}: {e_rpc}") # Catch RPC errors during polling
            except Exception as e_stat: logger.error(f"Unexpected Error fetching status {signature}: Type={type(e_stat)}, Error={e_stat}", exc_info=False)
            await asyncio.sleep(sleep_secs);
        logger.warning(f"Confirm timeout for {signature} after {timeout_secs}s."); return None

    def get_default_send_options(self, skip_preflight: bool = False) -> TxOpts:
         """ Returns default TxOpts for sending transactions. """
         return TxOpts(skip_preflight=skip_preflight, preflight_commitment=self._async_client.commitment or Confirmed)