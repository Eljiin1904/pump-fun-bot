# src/trading/raydium_seller.py
from typing import Optional
import asyncio
import base64

from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.core import RPCException

from .base import TokenInfo, TradeResult
from ..core.client import SolanaClient
from ..core.wallet import Wallet
from ..utils.logger import get_logger
from ..data.raydium_data import get_raydium_pool_data

logger = get_logger(__name__)

WSOL_MINT_ADDRESS = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v4/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v4/swap"

class RaydiumSeller:
    def __init__(self, client: SolanaClient, wallet: Wallet, **kwargs):
        """
        Handles selling tokens via Raydium (using Jupiter aggregator).
        """
        self.client = client
        self.wallet = wallet
        self.sell_slippage_bps: int = kwargs.get("sell_slippage_bps", 2500)  # e.g. 2500 = 25%
        self.max_retries: int = kwargs.get("max_retries", 2)
        self.confirm_timeout: int = kwargs.get("confirm_timeout", 60)
        self.quote_url: str = kwargs.get("quote_url", JUPITER_QUOTE_URL)
        self.swap_url: str = kwargs.get("swap_url", JUPITER_SWAP_URL)

    async def execute(self, token_info: TokenInfo, amount_to_sell_lamports: int) -> TradeResult:
        """
        Execute selling the given token amount (in lamports) via Raydium/Jupiter.
        Returns TradeResult with success status and tx signature if successful.
        """
        mint_address = str(token_info.mint)
        params = {
            "inputMint": mint_address,
            "outputMint": WSOL_MINT_ADDRESS,
            "amount": str(amount_to_sell_lamports),
            "slippageBps": str(self.sell_slippage_bps),
            "onlyDirectRoutes": "true"
        }
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                # Allow broader routing on retry
                if attempt > 1:
                    params["onlyDirectRoutes"] = "false"
                    logger.info(f"No direct pool route for {token_info.symbol}, trying Jupiter aggregator...")
                # Get quote from Jupiter API
                import httpx
                async with httpx.AsyncClient() as http_client:
                    quote_resp = await http_client.get(self.quote_url, params=params)
                quote_data = quote_resp.json() if quote_resp.status_code == 200 else None
                if not quote_data or "data" not in quote_data or len(quote_data["data"]) == 0:
                    last_error = "No swap route available"
                    logger.warning(f"No swap route found for {token_info.symbol} (attempt {attempt}).")
                    continue
                route_info = quote_data["data"][0]
                # Request swap transaction from Jupiter
                swap_request = {
                    "route": route_info,
                    "userPublicKey": str(self.wallet.pubkey),
                    "wrapUnwrapSOL": True
                }
                async with httpx.AsyncClient() as http_client:
                    swap_resp = await http_client.post(self.swap_url, json=swap_request)
                if swap_resp.status_code != 200:
                    swap_resp.raise_for_status()
                swap_data = swap_resp.json()
                if "swapTransaction" not in swap_data:
                    last_error = swap_data.get("error", "Unknown swap error")
                    logger.error(f"Jupiter swap API returned error for {token_info.symbol}: {last_error}")
                    continue
                # Decode and sign the swap transaction
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                transaction = VersionedTransaction.from_bytes(tx_bytes)
                try:
                    sig = self.wallet.payer.sign_message(to_bytes_versioned(transaction.message))
                    sig_list = list(transaction.signatures)
                    if not sig_list:
                        sig_list = [sig]
                    else:
                        sig_list[0] = sig
                    transaction.signatures = sig_list
                except Exception as e:
                    last_error = f"Failed to sign swap transaction: {e}"
                    logger.error(last_error)
                    continue
                # Send the signed transaction
                try:
                    tx_signature = await self.client.async_client.send_raw_transaction(bytes(transaction), opts={"skip_preflight": True, "max_retries": 3})
                except Exception as send_err:
                    last_error = f"Transaction send failed: {send_err}"
                    logger.warning(last_error)
                    continue
                # Confirm transaction
                confirmed = False
                for _ in range(self.confirm_timeout):
                    status_resp = await self.client.async_client.get_signature_statuses([tx_signature])
                    if status_resp.value and status_resp.value[0] and status_resp.value[0].confirmation_status in ("confirmed", "finalized"):
                        confirmed = True
                        break
                    await asyncio.sleep(1)
                if not confirmed:
                    last_error = f"Swap transaction {tx_signature} not confirmed in time"
                    logger.warning(last_error)
                    continue
                logger.info(f"Swap transaction confirmed: {tx_signature}")
                # Estimate price and amount from quote data (if available)
                price_per_token = None
                token_amount = None
                try:
                    in_amt = float(route_info.get("inAmount")) if route_info.get("inAmount") else None
                    out_amt = float(route_info.get("outAmount")) if route_info.get("outAmount") else None
                    if in_amt and out_amt:
                        price_per_token = (out_amt / 1e9) / (in_amt / 1e9)
                        token_amount = in_amt / 1e9
                except Exception:
                    price_per_token = None
                return TradeResult(success=True, tx_signature=tx_signature, price=price_per_token, amount=token_amount)
            except Exception as e:
                last_error = f"Unexpected error during Raydium sell: {e}"
                logger.error(last_error, exc_info=True)
                await asyncio.sleep(2)
                continue
        logger.error(f"Failed to sell {token_info.symbol} on Raydium after {self.max_retries} attempts: {last_error}")
        return TradeResult(success=False, error_message=last_error)
