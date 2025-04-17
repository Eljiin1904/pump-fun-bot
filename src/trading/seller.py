# src/trading/seller.py

import asyncio
from typing import Optional, Any, List

from solders.pubkey import Pubkey
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.signature import Signature
from solders.transaction_status import TransactionConfirmationStatus
from solana.rpc.commitment import Confirmed, Processed

from ..core.client import SolanaClient
# *** FIX: Correct import path for DEFAULT_TOKEN_DECIMALS ***
from ..core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL, DEFAULT_TOKEN_DECIMALS
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.wallet import Wallet
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..core.instruction_builder import InstructionBuilder
from ..trading.base import TokenInfo, TradeResult
from ..utils.logger import get_logger

logger = get_logger(__name__)

FETCH_RETRY_DELAY_SECONDS = 4.0
DEFAULT_COMPUTE_UNIT_LIMIT_SELL = 150_000

class TokenSeller:
    """ Handles the logic for selling tokens back to the pump.fun bonding curve. """

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        slippage: float, # Decimal
        max_retries: int,
        confirm_timeout: int,
        compute_unit_limit: int = DEFAULT_COMPUTE_UNIT_LIMIT_SELL
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.slippage_decimal = slippage
        self.max_retries = max_retries
        self.confirm_timeout = confirm_timeout
        self.compute_unit_limit = compute_unit_limit
        logger.info(f"TokenSeller Init: Slip={self.slippage_decimal*100:.1f}%, MaxRetries={self.max_retries}, CULimit={self.compute_unit_limit}")

    async def execute(self, token_info: TokenInfo, buyer_result: Optional[TradeResult] = None) -> TradeResult:
        """ Attempts to sell the user's entire balance of the specified token. """
        mint_str = str(token_info.mint)
        logger.info(f"Attempting sell for {token_info.symbol} ({mint_str[:6]})...")

        user_pubkey = self.wallet.pubkey
        mint_pubkey = token_info.mint
        user_token_account = Wallet.get_associated_token_address(user_pubkey, mint_pubkey)

        amount_to_sell_lamports: Optional[int] = None
        try:
            balance_lamports = await self.client.get_token_balance_lamports(user_token_account, commitment=Confirmed)
            if balance_lamports is None or balance_lamports <= 0: logger.warning(f"Sell {token_info.symbol}: Balance is zero or could not be fetched."); return TradeResult(success=False, error_message="Balance zero or fetch failed", amount=0)
            logger.info(f"Current balance for {token_info.symbol}: {balance_lamports} lamports."); amount_to_sell_lamports = balance_lamports
        except Exception as e: logger.error(f"Error fetching token balance for {user_token_account}: {e}", exc_info=True); return TradeResult(success=False, error_message=f"Balance fetch error: {e}")
        if amount_to_sell_lamports is None or amount_to_sell_lamports <= 0: logger.error(f"Sell {token_info.symbol}: Invalid amount_to_sell_lamports after balance check."); return TradeResult(success=False, error_message="Invalid sell amount derived", amount=0)

        retry_count = 0; error_message = "Max sell retries reached"
        while retry_count < self.max_retries:
            retry_count += 1; logger.info(f"Sell attempt {retry_count}/{self.max_retries} for {token_info.symbol} ({mint_str[:6]} - {amount_to_sell_lamports} lamports)")
            try:
                try: bonding_curve_pubkey, _ = BondingCurveManager.find_bonding_curve_pda(mint_pubkey); associated_bonding_curve_pubkey, _ = BondingCurveManager.find_associated_bonding_curve_pda(mint_pubkey); logger.debug(f"Sell using Curve PDA: {bonding_curve_pubkey}, Vault PDA: {associated_bonding_curve_pubkey}")
                except Exception as pda_e: logger.error(f"SELLER: Failed PDA derivation {mint_pubkey}: {pda_e}"); error_message = "Failed PDA derivation during sell"; break

                curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(bonding_curve_pubkey, commitment=Confirmed)
                if not curve_state: logger.warning(f"Sell attempt {retry_count}: Could not get curve state. Retrying..."); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                est_sol_out_lamports: int = 0; min_sol_output_lamports: int = 0
                try:
                    est_sol_out_lamports = curve_state.calculate_sol_out_for_tokens(amount_to_sell_lamports); min_sol_output_lamports = int(est_sol_out_lamports * (1 - self.slippage_decimal)); min_sol_output_lamports = max(0, min_sol_output_lamports); logger.debug(f"Est SOL Out: {est_sol_out_lamports}, MinSOLOut (Slippage): {min_sol_output_lamports}")
                except Exception as calc_e: logger.error(f"Error calculating sell output for {token_info.symbol}: {calc_e}", exc_info=True); error_message = "Sell calculation error"; await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                compute_unit_price_micro_lamports = 0
                try:
                    # *** FIX: Call correct method ***
                    fee = await self.priority_fee_manager.calculate_priority_fee(accounts=None)
                    if fee is not None: compute_unit_price_micro_lamports = fee; logger.info(f"Using priority fee for sell: {compute_unit_price_micro_lamports} micro-lamports")
                    else: logger.info("Priority Fee Manager returned None for sell. Using 0.")
                except AttributeError: logger.error("PriorityFeeManager missing 'calculate_priority_fee' method. Using 0 fee.")
                except Exception as fee_e: logger.error(f"Error calculating priority fee for sell: {fee_e}. Using 0.", exc_info=True)

                instructions: List[Any] = [set_compute_unit_limit(self.compute_unit_limit), set_compute_unit_price(int(compute_unit_price_micro_lamports))]
                sell_instruction = InstructionBuilder.get_pump_sell_instruction( user_pubkey=user_pubkey, mint_pubkey=mint_pubkey, bonding_curve_pubkey=bonding_curve_pubkey, associated_bonding_curve_pubkey=associated_bonding_curve_pubkey, user_token_account=user_token_account, token_amount=amount_to_sell_lamports, min_sol_output=min_sol_output_lamports )
                if sell_instruction is None: error_message = "Failed to build sell instruction. Stopping retries."; logger.error(error_message); break
                instructions.append(sell_instruction)

                tx_result: TransactionResult = await build_and_send_transaction( client=self.client, payer=self.wallet.payer, instructions=instructions, label=f"Sell_{token_info.symbol[:5]}" )
                if tx_result.success and tx_result.signature:
                    logger.info(f"Sell tx sent: {tx_result.signature}. Confirming..."); signature_obj = Signature.from_string(tx_result.signature); confirmation_status = await self.client.confirm_transaction( signature_obj, timeout_secs=self.confirm_timeout, commitment=TransactionConfirmationStatus.Confirmed ); status_str = str(confirmation_status) if confirmation_status else "Timeout/Failed"; is_confirmed = confirmation_status is not None and confirmation_status >= TransactionConfirmationStatus.Confirmed
                    if is_confirmed:
                        logger.info(f"Sell tx CONFIRMED: {tx_result.signature}"); sell_price_sol_per_token_estimate = 0.0
                        if amount_to_sell_lamports > 0 and DEFAULT_TOKEN_DECIMALS is not None:
                             try: sell_price_sol_per_token_estimate = (est_sol_out_lamports / LAMPORTS_PER_SOL) / (amount_to_sell_lamports / (10**DEFAULT_TOKEN_DECIMALS))
                             except ZeroDivisionError: pass
                        return TradeResult( success=True, tx_signature=tx_result.signature, price=sell_price_sol_per_token_estimate, amount=(est_sol_out_lamports / LAMPORTS_PER_SOL), initial_sol_liquidity=None )
                    else: error_message = f"Sell tx {tx_result.signature} confirmation failed. Status: {status_str}"; logger.warning(error_message); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue
                elif tx_result.error_type: error_message = f"Sell tx failed ({tx_result.error_type}): {tx_result.error_message or 'No specific message'}"; logger.warning(f"{error_message}. Retrying..."); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 1); continue
                else: error_message = "Sell tx send failed with unknown error."; logger.error(error_message); await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 1); continue
            except Exception as e: error_message = f"Unexpected error during sell attempt {retry_count}: {e}"; logger.error(error_message, exc_info=True);
            if "Failed to build sell instruction" in str(e): break
            await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 3)
        logger.error(f"Failed sell {token_info.symbol} after {self.max_retries} attempts: {error_message}"); return TradeResult(success=False, error_message=error_message, amount=0.0, price=0.0)