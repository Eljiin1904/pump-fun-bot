# src/core/client.py (Corrected - Unused Imports Removed)

from solana.rpc.api import Client as SolanaSyncClient
from solana.rpc.async_api import AsyncClient as SolanaAsyncClient
from solana.rpc.commitment import Commitment, Confirmed
from solders.pubkey import Pubkey
from solders.signature import Signature
# TransactionStatus is only used for type hint in confirm, can be removed if not strictly needed
from solders.transaction_status import TransactionConfirmationStatus # , TransactionStatus # <-- REMOVED TransactionStatus
# List and Dict are not used in the latest version of this file's code
from typing import Optional, Any #, List, Dict # <-- REMOVED List, Dict
import asyncio

from ..utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 30

class SolanaClient:
    """ Wraps sync and async Solana clients. """

    def __init__(self, rpc_endpoint: str, wss_endpoint: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        """ Initializes the Solana client wrapper. """
        logger.info(f"Initializing SolanaClient with RPC: {rpc_endpoint}")
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        self._timeout = timeout
        self._sync_client = SolanaSyncClient(rpc_endpoint, commitment=Confirmed, timeout=self._timeout)
        self._async_client = SolanaAsyncClient(rpc_endpoint, commitment=Confirmed)
        self._is_connected = False # For async context manager

    def get_async_client(self) -> SolanaAsyncClient:
        """Returns the initialized async client instance."""
        return self._async_client

    def get_sync_client(self) -> SolanaSyncClient:
         """Returns the initialized sync client instance."""
         return self._sync_client

    async def __aenter__(self):
        """Enter asynchronous context."""
        self._is_connected = True
        logger.info(f"Async Solana client context entered for {self.rpc_endpoint}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit asynchronous context and close connections."""
        logger.info("Closing async Solana client connection...")
        # Ensure close method exists and check if connected if available
        if hasattr(self._async_client, 'close') and hasattr(self._async_client,'is_closed') and not self._async_client.is_closed():
             await self._async_client.close()
        elif hasattr(self._async_client, 'close'): # Fallback if is_closed doesn't exist
             await self._async_client.close()
        self._is_connected = False
        logger.info("Async Solana client connection closed.")

    # --- Optional Wrapper Methods ---
    # Keep these wrappers as provided previously, they use Optional and Any
    async def get_account_info(self, pubkey: Pubkey, commitment: Optional[Commitment] = None, encoding: str = "base64") -> Optional[Any]: # ... (implementation) ...
        try: resp = await self._async_client.get_account_info(pubkey, commitment=commitment, encoding=encoding); return resp.value;
        except Exception as e: logger.error(f"Error get_account_info {pubkey}: {e}"); return None

    async def get_token_account_balance(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[Any]: # ... (implementation) ...
        try: resp = await self._async_client.get_token_account_balance(pubkey, commitment=commitment); return resp.value;
        except Exception as e: logger.error(f"Error get_token_balance {pubkey}: {e}"); return None

    async def get_token_supply(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[Any]: # ... (implementation) ...
         try: resp = await self._async_client.get_token_supply(pubkey, commitment=commitment); return resp.value;
         except Exception as e: logger.error(f"Error get_token_supply {pubkey}: {e}"); return None

    async def get_latest_blockhash(self, commitment: Optional[Commitment] = None) -> Optional[Any]: # ... (implementation) ...
        try: used_commitment = commitment or self._async_client.commitment or Confirmed; resp = await self._async_client.get_latest_blockhash(commitment=used_commitment); return resp;
        except Exception as e: logger.error(f"Error get_latest_blockhash: {e}", exc_info=True); return None

    async def send_transaction(self, tx: Any, opts: Any) -> Optional[Any]: # ... (implementation) ...
        try: resp = await self._async_client.send_transaction(tx, opts=opts); return resp;
        except Exception as e: logger.error(f"Error send_transaction: {e}", exc_info=True); raise

    async def confirm_transaction(self, signature: Signature, commitment: Optional[Commitment] = None, timeout_secs: int = 60, last_valid_block_height: Optional[int] = None, sleep_secs: float = 0.5) -> TransactionConfirmationStatus: # ... (implementation) ...
         if commitment is None: commitment = self._async_client.commitment or Confirmed
         logger.debug(f"Confirming {signature} with {commitment} (timeout {timeout_secs}s)")
         elapsed_time = 0
         while elapsed_time < timeout_secs:
              try: status_resp = await self._async_client.get_signature_statuses([signature]);
              except Exception as e_stat: logger.error(f"Error fetching status {signature}: {e_stat}"); await asyncio.sleep(sleep_secs); elapsed_time += sleep_secs; continue; # Retry status fetch
              if status_resp.value and status_resp.value[0]:
                  current_status = status_resp.value[0].confirmation_status; logger.debug(f"Sig {signature} status: {current_status}")
                  if current_status == TransactionConfirmationStatus.Confirmed or current_status == TransactionConfirmationStatus.Finalized: return current_status;
              elif status_resp.value and status_resp.value[0] is None: logger.debug(f"Sig {signature} not found yet...")
              else: logger.error(f"Error in status response {signature}: {status_resp.error}")
              await asyncio.sleep(sleep_secs); elapsed_time += sleep_secs;
         logger.warning(f"Confirm timeout {signature} after {timeout_secs}s."); return TransactionConfirmationStatus.Processed # Return Processed on timeout

    def get_default_send_options(self, skip_preflight: bool = False) -> Any: # ... (implementation) ...
         from solana.rpc.types import TxOpts; return TxOpts(skip_preflight=skip_preflight, preflight_commitment=self._async_client.commitment or Confirmed)