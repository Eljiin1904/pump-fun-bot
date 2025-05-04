# src/trading/seller.py

import asyncio
from typing import Optional, Any, List, Sequence # Added Sequence

from solders.pubkey import Pubkey

from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.signature import Signature
from solders.instruction import Instruction
# --- FIX: Use Solders commitment type ---
from solders.commitment_config import CommitmentLevel
from solders.transaction_status import TransactionConfirmationStatus
# --- END FIX ---
from solana.rpc.commitment import Confirmed # Keep for balance check if needed by client helper

# --- Project Imports ---
from ..core.client import SolanaClient
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
# --- FIX: Use Solders CommitmentLevel ---
DEFAULT_SELL_COMMITMENT = CommitmentLevel.Confirmed # Use Confirmed for sells
# --- END FIX ---


class TokenSeller:
    """ Handles the logic for selling tokens back to the pump.fun bonding curve. """

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        slippage: float, # Decimal, e.g. 0.002 for 0.2%
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

    # --- Helper to get balance (assuming client doesn't have it) ---
    async def _get_balance_lamports(self, token_account_pubkey: Pubkey) -> Optional[int]:
        """ Safely fetches token balance in lamports. """
        try:
            # Use Confirmed commitment for balance check before selling
            balance_resp = await self.client.get_token_account_balance(token_account_pubkey, commitment=Confirmed)
            if balance_resp and hasattr(balance_resp, 'amount'):
                return int(balance_resp.amount)
            else:
                logger.warning(f"Could not get valid balance object for {token_account_pubkey}.")
                return None # Indicate failure to get balance object
        except Exception as e:
            logger.error(f"Error fetching balance for {token_account_pubkey}: {e}", exc_info=True)
            return None # Indicate error during fetch

    async def execute(self, token_info: TokenInfo, buyer_result: Optional[TradeResult] = None) -> TradeResult:
        """ Attempts to sell the user's entire balance of the specified token. """
        mint_str = str(token_info.mint)
        logger.info(f"Attempting sell for {token_info.symbol} ({mint_str[:6]})...")

        user_pubkey = self.wallet.pubkey
        mint_pubkey = token_info.mint
        user_token_account = Wallet.get_associated_token_address(user_pubkey, mint_pubkey)

        # --- 1. Get Current Balance ---
        amount_to_sell_lamports = await self._get_balance_lamports(user_token_account)

        if amount_to_sell_lamports is None:
            # Error logged in _get_balance_lamports
            return TradeResult(success=False, error_message="Failed to fetch token balance", error_type="BalanceError")
        if amount_to_sell_lamports <= 0:
            logger.warning(f"Sell {token_info.symbol}: Balance is zero. Cannot sell.")
            return TradeResult(success=False, error_message="Zero balance", amount=0, error_type="BalanceError")

        logger.info(f"Attempting to sell {amount_to_sell_lamports} lamports of {token_info.symbol}.")

        # --- Sell Retry Loop ---
        retry_count = 0
        error_message = "Max sell retries reached"
        last_exception: Optional[Exception] = None

        while retry_count < self.max_retries:
            retry_count += 1
            logger.info(f"Sell attempt {retry_count}/{self.max_retries} for {token_info.symbol}")
            last_exception = None # Reset for this attempt

            try:
                # --- 2. Derive PDAs ---
                try:
                    bonding_curve_pubkey, _ = BondingCurveManager.find_bonding_curve_pda(mint_pubkey)
                    associated_bonding_curve_pubkey, _ = BondingCurveManager.find_associated_bonding_curve_pda(mint_pubkey)
                    logger.debug(f"Sell using Curve PDA: {bonding_curve_pubkey}, Vault PDA: {associated_bonding_curve_pubkey}")
                except Exception as pda_e:
                    logger.error(f"SELLER: Failed PDA derivation {mint_pubkey}: {pda_e}")
                    error_message = "Failed PDA derivation during sell"; break # Unrecoverable

                # --- 3. Get Curve State ---
                # --- FIX: Use correct commitment type ---
                curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(
                    bonding_curve_pubkey,
                    commitment=DEFAULT_SELL_COMMITMENT # Use Confirmed
                    # Or pass None: commitment=None
                )
                # --- END FIX ---
                if not curve_state:
                    logger.warning(f"Sell attempt {retry_count}: Could not get curve state. Retrying after delay...")
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                # --- 4. Calculate Min Output ---
                est_sol_out_lamports: int = 0
                min_sol_output_lamports: int = 0
                try:
                    est_sol_out_lamports = curve_state.calculate_sol_out_for_tokens(amount_to_sell_lamports)
                    min_sol_output_lamports = int(est_sol_out_lamports * (1 - self.slippage_decimal))
                    min_sol_output_lamports = max(0, min_sol_output_lamports) # Ensure non-negative
                    logger.debug(f"Est SOL Out: {est_sol_out_lamports}, MinSOLOut (Slippage): {min_sol_output_lamports}")
                    # Check if estimated output is reasonable (e.g., not negative if curve depleted)
                    if est_sol_out_lamports < 0:
                         logger.warning(f"Sell attempt {retry_count}: Estimated SOL out is negative. Curve may be depleted.")
                         error_message = "Sell failed: Negative estimated SOL output."
                         break # Exit loop if calculation seems invalid
                except Exception as calc_e:
                    logger.error(f"Error calculating sell output for {token_info.symbol}: {calc_e}", exc_info=True)
                    error_message = "Sell calculation error"
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                # --- 5. Calculate Priority Fee ---
                compute_unit_price_micro_lamports = 0
                # --- FIX: Define target accounts for seller fee ---
                priority_fee_target_accounts: List[Pubkey] = [
                    mint_pubkey,
                    bonding_curve_pubkey,
                    associated_bonding_curve_pubkey,
                    user_token_account,
                ]
                # --- END FIX ---
                try:
                    # --- FIX: Pass target accounts ---
                    fee = await self.priority_fee_manager.calculate_priority_fee(accounts=priority_fee_target_accounts)
                    # --- END FIX ---
                    if fee is not None and fee > 0:
                        compute_unit_price_micro_lamports = int(fee)
                        logger.info(f"Using priority fee for sell: {compute_unit_price_micro_lamports} micro-lamports")
                    elif fee == 0: logger.info("Priority Fee Manager returned 0 for sell. Using 0.")
                    else: logger.warning("Priority Fee Manager returned None for sell. Using 0 micro-lamports.")
                except AttributeError as fee_attr_err: logger.error(f"PriorityFeeManager missing attribute/method? {fee_attr_err}. Using 0 fee.", exc_info=True)
                except Exception as fee_e: logger.error(f"Error calculating priority fee for sell: {fee_e}. Using 0.", exc_info=True)

                # --- 6. Build Instructions ---
                instructions: List[Instruction] = []
                instructions.append(set_compute_unit_limit(self.compute_unit_limit))
                if compute_unit_price_micro_lamports > 0:
                     instructions.append(set_compute_unit_price(compute_unit_price_micro_lamports))

                sell_instruction = InstructionBuilder.get_pump_sell_instruction(
                    user_pubkey=user_pubkey, mint_pubkey=mint_pubkey,
                    bonding_curve_pubkey=bonding_curve_pubkey,
                    associated_bonding_curve_pubkey=associated_bonding_curve_pubkey,
                    user_token_account=user_token_account,
                    token_amount=amount_to_sell_lamports,
                    min_sol_output=min_sol_output_lamports
                )
                if sell_instruction is None:
                    error_message = "Failed to build sell instruction. Stopping retries."; logger.error(error_message); break
                instructions.append(sell_instruction)

                # --- 7. Send and Confirm ---
                tx_result: TransactionResult = await build_and_send_transaction(
                    client=self.client,
                    payer=self.wallet.payer,
                    instructions=instructions,
                    label=f"Sell_{token_info.symbol[:5]}",
                    confirm=True,
                    confirm_timeout_secs=self.confirm_timeout,
                    confirm_commitment="confirmed" # Confirm sells to 'confirmed'
                )

                # --- 8. Process Result ---
                if tx_result.success and tx_result.signature:
                    logger.info(f"Sell tx CONFIRMED: {tx_result.signature}")
                    sell_price_sol_per_token_estimate = 0.0
                    token_decimals = token_info.decimals if token_info.decimals is not None else DEFAULT_TOKEN_DECIMALS
                    # Calculate estimated price based on estimated SOL out
                    if amount_to_sell_lamports > 0 and token_decimals is not None:
                         try:
                             amount_ui = amount_to_sell_lamports / (10**token_decimals)
                             if amount_ui > 0:
                                 sell_price_sol_per_token_estimate = (est_sol_out_lamports / LAMPORTS_PER_SOL) / amount_ui
                         except ZeroDivisionError: pass
                    return TradeResult(
                        success=True,
                        tx_signature=tx_result.signature,
                        price=sell_price_sol_per_token_estimate,
                        amount=(est_sol_out_lamports / LAMPORTS_PER_SOL), # Log estimated SOL received
                        initial_sol_liquidity=None # Not relevant for sell result
                    )
                else: # Send or confirm failed
                    error_message = f"Sell tx failed ({tx_result.error_type}): {tx_result.error_message or 'No specific message'}"
                    logger.warning(f"{error_message}. Retrying attempt {retry_count+1}...")
                    last_exception = RuntimeError(error_message)
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + retry_count); continue # Delay and retry

            except Exception as e:
                last_exception = e
                error_message = f"Unexpected error during sell attempt {retry_count}: {e}"
                logger.error(error_message, exc_info=True)
                if "Failed to build sell instruction" in str(e): break # Unrecoverable build error
                # Delay before next attempt
                if retry_count < self.max_retries:
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + retry_count * 2) # Longer backoff
                # continue is implicit

        # --- End of Retry Loop ---
        logger.error(f"Failed sell {token_info.symbol} after {self.max_retries} attempts. Last error: {error_message}")
        return TradeResult(success=False, error_message=error_message, amount=0.0, price=0.0, error_type="MaxRetriesReached" if not last_exception else type(last_exception).__name__)