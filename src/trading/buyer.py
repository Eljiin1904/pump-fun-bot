# src/trading/buyer.py

import asyncio
from typing import Optional, Any

from solders.pubkey import Pubkey
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.transaction_status import TransactionConfirmationStatus
from solders.signature import Signature
from solana.rpc.commitment import Confirmed

from ..core.client import SolanaClient
from ..core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL, DEFAULT_TOKEN_DECIMALS
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.wallet import Wallet
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..core.instruction_builder import InstructionBuilder
from ..trading.base import TokenInfo, TradeResult
from ..utils.logger import get_logger

logger = get_logger(__name__)

# Increased delays from config/previous step
FETCH_RETRY_DELAY_SECONDS = 4.0
PREFETCH_RETRY_DELAY_SECONDS = 3.0
MAX_PREFETCH_RETRIES = 5

class TokenBuyer:
    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        amount_sol: float,
        slippage_decimal: float,
        max_retries: int,
        confirm_timeout: int
    ):
            # (Init remains the same as before)
        self.client = client; self.wallet = wallet; self.curve_manager = curve_manager; self.priority_fee_manager = priority_fee_manager; self.amount_sol = amount_sol; self.amount_lamports = int(amount_sol * LAMPORTS_PER_SOL); self.slippage_decimal = slippage_decimal; self.max_retries = max_retries; self.confirm_timeout = confirm_timeout; logger.info(f"TokenBuyer Init: Amt={self.amount_sol:.6f}, Slip={self.slippage_decimal * 100:.2f}%")

    async def _prefetch_curve_state(self, bonding_curve_pubkey: Pubkey, token_symbol: str) -> Optional[BondingCurveState]:
        """Attempts to fetch the bonding curve state with retries."""
        for attempt in range(MAX_PREFETCH_RETRIES):
            logger.debug(f"Prefetch attempt {attempt + 1}/{MAX_PREFETCH_RETRIES} for {token_symbol} curve state...")
            try:
                curve_state = await self.curve_manager.get_curve_state(
                    bonding_curve_pubkey,
                    commitment=Confirmed
                )
                if curve_state:
                    logger.info(f"Prefetch successful for {token_symbol} curve state.")
                    return curve_state # Successfully fetched, exit the function
            except Exception as e:
                logger.warning(f"Prefetch attempt {attempt + 1} failed with error: {e}")
                # Optional: Decide if certain errors should stop retrying immediately

            # If fetch failed or returned None, wait before retrying (unless it's the last attempt)
            if attempt < MAX_PREFETCH_RETRIES - 1:
                logger.debug(f"Prefetch attempt {attempt + 1} unsuccessful, sleeping for {PREFETCH_RETRY_DELAY_SECONDS}s...")
                await asyncio.sleep(PREFETCH_RETRY_DELAY_SECONDS)

        # If the loop completes without returning, prefetch failed
        logger.warning(f"Prefetch failed for {token_symbol} curve state after {MAX_PREFETCH_RETRIES} attempts.")
        return None # Explicitly return None after all retries failed

    async def execute(self, token_info: TokenInfo) -> TradeResult:
        retry_count = 0; error_message = "Max retries"; initial_sol_reserves: Optional[int] = None
        user_pubkey = self.wallet.pubkey; mint_pubkey = token_info.mint
        try: bonding_curve_pubkey, _ = BondingCurveManager.find_bonding_curve_pda(mint_pubkey); associated_bonding_curve_pubkey, _ = BondingCurveManager.find_associated_bonding_curve_pda(mint_pubkey); logger.debug(f"Buy using Curve PDA: {bonding_curve_pubkey}, Vault PDA: {associated_bonding_curve_pubkey}")
        except Exception as pda_e: logger.error(f"BUYER: Failed PDA derivation {mint_pubkey}: {pda_e}"); return TradeResult(success=False, error_message="Failed PDA derivation")
        user_token_account = Wallet.get_associated_token_address(user_pubkey, mint_pubkey)

        initial_curve_state = await self._prefetch_curve_state(bonding_curve_pubkey, token_info.symbol)
        if not initial_curve_state: return TradeResult(success=False, error_message="Failed to fetch bonding curve state during prefetch.")
        initial_sol_reserves = initial_curve_state.virtual_sol_reserves

        while retry_count < self.max_retries:
            retry_count += 1; logger.info(f"Buy attempt {retry_count}/{self.max_retries} for {token_info.symbol} ({mint_pubkey})")
            # --- FIX: Initialize curve_state to satisfy linter ---
            curve_state: Optional[BondingCurveState] = None
            # --- End Fix ---
            try:
                if retry_count == 1: curve_state = initial_curve_state
                else: curve_state = await self.curve_manager.get_curve_state( bonding_curve_pubkey, commitment=Confirmed )

                if not curve_state: error_message = "Failed to fetch bonding curve state on retry."; logger.warning(f"{error_message} Retrying main loop..."); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                if initial_sol_reserves is None: initial_sol_reserves = curve_state.virtual_sol_reserves # Should be set by prefetch

                try:
                    est_tokens_out = curve_state.calculate_tokens_out_for_sol(self.amount_lamports)
                    min_tokens_out_for_slippage = int(est_tokens_out * (1 - self.slippage_decimal))
                    min_token_output_for_ix = max(0, min_tokens_out_for_slippage) # Use calculated minimum w/ slippage
                    logger.debug(f"Est TknOut: {est_tokens_out}, MinTknOut (Slippage): {min_token_output_for_ix}, MaxSOLIn: {self.amount_lamports}")
                    if min_token_output_for_ix <= 0 and est_tokens_out <= 0 : error_message = "Min tokens out calc <= 0."; logger.warning(error_message); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue # Retry if calc is weird
                except Exception as calc_e: error_message = f"Error in buy calculation: {calc_e}"; logger.error(f"{error_message}. Retrying main loop..."); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                try:
                     compute_unit_price_micro_lamports = await self.priority_fee_manager.get_fee()
                     if compute_unit_price_micro_lamports is None: compute_unit_price_micro_lamports = 0
                except Exception as fee_e: logger.error(f"Error getting fee: {fee_e}. Using 0."); compute_unit_price_micro_lamports = 0
                compute_unit_limit = 200_000

                instructions = [ set_compute_unit_limit(compute_unit_limit), set_compute_unit_price(int(compute_unit_price_micro_lamports)) ]
                ata_instructions = InstructionBuilder.get_create_ata_instruction( payer_pubkey=user_pubkey, owner=user_pubkey, mint=mint_pubkey )
                instructions.extend(ata_instructions)

                # --- FIX: Pass correct keyword arguments ---
                buy_instruction = InstructionBuilder.get_pump_buy_instruction(
                    user_pubkey=user_pubkey,
                    mint_pubkey=mint_pubkey,
                    bonding_curve_pubkey=bonding_curve_pubkey,
                    associated_bonding_curve_pubkey=associated_bonding_curve_pubkey,
                    user_token_account=user_token_account,
                    amount_lamports=self.amount_lamports,     # Pass the SOL amount
                    min_token_output=min_token_output_for_ix # Pass the calculated min token out
                )
                # --- End Fix ---
                if buy_instruction is None: error_message = "Failed to build buy instruction."; logger.error(error_message); break
                instructions.append(buy_instruction)

                tx_result: TransactionResult = await build_and_send_transaction( client=self.client, payer=self.wallet.payer, instructions=instructions, label=f"Buy_{token_info.symbol[:5]}" )

                if tx_result.success and tx_result.signature:
                    logger.info(f"Buy tx sent: {tx_result.signature}. Confirming..."); signature_obj = Signature.from_string(tx_result.signature)
                    confirmation_status = await self.client.confirm_transaction( signature_obj, timeout_secs=self.confirm_timeout, commitment=Confirmed )
                    status_str = str(confirmation_status) if confirmation_status else "Timeout/Failed"
                    if confirmation_status and confirmation_status >= Confirmed:
                        logger.info(f"Buy tx CONFIRMED: {tx_result.signature}")
                        amount_tokens_bought_estimate_ui = est_tokens_out / (10 ** DEFAULT_TOKEN_DECIMALS) if est_tokens_out > 0 else 0.0
                        buy_price_sol_per_token_estimate = self.amount_sol / amount_tokens_bought_estimate_ui if amount_tokens_bought_estimate_ui > 0 else 0.0
                        return TradeResult( success=True, tx_signature=tx_result.signature, price=buy_price_sol_per_token_estimate, amount=amount_tokens_bought_estimate_ui, initial_sol_liquidity=initial_sol_reserves )
                    else: error_message = f"Buy tx {tx_result.signature} confirm failed. Status: {status_str}"; logger.warning(error_message); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue
                elif tx_result.error_type: error_message = f"Buy tx failed ({tx_result.error_type}): {tx_result.error_message or ''}"; logger.warning(f"{error_message}. Retry..."); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 1); continue
                else: error_message = "Buy tx send failed unknown error."; logger.error(error_message); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 1); continue
            except Exception as e: error_message = f"Unexpected error on buy attempt {retry_count}: {e}"; logger.error(error_message, exc_info=True); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 3)

        logger.error(f"Failed buy {token_info.symbol} after {self.max_retries} attempts: {error_message}")
        return TradeResult(success=False, error_message=error_message, initial_sol_liquidity=initial_sol_reserves)