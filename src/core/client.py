import asyncio
from typing import Optional, Any

# Solana client imports
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient as SolanaAsyncClient
from solana.rpc.commitment import Commitment, Confirmed, Processed  # for type hints or usage
from solana.rpc.core import RPCException as SolanaRpcException  # adjust if needed
from solana.rpc.types import TxOpts

# (If TransactionConfirmationStatus was imported before, it’s no longer used for commitments)
# from solders.transaction_status import TransactionConfirmationStatus

from ..utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_COMMITMENT = "confirmed"  # Use string-based commitment level


class SolanaClient:
    """Wraps sync and async Solana clients with improved error handling."""

    def __init__(self, rpc_endpoint: str, wss_endpoint: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        """Initializes the Solana client wrapper."""
        logger.info(f"Initializing SolanaClient with RPC: {rpc_endpoint}")
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        self._timeout = timeout
        # Initialize underlying clients with string commitment for compatibility
        logger.info(f"Initializing underlying clients with default commitment: '{DEFAULT_COMMITMENT}'")
        self._sync_client = None  # If a synchronous client is used elsewhere, instantiate similarly
        self._async_client = SolanaAsyncClient(rpc_endpoint, timeout=timeout, commitment=DEFAULT_COMMITMENT)

    def get_async_client(self) -> SolanaAsyncClient:
        """Returns the initialized async client instance."""
        return self._async_client

    def get_sync_client(self):
        """Returns the initialized sync client instance (if used)."""
        return self._sync_client

    async def __aenter__(self):
        """Enter asynchronous context."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context and close connections."""
        await self.close()

    async def close(self):
        """Closes the underlying async client connection."""
        logger.debug("Closing async Solana client connection...")
        close_method = getattr(self._async_client, 'aclose', getattr(self._async_client, 'close', None))
        if close_method:
            try:
                if asyncio.iscoroutinefunction(close_method):
                    await close_method()
                else:
                    close_method()
            except Exception as e:
                logger.error(f"Error closing async client: {e}")
        logger.debug("Async Solana client connection closed.")

    def _normalize_commitment(self, commitment: Optional[Any]) -> Optional[str]:
        """Convert commitment input to a lowercase string ('processed', 'confirmed', etc.), or None."""
        if commitment is None:
            return None
        if isinstance(commitment, str):
            return commitment.lower()
        # If using solana.rpc.commitment.Confirmed/Processed, they might be of type Commitment or have .commitment
        if hasattr(commitment, 'commitment'):  # solana-py Commitment object
            # Commitment.commitment is already a str like 'confirmed' or 'processed'
            return str(commitment.commitment).lower()
        if hasattr(commitment, 'name'):
            # For enums like TransactionConfirmationStatus or solders CommitmentLevel
            return str(commitment.name).lower()
        # Fallback: use str()
        return str(commitment).lower()

    async def get_account_info(self, pubkey: Pubkey, commitment: Optional[Commitment] = None,
                               encoding: str = "base64") -> Optional[Any]:
        """Fetches account info; returns the 'value' field or None on error."""
        method_name = "get_account_info"
        try:
            used_commitment = self._normalize_commitment(
                commitment or self._async_client.commitment or DEFAULT_COMMITMENT)
            resp = await self._async_client.get_account_info(pubkey, commitment=used_commitment, encoding=encoding)
            if hasattr(resp, 'value'):
                return resp.value
            # Handle cases where account is not found
            if resp and hasattr(resp, 'message') and "could not find account" in resp.message.lower():
                logger.debug(f"Account {pubkey} not found (get_account_info).")
                return None
            # Unexpected structure
            logger.error(f"{method_name} response for {pubkey} lacks 'value'. Response: {resp}")
            return None
        except SolanaRpcException as e_rpc:
            if "could not find account" in str(e_rpc).lower():
                logger.debug(f"Account {pubkey} not found (RPC exception).")
                return None
            logger.error(f"RPC Exception in {method_name} for {pubkey}: {e_rpc}")
            if hasattr(e_rpc, '__cause__') and e_rpc.__cause__:
                logger.error(f"  Underlying cause: {type(e_rpc.__cause__).__name__}: {e_rpc.__cause__}")
            return None
        except Exception as e:
            logger.error(f"Unexpected Error in {method_name}({pubkey}): {e}", exc_info=True)
            return None

    async def get_token_account_balance(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[Any]:
        """Fetches token account balance; returns the 'value' part or None if not found/error."""
        method_name = "get_token_account_balance"
        try:
            used_commitment = self._normalize_commitment(
                commitment or self._async_client.commitment or DEFAULT_COMMITMENT)
            resp = await self._async_client.get_token_account_balance(pubkey, commitment=used_commitment)
            if hasattr(resp, 'value'):
                # The value typically has 'amount', 'decimals', etc.
                if resp.value and hasattr(resp.value, 'amount'):
                    return resp.value  # Contains amount, decimals, uiAmount, etc.
                else:
                    # Account exists but value is missing expected fields (could be zero balance or not a token account)
                    logger.debug(f"Token account {pubkey} balance value missing 'amount' or is None: {resp.value}")
                    return resp.value
            # If response indicates account not found
            if resp and hasattr(resp, 'message') and "could not find account" in str(resp.message).lower():
                logger.debug(f"Token account {pubkey} not found (balance).")
                return None
            logger.error(f"{method_name} response for {pubkey} lacks 'value'. Response: {resp}")
            return None
        except SolanaRpcException as e_rpc:
            if "could not find account" in str(e_rpc).lower():
                logger.debug(f"Token account {pubkey} not found (RPC exception).")
                return None
            logger.error(f"RPC Exception in {method_name} for {pubkey}: {e_rpc}")
            if hasattr(e_rpc, '__cause__') and e_rpc.__cause__:
                logger.error(f"  Underlying cause: {type(e_rpc.__cause__).__name__}: {e_rpc.__cause__}")
            return None
        except Exception as e:
            logger.error(f"Unexpected Error in {method_name}({pubkey}): {e}", exc_info=True)
            return None

    async def get_token_balance_lamports(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[
        int]:
        """Fetches token account balance and returns the raw lamport amount (int) or None."""
        balance_value = await self.get_token_account_balance(pubkey, commitment)
        if balance_value and hasattr(balance_value, 'amount'):
            try:
                # amount is a string in the RPC response
                return int(balance_value.amount)
            except (ValueError, TypeError) as e:
                logger.error(
                    f"Could not parse token balance amount '{getattr(balance_value, 'amount', None)}' for {pubkey}: {e}")
                return None
        elif balance_value is None:
            # Already logged not found or error in get_token_account_balance
            logger.debug(f"Token account {pubkey} balance not found or error (lamports).")
            return None
        else:
            # balance_value exists but no 'amount' attribute (unexpected format)
            logger.error(f"Unexpected balance format for {pubkey}: {balance_value}")
            return None

    async def get_token_supply(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[Any]:
        """Fetches token supply; returns the 'value' field (contains amount, decimals, etc.) or None."""
        method_name = "get_token_supply"
        try:
            used_commitment = self._normalize_commitment(
                commitment or self._async_client.commitment or DEFAULT_COMMITMENT)
            resp = await self._async_client.get_token_supply(pubkey, commitment=used_commitment)
            if hasattr(resp, 'value'):
                if resp.value and hasattr(resp.value, 'amount'):
                    return resp.value  # Contains total supply info
                else:
                    logger.debug(f"Token supply for {pubkey} has no 'amount' field or is None: {resp.value}")
                    return resp.value
            # Handle not found (though get_token_supply usually returns value even if 0)
            if resp and hasattr(resp, 'message') and "could not find" in str(resp.message).lower():
                logger.debug(f"Token mint {pubkey} not found (supply).")
                return None
            logger.error(f"{method_name} response for {pubkey} lacks 'value'. Response: {resp}")
            return None
        except SolanaRpcException as e_rpc:
            if "could not find" in str(e_rpc).lower():
                logger.debug(f"Token mint {pubkey} not found (RPC exception).")
                return None
            logger.error(f"RPC Exception in {method_name} for {pubkey}: {e_rpc}")
            if hasattr(e_rpc, '__cause__') and e_rpc.__cause__:
                logger.error(f"  Underlying cause: {type(e_rpc.__cause__).__name__}: {e_rpc.__cause__}")
            return None
        except Exception as e:
            logger.error(f"Unexpected Error in {method_name}({pubkey}): {e}", exc_info=True)
            return None

    async def get_token_supply_lamports(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[int]:
        """Fetches total token supply and returns the raw supply amount (int lamports) or None."""
        supply_value = await self.get_token_supply(pubkey, commitment)
        if supply_value and hasattr(supply_value, 'amount'):
            try:
                return int(supply_value.amount)
            except (ValueError, TypeError) as e:
                logger.error(
                    f"Could not parse token supply amount '{getattr(supply_value, 'amount', None)}' for {pubkey}: {e}")
                return None
        elif supply_value is None:
            logger.debug(f"Token supply for {pubkey} not found or RPC error.")
            return None
        else:
            logger.error(f"Unexpected token supply format for {pubkey}: {supply_value}")
            return None

    async def get_latest_blockhash(self, commitment: Optional[Commitment] = None) -> Optional[Any]:
        """Fetches the latest blockhash (and last valid block height). Returns the whole response value or None."""
        method_name = "get_latest_blockhash"
        try:
            used_commitment = self._normalize_commitment(
                commitment or self._async_client.commitment or DEFAULT_COMMITMENT)
            resp = await self._async_client.get_latest_blockhash(commitment=used_commitment)
            if hasattr(resp, 'value'):
                return resp.value  # This includes 'blockhash' and 'lastValidBlockHeight'
            logger.error(f"{method_name} response lacks 'value'. Response: {resp}")
            return None
        except Exception as e:
            logger.error(f"Unexpected Error in {method_name}: {e}", exc_info=True)
            return None

    # ... (Other methods like sending transactions, confirming transactions, etc., if present) ...
    # Ensure any place commitment is used, _normalize_commitment is applied.
