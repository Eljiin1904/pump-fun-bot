# src/trading/seller.py

import asyncio
from typing import Optional

# --- Solana/Solders Imports ---
from solders.pubkey import Pubkey
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.transaction_status import TransactionConfirmationStatus
# --- Add Signature import ---
from solders.signature import Signature
# --- Add Commitment Enum ---
from solana.rpc.commitment import Confirmed, Commitment # Keep both if commitment flexibility needed
from solana.rpc.types import TokenAmount # Keep for type hint

from ..core.client import SolanaClient
# --- Import Constants FROM curve.py ---
from ..core.curve import (
    BondingCurveManager,
    BondingCurveState,
    LAMPORTS_PER_SOL,
    DEFAULT_TOKEN_DECIMALS # Assuming token uses 6 decimals unless specified otherwise
)
# --- End Import ---
from ..core.priority_fee.manager import PriorityFeeManager # Verify method name get_fee()
from ..core.wallet import Wallet
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..core.instruction_builder import InstructionBuilder
from ..trading.base import TokenInfo, TradeResult
from ..utils.logger import get_logger


logger = get_logger(__name__)
# LAMPORTS_PER_SOL defined via import

class TokenSeller:
    """Handles the token selling process on the bonding curve."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        slippage: float, # Expecting decimal e.g. 0.02
        max_retries: int,
        confirm_timeout: int, # Ensure stored
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.slippage_decimal = slippage
        self.max_retries = max_retries
        self.confirm_timeout = confirm_timeout # Store it
        logger.info(f"TokenSeller (Bonding Curve) initialized with Slippage: {self.slippage_decimal * 100:.2f}%")


    async def execute(self, token_info: TokenInfo, buyer_result: TradeResult) -> TradeResult:
        """Executes the sell transaction for the specified token on the bonding curve."""
        retry_count = 0
        error_message = "Max retries reached"

        # --- Get dynamic/derived addresses needed for instructions ---
        user_pubkey = self.wallet.pubkey
        mint_pubkey = token_info.mint
        bonding_curve_pubkey = token_info.bonding_curve # From TokenInfo
        user_token_account = Wallet.get_associated_token_address(user_pubkey, mint_pubkey)

        # Get associated vault - CRITICAL: Assumes curve.py get_curve_details works
        # Need curve_manager instance here
        curve_details = await self.curve_manager.get_bonding_curve_details(bonding_curve_pubkey)
        if not curve_details:
             logger.error(f"SELLER: Failed get curve details {mint_pubkey}. Cannot determine SOL vault.")
             return TradeResult(success=False, error_message="Failed get curve details for sell")
        # We only need the vault address for the instruction, not the full state here
        _curve_state_for_check, associated_bonding_curve_pubkey = curve_details
        # --- End Get ---


        while retry_count < self.max_retries:
            retry_count += 1
            logger.info(f"Sell attempt {retry_count}/{self.max_retries} for {token_info.symbol} ({mint_pubkey})")

            try:
                # 1. Get user's current token balance
                logger.debug(f"Fetching token balance for ATA: {user_token_account}")
                balance_response: Optional[TokenAmount] = await self.client.get_token_account_balance(
                    user_token_account, commitment=Confirmed # Use Enum
                )

                token_balance_lamports = 0
                token_balance_ui = 0.0
                token_decimals = DEFAULT_TOKEN_DECIMALS # Use imported default

                if balance_response is not None and hasattr(balance_response, 'amount') and isinstance(balance_response.amount, str) and balance_response.amount.isdigit():
                    token_balance_lamports = int(balance_response.amount)
                    if hasattr(balance_response, 'decimals'):
                        token_decimals = balance_response.decimals
                    # --- Calculate UI amount safely ---
                    token_balance_ui = token_balance_lamports / (10**token_decimals)
                else:
                    error_message = f"Could not get valid token balance for {user_token_account}."
                    logger.warning(error_message) # Log as warning, might be zero balance
                    # Check if account exists at all
                    acc_info_res = await self.client.get_account_info(user_token_account, commitment=Confirmed)
                    if acc_info_res is None or acc_info_res.value is None:
                         logger.info(f"ATA {user_token_account} does not exist. Nothing to sell.")
                         return TradeResult(success=True, error_message="Account does not exist")
                    # If account exists but balance fetch failed or was non-digit, assume 0 for safety?
                    # Or retry? Let's assume 0 for now if response was bad.
                    token_balance_lamports = 0
                    token_balance_ui = 0.0

                logger.info(f"Current balance {token_info.symbol}: {token_balance_ui} ({token_balance_lamports} lamports)")
                if token_balance_lamports <= 0:
                    error_message = f"Zero balance found for {token_info.symbol}. Cannot sell."
                    logger.warning(error_message)
                    return TradeResult(success=True, error_message="Zero balance to sell") # Consider success

                # 2. Get current curve state for calculation
                curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(bonding_curve_pubkey)
                if not curve_state:
                    error_message = "Failed fetch bonding curve state for sell."; logger.warning(f"{error_message} Retry..."); await asyncio.sleep(2); continue

                # 3. Calculate expected SOL out and slippage
                try:
                     est_sol_out_lamports = curve_state.calculate_sol_out_for_tokens(token_balance_lamports)
                     min_sol_out_lamports = int(est_sol_out_lamports * (1 - self.slippage_decimal))
                     logger.debug(f"Selling {token_balance_lamports}. Est SOL out: {est_sol_out_lamports}, Min SOL out: {min_sol_out_lamports}")
                     if min_sol_out_lamports < 0: min_sol_out_lamports = 0
                except (AttributeError, ValueError, TypeError) as calc_e:
                     error_message = f"Error sell calculation: {calc_e}"; logger.error(f"{error_message}. Retry..."); await asyncio.sleep(2); continue

                # 4. Get priority fees
                try:
                    # --- Use Correct Method Name (Assuming get_fee exists) ---
                    compute_unit_price_micro_lamports = await self.priority_fee_manager.get_fee() # ADJUST IF NEEDED
                except AttributeError: logger.error("FATAL: PriorityFeeManager missing get_fee()!"); raise
                except Exception as fee_e: logger.error(f"Error getting priority fee: {fee_e}. Using 0."); compute_unit_price_micro_lamports = 0
                compute_unit_limit = 200_000 # Placeholder - ADJUST

                # 5. Build instructions
                instructions = [ set_compute_unit_limit(compute_unit_limit), set_compute_unit_price(compute_unit_price_micro_lamports) ]
                # --- Corrected call arguments ---
                sell_instruction = InstructionBuilder.get_pump_sell_instruction(
                    user_pubkey=user_pubkey,
                    mint_pubkey=mint_pubkey,
                    bonding_curve_pubkey=bonding_curve_pubkey,
                    associated_bonding_curve_pubkey=associated_bonding_curve_pubkey, # Use derived vault key
                    user_token_account=user_token_account, # Use derived user ATA
                    token_amount=token_balance_lamports, # Sell entire balance
                    min_sol_output=min_sol_out_lamports
                )
                # --- End Corrected ---
                if sell_instruction is None: error_message = "Failed build pump sell ix."; logger.error(error_message); break
                instructions.append(sell_instruction)

                # 6. Build and send transaction
                tx_result: TransactionResult = await build_and_send_transaction(
                    client=self.client, payer=self.wallet.payer, instructions=instructions, label=f"Sell_{token_info.symbol[:5]}" )

                # 7. Process result
                if tx_result.success and tx_result.signature:
                    logger.info(f"Sell tx sent: {tx_result.signature}. Confirming...")
                    # --- Convert signature string to Signature object ---
                    try: signature_obj = Signature.from_string(tx_result.signature)
                    except ValueError: logger.error(f"Invalid sig format: {tx_result.signature}"); error_message = "Invalid sig format"; break
                    confirmation_status = await self.client.confirm_transaction(
                         signature_obj, # Pass Signature object
                         timeout_secs=self.confirm_timeout, # Use stored timeout
                         commitment=Confirmed # Use Enum
                    )
                    # --- End Convert ---

                    # --- Use str() for status name ---
                    status_str = str(confirmation_status) if confirmation_status else "Unknown/Timeout"
                    # --- End Use str() ---

                    if confirmation_status == TransactionConfirmationStatus.Confirmed or confirmation_status == TransactionConfirmationStatus.Finalized:
                         logger.info(f"Sell tx CONFIRMED: {tx_result.signature}")
                         # Approx calcs
                         sell_price_sol_per_token = (est_sol_out_lamports / LAMPORTS_PER_SOL) / token_balance_ui if token_balance_ui > 0 else 0
                         amount_tokens_sold = token_balance_ui
                         return TradeResult( success=True, tx_signature=tx_result.signature, price=sell_price_sol_per_token, amount=amount_tokens_sold )
                    else:
                         error_message = f"Sell tx {tx_result.signature} confirm failed. Status: {status_str}"; logger.warning(error_message) # Retry loop handles next attempt

                elif tx_result.error_type: error_message = f"Sell tx failed ({tx_result.error_type}): {tx_result.error_message or 'No details'}"; logger.warning(f"{error_message}. Retry..."); await asyncio.sleep(3)
                else: error_message = "Sell tx send failed unknown error."; logger.error(error_message); await asyncio.sleep(3)

            except Exception as e:
                error_message = f"Unexpected error sell attempt {retry_count}: {e}"; logger.error(error_message, exc_info=True); await asyncio.sleep(5)

        # Loop finished without success
        logger.error(f"Failed sell {token_info.symbol} after {self.max_retries} attempts: {error_message}")
        return TradeResult(success=False, error_message=error_message)