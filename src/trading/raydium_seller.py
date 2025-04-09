# src/trading/raydium_seller.py
# Implements selling on DEX via Jupiter V6 API

import httpx
import base64
# import time # Unused
# import asyncio # Unused
# Remove List if Dict, Optional, Any are sufficient
from typing import Optional, Dict, Any # Removed List

from solders.pubkey import Pubkey
# from solders.keypair import Keypair # Unused type import
from solders.transaction import VersionedTransaction
from solders.signature import Signature
# --- Import Commitment types ---
from solders.commitment_config import CommitmentLevel # For type hint if needed by client.confirm
from solana.rpc.commitment import Confirmed # Import specific commitment level
# --- End Import ---
from solders.transaction_status import TransactionConfirmationStatus

# Define Constants Locally
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_TOKEN_DECIMALS = 6
SOL_MINT_ADDRESS = "So11111111111111111111111111111111111111112"

# Core/Helper Imports
from ..core.client import SolanaClient
from ..core.wallet import Wallet
# Remove build_and_send if using direct send exclusively here
# from ..core.transactions import build_and_send_transaction
from ..core.transactions import TransactionResult # Keep Result type
from ..trading.base import TokenInfo, TradeResult
from ..utils.logger import get_logger

logger = get_logger(__name__)

# Jupiter V6 API Endpoints
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

REQUEST_TIMEOUT = 30.0

class RaydiumSeller:
    """ Handles selling tokens on DEX via Jupiter aggregator. """

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        slippage_bps: int = 500
        ):
        self.client = client
        self.wallet = wallet
        self.slippage_bps = slippage_bps
        self.async_client = client.get_async_client()
        logger.info(f"RaydiumSeller initialized with Slippage: {self.slippage_bps} BPS")

    # _get_jupiter_quote method remains the same
    async def _get_jupiter_quote( self, input_mint: Pubkey, output_mint: Pubkey, amount_lamports: int) -> Optional[Dict[str, Any]]:
        # ... (implementation as before) ...
        logger.debug(f"Fetching Jupiter quote: {amount_lamports} lamports {input_mint} -> {output_mint}")
        params = { "inputMint": str(input_mint), "outputMint": str(output_mint), "amount": str(amount_lamports), "slippageBps": self.slippage_bps }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.get(JUPITER_QUOTE_API, params=params); response.raise_for_status(); quote_response = response.json();
                if not quote_response or not isinstance(quote_response, dict) or "outAmount" not in quote_response: logger.error(f"Invalid quote response: {quote_response}"); return None
                logger.info(f"Jupiter quote: Sell {amount_lamports / (10**DEFAULT_TOKEN_DECIMALS):.4f} -> Min {int(quote_response.get('otherAmountThreshold',0))/(10**DEFAULT_TOKEN_DECIMALS):.4f} SOL")
                return quote_response
        except httpx.HTTPStatusError as e: logger.error(f"HTTP error Jupiter quote: {e.response.status_code} - {e.response.text}"); return None
        except Exception as e: logger.error(f"Error Jupiter quote: {e}", exc_info=True); return None

    # _get_jupiter_swap_tx method remains the same
    async def _get_jupiter_swap_tx( self, quote_response: Dict[str, Any], user_pubkey: Pubkey) -> Optional[Dict[str, Any]]:
        # ... (implementation as before) ...
        logger.debug("Fetching Jupiter swap transaction...")
        payload = { "quoteResponse": quote_response, "userPublicKey": str(user_pubkey), "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"}
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(JUPITER_SWAP_API, json=payload); response.raise_for_status(); swap_response = response.json();
                if not swap_response or not isinstance(swap_response, dict) or "swapTransaction" not in swap_response: logger.error(f"Invalid swap response: {swap_response}"); return None
                logger.info("Jupiter swap transaction received."); return swap_response
        except httpx.HTTPStatusError as e: logger.error(f"HTTP error Jupiter swap tx: {e.response.status_code} - {e.response.text}"); return None
        except Exception as e: logger.error(f"Error Jupiter swap tx: {e}", exc_info=True); return None


    async def execute(self, token_info: TokenInfo, amount_to_sell_lamports: int) -> TradeResult:
        """ Executes the sell via Jupiter using direct send. """
        logger.info(f"Executing DEX sell {amount_to_sell_lamports} lamports {token_info.symbol} ({token_info.mint}) via Jupiter...")
        if amount_to_sell_lamports <= 0: return TradeResult(success=False, error_message="Amount <= 0")

        # 1. Get Quote
        quote_response = await self._get_jupiter_quote(token_info.mint, Pubkey.from_string(SOL_MINT_ADDRESS), amount_to_sell_lamports)
        if not quote_response: return TradeResult(success=False, error_message="Failed Jupiter quote")

        est_price: Optional[float] = None
        try: est_out_lamports = int(quote_response.get("outAmount", 0)); est_price = (est_out_lamports / LAMPORTS_PER_SOL) / (amount_to_sell_lamports / (10**DEFAULT_TOKEN_DECIMALS)) if amount_to_sell_lamports > 0 else 0; logger.info(f"Quote est price: {est_price:.9f} SOL/Token")
        except Exception as price_e: logger.warning(f"Could not calc price from quote: {price_e}")

        # 2. Get Swap Transaction
        swap_response = await self._get_jupiter_swap_tx(quote_response, self.wallet.pubkey)
        if not swap_response: return TradeResult(success=False, error_message="Failed Jupiter swap tx", price=est_price)

        # 3. Deserialize, Sign, Send, Confirm
        swap_tx_str = swap_response.get("swapTransaction")
        if not swap_tx_str: return TradeResult(success=False, error_message="Jupiter response missing swapTransaction", price=est_price)

        final_signature: Optional[Signature] = None; error_message = "Transaction failed"; error_type = "Unknown"
        try:
            logger.info("Processing Jupiter swap transaction...")
            swap_tx_bytes = base64.b64decode(swap_tx_str)
            swap_tx_deserialized = VersionedTransaction.from_bytes(swap_tx_bytes)

            # --- Corrected Signing ---
            # Sign the deserialized transaction INSTANCE
            swap_tx_signed = swap_tx_deserialized.sign([self.wallet.payer])
            # --- End Correction ---

            raw_tx_bytes = bytes(swap_tx_signed)

            # Direct Send using Async Client
            opts = self.client.get_default_send_options(skip_preflight=False)
            logger.debug(f"Sending raw Jupiter swap tx ({len(raw_tx_bytes)} bytes)...")
            txid_resp = await self.async_client.send_raw_transaction(raw_tx_bytes, opts=opts)
            final_signature = txid_resp.value # Signature object
            logger.info(f"Jupiter Swap TX Sent: {str(final_signature)}")

            # Confirmation
            logger.debug(f"Confirming Jupiter Swap TX: {str(final_signature)}")
            # --- Pass Commitment Enum ---
            confirmation_status = await self.client.confirm_transaction(
                 final_signature, commitment=Confirmed, timeout_secs=90 # Use Confirmed enum
            )
            # --- End Pass Enum ---

            # --- Use str() for status name ---
            status_str = str(confirmation_status) if confirmation_status else "Unknown/Timeout"
            # --- End Use str() ---

            if confirmation_status == TransactionConfirmationStatus.Confirmed or confirmation_status == TransactionConfirmationStatus.Finalized:
                 logger.info(f"Jupiter Swap TX CONFIRMED: {str(final_signature)}")
                 return TradeResult(
                     success=True, signature=str(final_signature),
                     price=est_price, amount=amount_to_sell_lamports / (10**DEFAULT_TOKEN_DECIMALS)
                 )
            else:
                 error_message = f"Swap tx confirm failed/timed out. Status: {status_str}"; error_type = "ConfirmTimeout"; logger.warning(error_message)

        except Exception as e:
            error_message = f"Error during Jupiter swap execution: {e}"; error_type = "SwapExecutionError"; logger.error(error_message, exc_info=True)

        # Return failure
        return TradeResult( success=False, error_message=error_message, error_type=error_type, signature=str(final_signature) if final_signature else None, price=est_price )