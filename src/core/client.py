# src/core/client.py (Corrected Syntax)

import asyncio
from typing import Optional, Any, List # Added List

from solana.keypair import Keypair
from solders.pubkey import Pubkey
# Correct type imports if specific response objects are needed elsewhere,
# otherwise rely on structural typing/Any for now in wrapper methods.
from solders.rpc.responses import GetAccountInfoResp, GetBalanceResp, GetTokenAccountBalanceResp, GetLatestBlockhashResp # Example imports
from solders.transaction import Transaction # Needed for send_transaction type hint
from solders.signature import Signature # Needed for confirm_transaction type hint
from solana.rpc.commitment import Commitment, Confirmed, Processed # Use solders commitment
from solana.rpc.api import Client as SolanaSyncClient
from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
from solana.rpc.types import TxOpts # Import TxOpts
from solana.exceptions import SolanaRpcException
# Adjust relative path if utils is not directly under src
from ..utils.logger import get_logger

logger = get_logger(__name__)

# --- Ensure NO Devnet Default Here ---
DEFAULT_RPC_ENDPOINT: Optional[str] = None
DEFAULT_WSS_ENDPOINT: Optional[str] = None

class SolanaClient:
    """ Async wrapper for Solana client interactions. """

    def __init__(self, rpc_endpoint: Optional[str] = None, wss_endpoint: Optional[str] = None, commitment: Commitment = Confirmed):
        self.rpc_endpoint = rpc_endpoint or DEFAULT_RPC_ENDPOINT
        self.wss_endpoint = wss_endpoint or DEFAULT_WSS_ENDPOINT

        if not self.rpc_endpoint:
            logger.critical("FATAL: No RPC endpoint provided or defaulted in SolanaClient.")
            raise ValueError("RPC endpoint is required for SolanaClient.")

        logger.info(f"Initializing SolanaClient with RPC: {self.rpc_endpoint}")
        self._commitment = commitment
        self._async_client: Optional[SolanaPyAsyncClient] = None
        # self._sync_client: Optional[SolanaSyncClient] = None # Keep if needed
        self._closed = False
        self._initialize_clients() # Call initialization method

    def _initialize_clients(self):
        """Initializes the underlying Solana clients."""
        # Ensure correct indentation
        try:
            logger.info(f"Initializing underlying clients with default commitment: '{self._commitment}'")
            self._async_client = SolanaPyAsyncClient(self.rpc_endpoint, commitment=self._commitment)
            # self._sync_client = SolanaSyncClient(self.rpc_endpoint, commitment=self._commitment) # If sync needed
        except Exception as e:
            logger.error(f"Failed to initialize Solana clients: {e}", exc_info=True)
            raise # Re-raise after logging

    @property
    def async_client(self) -> SolanaPyAsyncClient:
        """Provides access to the underlying raw AsyncClient."""
        if self._closed:
             raise RuntimeError("Cannot access async_client after SolanaClient is closed.")
        if not self._async_client:
            logger.warning("AsyncClient accessed before proper initialization or after error. Re-initializing.")
            self._initialize_clients() # Attempt re-initialization
            if not self._async_client: # Still failed
                 raise RuntimeError("Failed to get underlying AsyncClient.")
        return self._async_client # Return the instance

    async def connect(self):
        """Explicitly connect the async client (checks health)."""
        # Ensure correct indentation
        if not self._async_client:
             self._initialize_clients()
        # Add defensive check again after initialization attempt
        if not self._async_client:
             raise ConnectionError("Failed to initialize async client for connection.")
        try:
            health = await self._async_client.get_health()
            logger.info(f"AsyncClient connected successfully to {self.rpc_endpoint}. Health: {health}")
        except Exception as e:
            logger.error(f"Failed to connect/check health for Solana client: {e}", exc_info=True)
            await self.close()
            raise ConnectionError(f"Failed to connect to RPC endpoint {self.rpc_endpoint}") from e

    async def close(self):
        """Closes the underlying Solana clients."""
        # Ensure correct indentation
        if self._closed:
             logger.debug("SolanaClient close requested, but already marked closed.")
             return

        logger.info("Closing SolanaClient connections...")
        closed_something = False
        if self._async_client and hasattr(self._async_client, 'close') and not getattr(self._async_client, '_closed', True):
            try:
                await self._async_client.close()
                logger.debug("AsyncClient closed.")
                closed_something = True
            except Exception as e:
                 logger.error(f"Error closing AsyncClient: {e}")
        # Close sync client if used
        # if self._sync_client and hasattr(self._sync_client, 'close')...

        self._closed = True # Mark as closed
        if closed_something:
            logger.info("SolanaClient connections closed.")
        else:
            logger.info("SolanaClient connections were likely already closed or not initialized.")


    # --- Wrapper methods (Ensure proper indentation and syntax) ---
    async def get_account_info(self, pubkey: Pubkey, commitment: Optional[Commitment] = None, encoding: str = 'base64') -> Optional[GetAccountInfoResp]:
        """ Wraps async_client.get_account_info """
        if self._closed or not self._async_client: raise RuntimeError("Client closed or not initialized.")
        try:
            commit = commitment or self._commitment
            # Ensure method call syntax is correct
            return await self.async_client.get_account_info(pubkey, commitment=commit, encoding=encoding)
        except SolanaRpcException as e: logger.error(f"RPC Exception getting account info for {pubkey}: {e}"); return None
        except Exception as e: logger.error(f"Unexpected error getting account info for {pubkey}: {e}", exc_info=True); return None

    async def get_balance(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[GetBalanceResp]:
        """ Wraps async_client.get_balance """
        if self._closed or not self._async_client: raise RuntimeError("Client closed or not initialized.")
        try:
             commit = commitment or self._commitment
             return await self.async_client.get_balance(pubkey, commitment=commit)
        except SolanaRpcException as e: logger.error(f"RPC Exception getting balance for {pubkey}: {e}"); return None
        except Exception as e: logger.error(f"Unexpected error getting balance for {pubkey}: {e}", exc_info=True); return None

    async def get_token_account_balance(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[GetTokenAccountBalanceResp]:
        """ Wraps async_client.get_token_account_balance """
        if self._closed or not self._async_client: raise RuntimeError("Client closed or not initialized.")
        try:
            commit = commitment or self._commitment
            return await self.async_client.get_token_account_balance(pubkey, commitment=commit)
        except SolanaRpcException as e: logger.error(f"RPC Exception getting token balance for {pubkey}: {e}"); return None
        except Exception as e: logger.error(f"Unexpected error getting token balance for {pubkey}: {e}", exc_info=True); return None

    # Ensure correct arguments and unpacking for send/confirm
    async def send_transaction(self, txn: Transaction, signers: List[Keypair], opts: Optional[TxOpts] = None):
         if self._closed or not self._async_client: raise RuntimeError("Client closed or not initialized.")
         # Pass signers as separate arguments using *
         return await self.async_client.send_transaction(txn, *signers, opts=opts)

    async def confirm_transaction(self, tx_sig: Signature, commitment: Optional[Commitment]=None, sleep_seconds: float=0.5, last_valid_block_height: Optional[int]=None):
        if self._closed or not self._async_client: raise RuntimeError("Client closed or not initialized.")
        commit = commitment or self._commitment
        return await self.async_client.confirm_transaction(
            signature=tx_sig, # Use named argument for clarity
            commitment=commit,
            sleep_seconds=sleep_seconds,
            last_valid_block_height=last_valid_block_height
            )

    async def get_latest_blockhash(self, commitment: Optional[Commitment] = None) -> Optional[str]: # Return type adjustment
         if self._closed or not self._async_client: raise RuntimeError("Client closed or not initialized.")
         try:
             commit = commitment or self._commitment
             resp: GetLatestBlockhashResp = await self.async_client.get_latest_blockhash(commitment=commit)
             # Check if resp and resp.value and resp.value.blockhash exist
             if resp and hasattr(resp, 'value') and resp.value and hasattr(resp.value, 'blockhash'):
                  return str(resp.value.blockhash) # Return as string
             else:
                  logger.error(f"Unexpected response structure for get_latest_blockhash: {resp}")
                  return None
         except Exception as e:
              logger.error(f"Error getting latest blockhash: {e}", exc_info=True)
              return None


# --- END OF FILE ---