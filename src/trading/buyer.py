# src/trading/buyer.py

import asyncio
from typing import Optional

# --- Solana/Solders Imports ---
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.transaction_status import TransactionConfirmationStatus
from solders.signature import Signature # Keep this import

from ..core.client import SolanaClient
# --- Import Constants FROM curve.py ---
from ..core.curve import (
    BondingCurveManager,
    BondingCurveState,
    LAMPORTS_PER_SOL,          # Import constant
    DEFAULT_TOKEN_DECIMALS     # Import constant
)
# --- End Import ---
from ..core.priority_fee.manager import PriorityFeeManager # Verify method name get_fee()
from ..core.wallet import Wallet
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..core.instruction_builder import InstructionBuilder
from ..trading.base import TokenInfo, TradeResult
from ..utils.logger import get_logger

logger = get_logger(__name__)
# LAMPORTS_PER_SOL = 1_000_000_000 # Defined in curve.py now

class TokenBuyer:
    """Handles the token buying process on the bonding curve."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        amount_sol: float,
        slippage_decimal: float, # Expect decimal e.g. 0.25
        max_retries: int,
        confirm_timeout: int,
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.amount_sol = amount_sol
        # Use imported constant
        self.amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        self.slippage_decimal = slippage_decimal
        self.max_retries = max_retries
        self.confirm_timeout = confirm_timeout
        logger.info(f"TokenBuyer initialized with SOL amount: {self.amount_sol:.6f}, Slippage: {self.slippage_decimal * 100:.2f}%")


    async def execute(self, token_info: TokenInfo) -> TradeResult:
        """Executes the buy transaction for the specified token."""
        retry_count = 0
        error_message = "Max retries reached"
        initial_sol_reserves: Optional[int] = None

        # --- Get dynamic addresses ---
        user_pubkey = self.wallet.pubkey
        mint_pubkey = token_info.mint
        bonding_curve_pubkey = token_info.bonding_curve
        curve_details = await self.curve_manager.get_bonding_curve_details(bonding_curve_pubkey)
        if not curve_details:
             logger.error(f"BUYER: Failed get curve details {mint_pubkey}.")
             return TradeResult(success=False, error_message="Failed get curve details")
        curve_state_for_check, associated_bonding_curve_pubkey = curve_details
        user_token_account = Wallet.get_associated_token_address(user_pubkey, mint_pubkey)
        # --- End Get ---

        while retry_count < self.max_retries:
            retry_count += 1
            logger.info(f"Buy attempt {retry_count}/{self.max_retries} for {token_info.symbol} ({mint_pubkey})")

            try:
                # 1. Get current curve state
                curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(bonding_curve_pubkey)
                if not curve_state:
                    error_message = "Failed fetch bonding curve state."; logger.warning(f"{error_message} Retry..."); await asyncio.sleep(2); continue

                if retry_count == 1 and curve_state_for_check: initial_sol_reserves = curve_state_for_check.virtual_sol_reserves
                else: initial_sol_reserves = curve_state.virtual_sol_reserves

                # 2. Calculate amounts
                try:
                     est_tokens_out = curve_state.calculate_tokens_out_for_sol(self.amount_lamports)
                     min_tokens_out = int(est_tokens_out * (1 - self.slippage_decimal))
                     max_sol_cost_lamports = self.amount_lamports
                     logger.debug(f"Est tokens out: {est_tokens_out}, Min tokens out: {min_tokens_out}, Max SOL cost: {max_sol_cost_lamports}")
                     if min_tokens_out <= 0: error_message = f"Min tokens out <= 0 ({min_tokens_out})."; logger.error(error_message); break
                except (AttributeError, ValueError, TypeError) as calc_e:
                     error_message = f"Error buy calc: {calc_e}"; logger.error(f"{error_message}. Retry..."); await asyncio.sleep(2); continue

                # 3. Get priority fees
                try: compute_unit_price_micro_lamports = await self.priority_fee_manager.get_fee() # Use assumed name 'get_fee'
                except AttributeError: logger.error("FATAL: PriorityFeeManager missing get_fee()!"); raise
                except Exception as fee_e: logger.error(f"Error getting priority fee: {fee_e}. Using 0."); compute_unit_price_micro_lamports = 0
                compute_unit_limit = 200_000 # Placeholder - ADJUST

                # 4. Build instructions
                instructions = [ set_compute_unit_limit(compute_unit_limit), set_compute_unit_price(compute_unit_price_micro_lamports) ]
                ata_instructions = InstructionBuilder.get_create_ata_instruction(payer_pubkey=user_pubkey, owner=user_pubkey, mint=mint_pubkey)
                instructions.extend(ata_instructions)
                buy_instruction = InstructionBuilder.get_pump_buy_instruction(
                    user_pubkey=user_pubkey, mint_pubkey=mint_pubkey, bonding_curve_pubkey=bonding_curve_pubkey,
                    associated_bonding_curve_pubkey=associated_bonding_curve_pubkey, user_token_account=user_token_account,
                    amount_lamports=max_sol_cost_lamports, min_token_output=min_tokens_out )
                if buy_instruction is None: error_message = "Failed build pump buy ix."; logger.error(error_message); break
                instructions.append(buy_instruction)

                # 5. Build and send transaction
                tx_result: TransactionResult = await build_and_send_transaction(
                    client=self.client, payer=self.wallet.payer, instructions=instructions, label=f"Buy_{token_info.symbol[:5]}" )

                # 6. Process result
                if tx_result.success and tx_result.signature:
                    logger.info(f"Buy tx sent: {tx_result.signature}. Confirming...")
                    try: signature_obj = Signature.from_string(tx_result.signature)
                    except ValueError: logger.error(f"Invalid sig format: {tx_result.signature}"); error_message = "Invalid sig format"; break
                    confirmation_status = await self.client.confirm_transaction(signature_obj, timeout_secs=self.confirm_timeout)

                    if confirmation_status == TransactionConfirmationStatus.Confirmed or confirmation_status == TransactionConfirmationStatus.Finalized:
                         logger.info(f"Buy tx CONFIRMED: {tx_result.signature}")
                         token_decimals = DEFAULT_TOKEN_DECIMALS # Use imported constant
                         amount_tokens_bought = est_tokens_out / (10**token_decimals) if est_tokens_out > 0 else 0.0
                         buy_price_sol_per_token = self.amount_sol / amount_tokens_bought if amount_tokens_bought > 0 else 0.0
                         return TradeResult( success=True, tx_signature=tx_result.signature, price=buy_price_sol_per_token, amount=amount_tokens_bought, initial_sol_liquidity=initial_sol_reserves )
                    else: status_str = str(confirmation_status) if confirmation_status else "Unknown/Timeout"; error_message = f"Buy tx {tx_result.signature} confirm failed. Status: {status_str}"; logger.warning(error_message)
                elif tx_result.error_type: error_message = f"Buy tx failed ({tx_result.error_type}): {tx_result.error_message or 'No details'}"; logger.warning(f"{error_message}. Retry..."); await asyncio.sleep(3)
                else: error_message = "Buy tx send failed unknown error."; logger.error(error_message); await asyncio.sleep(3)

            except Exception as e:
                error_message = f"Unexpected error buy attempt {retry_count}: {e}"; logger.error(error_message, exc_info=True); await asyncio.sleep(5)

        # Loop finished without success
        logger.error(f"Failed buy {token_info.symbol} after {self.max_retries} attempts: {error_message}")
        return TradeResult( success=False, error_message=error_message, initial_sol_liquidity=initial_sol_reserves )