# src/trading/seller.py

import asyncio
from typing import Optional, Any # Use Any for balance_response type hint

from solders.pubkey import Pubkey
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.transaction_status import TransactionConfirmationStatus
from solders.signature import Signature
# Removed incorrect TokenAmount import
from solana.rpc.commitment import Confirmed

from ..core.client import SolanaClient
from ..core.wallet import Wallet
from ..core.curve import ( BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL, DEFAULT_TOKEN_DECIMALS )
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..core.instruction_builder import InstructionBuilder
from ..trading.base import TokenInfo, TradeResult
from ..utils.logger import get_logger

logger = get_logger(__name__)
SELL_RETRY_DELAY_SECONDS = 3

class TokenSeller:
    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        slippage: float, # decimal
        max_retries: int,
        confirm_timeout: int
    ):
        self.client = client; self.wallet = wallet; self.curve_manager = curve_manager; self.priority_fee_manager = priority_fee_manager; self.slippage_decimal = slippage; self.max_retries = max_retries; self.confirm_timeout = confirm_timeout; logger.info(f"TokenSeller (Bonding Curve) initialized with Slippage: {self.slippage_decimal * 100:.2f}%")

    async def execute(self, token_info: TokenInfo, buyer_result: Optional[TradeResult] = None) -> TradeResult:
        retry_count = 0; error_message = "Max retries"; initial_buy_price = buyer_result.price if buyer_result else None
        user_pubkey = self.wallet.pubkey; mint_pubkey = token_info.mint
        try: bonding_curve_pubkey, _ = BondingCurveManager.find_bonding_curve_pda(mint_pubkey); associated_bonding_curve_pubkey, _ = BondingCurveManager.find_associated_bonding_curve_pda(mint_pubkey)
        except Exception as pda_e: logger.error(f"SELLER: Failed PDA derivation {mint_pubkey}: {pda_e}"); return TradeResult(success=False, error_message="Failed PDA derivation")
        user_token_account = Wallet.get_associated_token_address(user_pubkey, mint_pubkey)

        while retry_count < self.max_retries:
            retry_count += 1; logger.info(f"Sell attempt {retry_count}/{self.max_retries} for {token_info.symbol} ({mint_pubkey})")
            try:
                # 1. Get current balance to sell
                # Client returns value object (Any) or None
                balance_response: Optional[Any] = await self.client.get_token_account_balance( user_token_account, commitment=Confirmed )
                amount_tokens_to_sell = 0
                # --- FIX: Check attributes instead of type ---
                if balance_response and hasattr(balance_response, 'amount') and hasattr(balance_response, 'ui_amount_string'):
                    try:
                        amount_tokens_to_sell = int(balance_response.amount)
                    except (ValueError, TypeError):
                        logger.warning(f"Seller: Could not parse balance amount '{balance_response.amount}' for {user_token_account}. Assuming 0.")
                        amount_tokens_to_sell = 0
                elif balance_response is not None:
                    logger.warning(f"Seller received unexpected balance response structure for {user_token_account}: {balance_response}")
                # If balance_response is None, amount_tokens_to_sell remains 0

                if amount_tokens_to_sell <= 0:
                    logger.warning(f"No balance in ATA {user_token_account} for {token_info.symbol}. Assuming already sold or error.")
                    # Consider if this should be success or failure based on context
                    return TradeResult(success=True, tx_signature="N/A - Balance Zero", price=0.0, amount=0.0)
                logger.info(f"Current balance to sell: {amount_tokens_to_sell} lamports of {token_info.symbol}")

                # 2. Get curve state for calculation
                curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state( bonding_curve_pubkey, commitment=Confirmed )
                if not curve_state: error_message = "Failed fetch curve state for sell."; logger.warning(f"{error_message} Retry..."); await asyncio.sleep(SELL_RETRY_DELAY_SECONDS); continue

                # 3. Calculate min SOL out
                try:
                    est_sol_out_lamports = curve_state.calculate_sol_out_for_tokens(amount_tokens_to_sell)
                    min_sol_out_for_slippage = int(est_sol_out_lamports * (1 - self.slippage_decimal))
                    logger.debug(f"Est SOL Out: {est_sol_out_lamports}, MinSOLOut (Slippage): {min_sol_out_for_slippage}")
                    min_sol_out_for_slippage = max(0, min_sol_out_for_slippage) # Ensure not negative
                except Exception as calc_e: error_message = f"Error in sell calc: {calc_e}"; logger.error(f"{error_message}. Retry..."); await asyncio.sleep(SELL_RETRY_DELAY_SECONDS); continue

                # 4. Get priority fees
                try:
                    # Use the get_fee method
                    compute_unit_price_micro_lamports = await self.priority_fee_manager.get_fee()
                    if compute_unit_price_micro_lamports is None: compute_unit_price_micro_lamports = 0
                    logger.debug(f"Seller using priority fee: {compute_unit_price_micro_lamports}")
                except Exception as fee_e: logger.error(f"Error getting fee for sell: {fee_e}. Using 0."); compute_unit_price_micro_lamports = 0
                compute_unit_limit = 200_000 # Adjust if needed for sell ix

                # 5. Build transaction
                instructions = [ set_compute_unit_limit(compute_unit_limit), set_compute_unit_price(int(compute_unit_price_micro_lamports)) ]
                sell_instruction = InstructionBuilder.get_pump_sell_instruction( user_pubkey=user_pubkey, mint_pubkey=mint_pubkey, bonding_curve_pubkey=bonding_curve_pubkey, associated_bonding_curve_pubkey=associated_bonding_curve_pubkey, user_token_account=user_token_account, token_amount_to_sell=amount_tokens_to_sell, min_sol_output_lamports=min_sol_out_for_slippage )
                if sell_instruction is None: error_message = "Failed build sell instruction."; logger.error(error_message); break # Exit loop if build fails
                instructions.append(sell_instruction)

                # 6. Send and confirm
                tx_result: TransactionResult = await build_and_send_transaction( client=self.client, payer=self.wallet.payer, instructions=instructions, label=f"Sell_{token_info.symbol[:5]}" )

                if tx_result.success and tx_result.signature:
                    logger.info(f"Sell tx sent: {tx_result.signature}. Confirming..."); signature_obj = Signature.from_string(tx_result.signature)
                    confirmation_status = await self.client.confirm_transaction( signature_obj, timeout_secs=self.confirm_timeout, commitment=Confirmed )
                    status_str = str(confirmation_status) if confirmation_status else "Timeout/Failed"
                    if confirmation_status and confirmation_status >= Confirmed:
                        logger.info(f"Sell tx CONFIRMED: {tx_result.signature}")
                        # Calculate price based on estimate
                        sell_price_sol_per_token = 0.0
                        if amount_tokens_to_sell > 0:
                             ui_tokens_sold = amount_tokens_to_sell / (10**DEFAULT_TOKEN_DECIMALS)
                             sol_received_estimate = est_sol_out_lamports / LAMPORTS_PER_SOL
                             if ui_tokens_sold > 0:
                                 sell_price_sol_per_token = sol_received_estimate / ui_tokens_sold
                        return TradeResult( success=True, tx_signature=tx_result.signature, price=sell_price_sol_per_token, amount=sol_received_estimate ) # Return SOL received
                    else: error_message = f"Sell tx {tx_result.signature} confirm failed/timeout. Status: {status_str}"; logger.warning(error_message); await asyncio.sleep(SELL_RETRY_DELAY_SECONDS); continue # Retry confirm/sell
                elif tx_result.error_type: error_message = f"Sell tx failed ({tx_result.error_type}): {tx_result.error_message or ''}"; logger.warning(f"{error_message}. Retry..."); await asyncio.sleep(SELL_RETRY_DELAY_SECONDS + 1); continue
                else: error_message = "Sell tx send failed unknown error."; logger.error(error_message); await asyncio.sleep(SELL_RETRY_DELAY_SECONDS + 1); continue
            except Exception as e: error_message = f"Unexpected error sell attempt {retry_count}: {e}"; logger.error(error_message, exc_info=True); await asyncio.sleep(SELL_RETRY_DELAY_SECONDS + 3)

        logger.error(f"Failed sell {token_info.symbol} after {self.max_retries} attempts: {error_message}")
        return TradeResult(success=False, error_message=error_message)