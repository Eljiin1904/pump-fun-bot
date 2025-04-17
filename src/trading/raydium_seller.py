# src/trading/raydium_seller.py

import asyncio
from typing import Optional, Any, Dict, List

from solders.pubkey import Pubkey
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID
WSOL_ADDRESS = Pubkey.from_string("So11111111111111111111111111111111111111112")
from solders.transaction_status import TransactionConfirmationStatus
from solana.rpc.commitment import Confirmed

# --- Project Imports ---
from ..core.client import SolanaClient
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.wallet import Wallet
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..trading.base import TokenInfo, TradeResult
from ..utils.logger import get_logger
# Import Raydium specific tools if available
try:
    from ..tools.raydium_amm import RaydiumAMMInfoFetcher, RaydiumSwapWrapper
    RAYDIUM_TOOLS_ENABLED = True
except ImportError:
    try: temp_logger = get_logger(__name__); temp_logger.warning("Raydium tools (AMMInfoFetcher, SwapWrapper) not found. RaydiumSeller will not function.")
    except: print("WARNING: Raydium components not found, Raydium features disabled (logger not init).")
    RaydiumAMMInfoFetcher = None
    RaydiumSwapWrapper = None
    RAYDIUM_TOOLS_ENABLED = False

if 'logger' not in locals(): logger = get_logger(__name__)

DEFAULT_COMPUTE_UNIT_LIMIT_RAYDIUM = 600_000
FETCH_RETRY_DELAY_SECONDS = 4.0

class RaydiumSeller:
    """ Handles selling tokens on Raydium AMM pools. """

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        slippage_bps: int,
        max_retries: int = 3,
        confirm_timeout: int = 60,
        compute_unit_limit: int = DEFAULT_COMPUTE_UNIT_LIMIT_RAYDIUM
    ):
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.slippage_bps = slippage_bps
        self.max_retries = max_retries
        self.confirm_timeout = confirm_timeout
        self.compute_unit_limit = compute_unit_limit
        self.amm_fetcher = RaydiumAMMInfoFetcher(client) if RAYDIUM_TOOLS_ENABLED and RaydiumAMMInfoFetcher else None
        self.swap_wrapper = RaydiumSwapWrapper() if RAYDIUM_TOOLS_ENABLED and RaydiumSwapWrapper else None
        if not RAYDIUM_TOOLS_ENABLED or not self.amm_fetcher or not self.swap_wrapper: logger.error("RaydiumSeller initialized BUT Raydium tools are missing/disabled. Sell operations will fail.")
        logger.info(f"RaydiumSeller Init: Slip={self.slippage_bps} BPS, MaxRetries={self.max_retries}, CULimit={self.compute_unit_limit}")

    async def execute(self, token_info: TokenInfo, amount_in_lamports: int) -> TradeResult:
        """ Attempts to sell the specified amount of tokens on Raydium. """
        mint_str = str(token_info.mint); logger.info(f"Attempting Raydium sell for {token_info.symbol} ({mint_str[:6]} - {amount_in_lamports} lamports)...")
        if not self.amm_fetcher or not self.swap_wrapper: logger.error(f"Raydium Sell {token_info.symbol}: Tools not available."); return TradeResult(success=False, error_message="Raydium tools not available/enabled.")
        user_pubkey = self.wallet.pubkey; mint_pubkey = token_info.mint
        retry_count = 0; error_message = "Max Raydium sell retries reached"
        while retry_count < self.max_retries:
            retry_count += 1; logger.info(f"Raydium sell attempt {retry_count}/{self.max_retries} for {token_info.symbol}")
            try:
                pool_info: Optional[Dict[str, Any]] = await self.amm_fetcher.get_pool_info_for_token(str(mint_pubkey))
                if not pool_info or 'amm_id' not in pool_info: logger.warning(f"Raydium Sell: No AMM pool found for {token_info.symbol} ({mint_str}). Cannot sell on Raydium."); error_message = "No Raydium pool found for token"; break
                amm_id = Pubkey.from_string(pool_info['amm_id']); logger.debug(f"Found Raydium Pool: {amm_id}")
                compute_unit_price_micro_lamports = 0
                try:
                    # *** FIX: Call correct method ***
                    fee_accounts: List[Pubkey] = [amm_id]
                    fee = await self.priority_fee_manager.calculate_priority_fee(accounts=fee_accounts)
                    if fee is not None: compute_unit_price_micro_lamports = fee; logger.info(f"Using priority fee for Raydium sell: {compute_unit_price_micro_lamports} micro-lamports")
                    else: logger.info("Priority Fee Manager returned None for Raydium sell. Using 0.")
                except AttributeError: logger.error("PriorityFeeManager missing 'calculate_priority_fee' method. Using 0 fee.")
                except Exception as fee_e: logger.error(f"Error calculating priority fee for Raydium sell: {fee_e}. Using 0.", exc_info=True)
                swap_instructions: Optional[List[Any]] = None
                try:
                    input_token_mint = mint_pubkey; output_token_mint = WSOL_ADDRESS
                    swap_instructions = await self.swap_wrapper.build_swap_instructions( pool_info=pool_info, input_mint=input_token_mint, output_mint=output_token_mint, amount_in=amount_in_lamports, slippage_bps=self.slippage_bps, owner_pubkey=user_pubkey )
                    if not swap_instructions: logger.error(f"Raydium Sell: Failed to build swap instructions for {token_info.symbol}."); error_message = "Failed to build Raydium swap instructions"; await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue
                except Exception as build_e: logger.error(f"Error building Raydium swap instructions for {token_info.symbol}: {build_e}", exc_info=True); error_message = f"Build swap instruction error: {build_e}"; await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue
                instructions: List[Any] = [set_compute_unit_limit(self.compute_unit_limit), set_compute_unit_price(int(compute_unit_price_micro_lamports)), *swap_instructions]
                tx_result: TransactionResult = await build_and_send_transaction( client=self.client, payer=self.wallet.payer, instructions=instructions, label=f"Sell_Raydium_{token_info.symbol[:5]}" )
                if tx_result.success and tx_result.signature:
                    logger.info(f"Raydium Sell tx sent: {tx_result.signature}. Confirming..."); signature_obj = Signature.from_string(tx_result.signature); confirmation_status = await self.client.confirm_transaction( signature_obj, timeout_secs=self.confirm_timeout, commitment=TransactionConfirmationStatus.Confirmed ); status_str = str(confirmation_status) if confirmation_status else "Timeout/Failed"; is_confirmed = confirmation_status is not None and confirmation_status >= TransactionConfirmationStatus.Confirmed
                    if is_confirmed: logger.info(f"Raydium Sell tx CONFIRMED: {tx_result.signature}"); return TradeResult( success=True, tx_signature=tx_result.signature, price=None, amount=None )
                    else: error_message = f"Raydium Sell tx {tx_result.signature} confirmation failed. Status: {status_str}"; logger.warning(error_message); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue
                elif tx_result.error_type: error_message = f"Raydium Sell tx failed ({tx_result.error_type}): {tx_result.error_message or 'No specific message'}"; logger.warning(f"{error_message}. Retrying..."); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 1); continue
                else: error_message = "Raydium Sell tx send failed with unknown error."; logger.error(error_message); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 1); continue
            except Exception as e: error_message = f"Unexpected error during Raydium sell attempt {retry_count}: {e}"; logger.error(error_message, exc_info=True);
            if "No Raydium pool found" in error_message: break
            await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 3)
        logger.error(f"Failed Raydium sell {token_info.symbol} after {self.max_retries} attempts: {error_message}"); return TradeResult(success=False, error_message=error_message, amount=0.0, price=0.0)