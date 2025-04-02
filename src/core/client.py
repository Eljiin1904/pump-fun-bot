"""
Solana client abstraction for blockchain operations.
"""

import asyncio
import json
from typing import Any, Optional

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Commitment
from solana.rpc.types import TxOpts
# V V V FIX: Import Account for type hinting V V V
from solders.account import Account
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
# V V V FIX: Import Signature (from previous fix) V V V
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from ..utils.logger import get_logger

logger = get_logger(__name__)


class SolanaClient:
    """
    Abstraction for Solana RPC client operations.
    (Docstring unchanged)
    """

    def __init__(self, rpc_endpoint: str):
        """Initialize Solana client with RPC endpoint. (Docstring unchanged)"""
        self.rpc_endpoint = rpc_endpoint
        self._client: Optional[AsyncClient] = None

    async def __aenter__(self):
        """Enter the async context manager, initializing the client."""
        await self.get_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the async context manager, closing the client connection."""
        await self.close()

    async def get_client(self) -> AsyncClient:
        """Get or create the AsyncClient instance. (Docstring unchanged)"""
        if self._client is None:
            try:
                self._client = AsyncClient(self.rpc_endpoint)
                await self._client.is_connected()
                logger.info(f"Solana client connected to {self.rpc_endpoint}")
            except Exception as e:
                logger.error(f"Failed to initialize or connect Solana client at {self.rpc_endpoint}: {e}", exc_info=True)
                self._client = None
                raise RuntimeError(f"Failed to initialize Solana AsyncClient: {e}")

        if self._client is None:
            raise RuntimeError("Solana AsyncClient is not initialized.")
        return self._client

    async def close(self):
        """Close the client connection if open. (Implementation unchanged)"""
        if self._client:
            try:
                await self._client.close()
                logger.info("Solana client connection closed.")
            except Exception as e:
                logger.error(f"Error closing Solana client: {e}", exc_info=True)
            finally:
                self._client = None

    # V V V FIX: Updated return type hint to Optional[Account] V V V
    async def get_account_info(self, pubkey: Pubkey, commitment: Optional[Commitment] = None) -> Optional[Account]:
        """Get account info from the blockchain.

        Args:
            pubkey: Public key of the account.
            commitment: Optional commitment level.

        Returns:
            A solders.account.Account object or None if not found or error occurs.
        """
        try:
            client = await self.get_client()
            # The response.value is the Account object itself
            response = await client.get_account_info(pubkey, commitment=commitment)
            if response.value:
                # Return the Account object directly
                return response.value
            else:
                logger.debug(f"Account info not found for {pubkey} with commitment {commitment}")
                return None
        except Exception as e:
            logger.error(f"Error getting account info for {pubkey}: {e}", exc_info=True)
            return None  # Return None on errors

    async def get_token_account_balance(self, token_account: Pubkey, commitment: Optional[Commitment] = None) -> Optional[int]:
        """Get token balance for an account. (Implementation unchanged)"""
        try:
            client = await self.get_client()
            response = await client.get_token_account_balance(token_account, commitment=commitment)
            if response.value:
                return int(response.value.amount)
            else:
                logger.debug(f"Token account {token_account} not found or has no balance info.")
                return None
        except Exception as e:
            logger.error(f"Error getting token balance for {token_account}: {e}", exc_info=True)
            return None

    async def get_latest_blockhash(self, commitment: Optional[Commitment] = Confirmed) -> Hash:
        """Get the latest blockhash. (Implementation unchanged)"""
        try:
            client = await self.get_client()
            response = await client.get_latest_blockhash(commitment=commitment)
            if not response or not hasattr(response, 'value') or not response.value or not hasattr(response.value, 'blockhash') or not response.value.blockhash:
                raise ValueError("Received invalid or empty response when fetching blockhash")
            return response.value.blockhash
        except Exception as e:
            logger.error(f"Failed to get latest blockhash (commitment: {commitment}): {e}", exc_info=True)
            raise

    async def get_token_supply(self, mint_pk: Pubkey, commitment: Optional[Commitment] = None) -> Optional[Any]:
        """Get token supply for the given mint.

        Args:
            mint_pk: Public key of the token mint.
            commitment: Optional commitment level.

        Returns:
            The token supply response object if available, otherwise None.
        """
        try:
            client = await self.get_client()
            response = await client.get_token_supply(mint_pk, commitment=commitment)
            if response.value:
                return response.value
            else:
                logger.debug(f"Token supply not found for mint {mint_pk}")
                return None
        except Exception as e:
            logger.error(f"Error getting token supply for {mint_pk}: {e}", exc_info=True)
            return None

    async def build_and_send_transaction(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        skip_preflight: bool = True,
        max_retries: int = 10,
        priority_fee: Optional[int] = None,
        compute_unit_limit: int = 400_000
    ) -> str:
        """Build and send a Versioned Transaction... (Implementation unchanged from last version)"""
        if not instructions:
            raise ValueError("Instructions list cannot be empty for build_and_send_transaction.")

        client = await self.get_client()
        payer_pubkey = signer_keypair.pubkey()

        logger.info(
            f"Preparing transaction with priority_fee={priority_fee}, cu_limit={compute_unit_limit}, skip_preflight={skip_preflight}"
        )

        tx_instructions = []
        if priority_fee is not None and priority_fee > 0:
            logger.debug(f"Adding compute budget instructions: limit={compute_unit_limit}, price={priority_fee}")
            tx_instructions.append(set_compute_unit_limit(compute_unit_limit))
            tx_instructions.append(set_compute_unit_price(priority_fee))
        tx_instructions.extend(instructions)

        last_exception: Optional[Exception] = None

        for attempt in range(max_retries):
            try:
                recent_blockhash = await self.get_latest_blockhash()
                logger.debug(f"Attempt {attempt + 1}: Using blockhash {recent_blockhash}")

                message = MessageV0.try_compile(
                    payer=payer_pubkey,
                    instructions=tx_instructions,
                    address_lookup_table_accounts=[],
                    recent_blockhash=recent_blockhash,
                )

                transaction = VersionedTransaction(message, [signer_keypair])

                tx_opts = TxOpts(
                    skip_preflight=skip_preflight,
                    preflight_commitment=Confirmed
                )

                response = await client.send_transaction(transaction, opts=tx_opts)  # Uses opts=

                signature = str(response.value)
                logger.info(f"Transaction sent successfully (Attempt {attempt + 1}/{max_retries}). Signature: {signature}")
                return signature

            except Exception as e:
                last_exception = e
                logger.error(
                    f"Attempt {attempt + 1}/{max_retries} failed: {type(e).__name__}: {str(e)}",
                    exc_info=(attempt == max_retries - 1)
                )
                if attempt == max_retries - 1:
                    break

                wait_time = min(2 ** attempt, 60)
                logger.warning(f"Retrying transaction in {wait_time}s...")
                await asyncio.sleep(wait_time)

        logger.critical(f"Transaction failed permanently after {max_retries} attempts.")
        if last_exception:
            raise last_exception
        else:
            raise RuntimeError("Transaction failed after maximum retries without capturing a specific error.")

    async def confirm_transaction(
        self,
        signature: str,  # Accepts string
        commitment: Commitment = Confirmed,
        max_timeout_seconds: int = 60,
    ) -> bool:
        """Wait for transaction confirmation with timeout. (Includes Signature.from_string fix)"""
        client = await self.get_client()
        logger.info(f"Confirming transaction {signature} with commitment {commitment} (timeout={max_timeout_seconds}s)...")
        try:
            # Convert string to Signature object
            try:
                sig_obj = Signature.from_string(signature)
            except ValueError as e:
                logger.error(f"Invalid signature format provided for confirmation: {signature} - {e}")
                return False

            # Pass Signature object, no sleep_secs
            await asyncio.wait_for(
                client.confirm_transaction(
                    sig_obj,  # Pass Signature object
                    commitment=commitment
                ),
                timeout=max_timeout_seconds
            )
            logger.info(f"Transaction {signature} confirmed.")
            return True
        except asyncio.TimeoutError:
            logger.error(f"Confirmation timed out after {max_timeout_seconds}s for transaction {signature} (Commitment: {commitment}).")
            return False
        except Exception as e:
            logger.error(f"Error during confirmation check for transaction {signature}: {str(e)}", exc_info=True)
            return False

    async def post_rpc(self, body: dict[str, Any], timeout_seconds: int = 10) -> Optional[dict[str, Any]]:
        """Send a raw RPC request... (Implementation unchanged)"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.rpc_endpoint,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
                    if 'error' in result:
                        logger.error(f"RPC returned an error: {result['error']}")
                        return None
                    return result
        except aiohttp.ClientResponseError as e:
            logger.error(f"HTTP error during RPC request: {e.status} {e.message}", exc_info=True)
            return None
        except aiohttp.ClientConnectionError as e:
            logger.error(f"Connection error during RPC request: {e}", exc_info=True)
            return None
        except asyncio.TimeoutError:
            logger.error(f"RPC request timed out after {timeout_seconds} seconds.")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON RPC response: {str(e)}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error during raw RPC POST request: {e}", exc_info=True)
            return None
