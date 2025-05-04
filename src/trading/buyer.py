# src/trading/buyer.py
import asyncio
from typing import Optional, Any, List, Sequence

from solders.pubkey import Pubkey
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.signature import Signature
from solders.instruction import Instruction
from solders.commitment_config import CommitmentLevel
from solana.rpc.commitment import Processed, Confirmed
from solana.exceptions import SolanaRpcException

# --- Project Imports ---
from ..core.client import SolanaClient
from ..core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL, DEFAULT_TOKEN_DECIMALS
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.wallet import Wallet
from ..core.transactions import build_and_send_transaction, TransactionResult
from ..core.instruction_builder import InstructionBuilder
# --- FIX: base import needed for TradeResult ---
from ..trading.base import TokenInfo, TradeResult
# --- END FIX ---
from ..utils.logger import get_logger

# Initialize logger at module level
# logger = get_logger(__name__) # Keep if preferred for static methods

FETCH_RETRY_DELAY_SECONDS = 4.0
PREFETCH_RETRY_DELAY_SECONDS = 1.5
MAX_PREFETCH_RETRIES = 5
DEFAULT_COMPUTE_UNIT_LIMIT = 200_000
DEFAULT_PREFETCH_COMMITMENT = CommitmentLevel.Processed


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
        confirm_timeout: int,
        compute_unit_limit: int = DEFAULT_COMPUTE_UNIT_LIMIT
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.amount_sol = amount_sol
        self.amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        self.slippage_decimal = slippage_decimal
        self.max_retries = max_retries
        self.confirm_timeout = confirm_timeout
        self.compute_unit_limit = compute_unit_limit
        # --- FIX: Initialize logger for the instance ---
        self.logger = get_logger(__name__)
        # --- END FIX ---
        self.logger.info(f"TokenBuyer Init: Amt={self.amount_sol:.6f}, Slip={self.slippage_decimal * 100:.2f}%, MaxRetries={self.max_retries}, CULimit={self.compute_unit_limit}")


    async def _prefetch_curve_state(self, bonding_curve_pubkey: Pubkey, token_symbol: str) -> Optional[BondingCurveState]:
        """ Fetches the curve state with retries before the buy attempt. """
        for attempt in range(MAX_PREFETCH_RETRIES):
            self.logger.debug(f"Prefetch attempt {attempt + 1}/{MAX_PREFETCH_RETRIES} for {token_symbol} curve state...")
            try:
                # --- Pass correct commitment type (enum or None) ---
                curve_state = await self.curve_manager.get_curve_state(
                    bonding_curve_pubkey,
                    commitment=DEFAULT_PREFETCH_COMMITMENT
                )
                if curve_state:
                    self.logger.info(f"Prefetch successful for {token_symbol} curve state.")
                    return curve_state
                else:
                     self.logger.warning(f"Prefetch attempt {attempt+1} failed: get_curve_state returned None.")
            except Exception as prefetch_e:
                # Log the specific error from get_curve_state if it raised one
                self.logger.warning(f"Prefetch attempt {attempt + 1} failed with exception: {prefetch_e}", exc_info=True)

            # Delay before next prefetch attempt
            if attempt < MAX_PREFETCH_RETRIES - 1:
                self.logger.debug(f"Prefetch attempt {attempt + 1} unsuccessful, sleeping for {PREFETCH_RETRY_DELAY_SECONDS}s...")
                await asyncio.sleep(PREFETCH_RETRY_DELAY_SECONDS)

        self.logger.warning(f"Prefetch failed for {token_symbol} curve state after {MAX_PREFETCH_RETRIES} attempts.")
        return None


    async def execute(self, token_info: TokenInfo) -> TradeResult:
        """ Executes the token buy process with retries. """
        # --- Use self.logger throughout this method ---
        self.logger.info(f"Executing buy for {token_info.symbol} ({token_info.mint})")
        retry_count = 0
        initial_sol_reserves: Optional[int] = None
        error_message: str = f"Buy failed for {token_info.symbol} after max retries"
        last_exception: Optional[Exception] = None
        error_type_str = "MaxRetriesReached" # Default error type if loop finishes

        user_pubkey = self.wallet.pubkey
        mint_pubkey = token_info.mint

        try:
            bonding_curve_pubkey, _ = BondingCurveManager.find_bonding_curve_pda(mint_pubkey)
            associated_bonding_curve_pubkey, _ = BondingCurveManager.find_associated_bonding_curve_pda(mint_pubkey)
        except Exception as pda_e:
            self.logger.error(f"BUYER: Failed PDA derivation {mint_pubkey}: {pda_e}")
            # Pass error_type correctly
            return TradeResult(success=False, error_message="Failed PDA derivation", error_type="BuildError")

        user_token_account = Wallet.get_associated_token_address(user_pubkey, mint_pubkey)

        priority_fee_target_accounts: List[Pubkey] = [
            mint_pubkey, bonding_curve_pubkey, associated_bonding_curve_pubkey,
            user_token_account,
        ]

        # Prefetch state before entering retry loop
        initial_curve_state = await self._prefetch_curve_state(bonding_curve_pubkey, token_info.symbol)
        if not initial_curve_state:
            # Pass error_type correctly
            return TradeResult(success=False, error_message="Failed to fetch bonding curve state during prefetch.", error_type="PrefetchError")
        initial_sol_reserves = initial_curve_state.virtual_sol_reserves

        # --- Buy Retry Loop ---
        while retry_count < self.max_retries:
            retry_count += 1
            self.logger.info(f"Buy attempt {retry_count}/{self.max_retries} for {token_info.symbol} ({mint_pubkey})")
            last_exception = None

            try:
                if retry_count == 1: curve_state = initial_curve_state
                else: curve_state = await self.curve_manager.get_curve_state(bonding_curve_pubkey, commitment=CommitmentLevel.Confirmed)

                if not curve_state:
                    error_message = "Failed fetch curve state retry."; self.logger.warning(f"{error_message} Retrying...");
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                est_tokens_out = curve_state.calculate_tokens_out_for_sol(self.amount_lamports)
                min_tokens_out_for_slippage = int(est_tokens_out * (1 - self.slippage_decimal))
                min_token_output_for_ix = max(0, min_tokens_out_for_slippage)
                self.logger.debug(f"Est TknOut: {est_tokens_out}, MinTknOut (Slippage): {min_token_output_for_ix}, MaxSOLIn: {self.amount_lamports}")

                if min_token_output_for_ix <= 0 and est_tokens_out <= 0:
                    error_message = "Est tokens out <= 0."; self.logger.warning(error_message);
                    if est_tokens_out <= 0: error_message = "Buy failed: Est tokens out zero."; break
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                compute_unit_price_micro_lamports = 0
                try:
                    fee = await self.priority_fee_manager.calculate_priority_fee(accounts=priority_fee_target_accounts)
                    if fee is not None and fee > 0: compute_unit_price_micro_lamports = int(fee); self.logger.info(f"Using priority fee: {compute_unit_price_micro_lamports} micro-lamports")
                    elif fee == 0: self.logger.info("Priority Fee Manager returned 0.")
                    else: self.logger.warning("Priority Fee Manager returned None. Using 0.")
                except AttributeError as fee_attr_err: self.logger.error(f"PriorityFeeManager missing method? {fee_attr_err}. Using 0 fee.", exc_info=True)
                except Exception as fee_e: self.logger.error(f"Error calculating priority fee: {fee_e}. Using 0.", exc_info=True)

                instructions: List[Instruction] = []
                instructions.append(set_compute_unit_limit(self.compute_unit_limit))
                if compute_unit_price_micro_lamports > 0: instructions.append(set_compute_unit_price(compute_unit_price_micro_lamports))
                ata_instructions: List[Instruction] = await InstructionBuilder.get_create_ata_instruction_if_needed( payer_pubkey=user_pubkey, owner=user_pubkey, mint=mint_pubkey, client=self.client.get_async_client() ); instructions.extend(ata_instructions)
                buy_instruction = InstructionBuilder.get_pump_buy_instruction( user_pubkey=user_pubkey, mint_pubkey=mint_pubkey, bonding_curve_pubkey=bonding_curve_pubkey, associated_bonding_curve_pubkey=associated_bonding_curve_pubkey, user_token_account=user_token_account, amount_lamports=self.amount_lamports, min_token_output=min_token_output_for_ix )
                if buy_instruction is None: error_message = "Failed build buy instruction"; self.logger.error(error_message + ". Stopping."); break; instructions.append(buy_instruction)

                tx_result: TransactionResult = await build_and_send_transaction( client=self.client, payer=self.wallet.payer, instructions=instructions, label=f"Buy_{token_info.symbol[:5]}", confirm=True, confirm_timeout_secs=self.confirm_timeout, confirm_commitment="confirmed", max_retries_sending=1 )

                if tx_result.success and tx_result.signature:
                    self.logger.info(f"Buy tx CONFIRMED: {tx_result.signature}")
                    est_tokens_out_initial = initial_curve_state.calculate_tokens_out_for_sol(self.amount_lamports); amount_tokens_bought_estimate_ui = 0.0; buy_price_sol_per_token_estimate = 0.0;
                    token_decimals = token_info.decimals if token_info.decimals is not None else DEFAULT_TOKEN_DECIMALS
                    if est_tokens_out_initial > 0: amount_tokens_bought_estimate_ui = est_tokens_out_initial / (10 ** token_decimals)
                    if amount_tokens_bought_estimate_ui > 0: buy_price_sol_per_token_estimate = self.amount_sol / amount_tokens_bought_estimate_ui
                    # Ensure error_type is None on success
                    return TradeResult( success=True, tx_signature=tx_result.signature, price=buy_price_sol_per_token_estimate, amount=amount_tokens_bought_estimate_ui, initial_sol_liquidity=initial_sol_reserves, error_type=None )
                else:
                    error_message = f"Buy tx failed ({tx_result.error_type}): {tx_result.error_message or 'No message'}"; self.logger.warning(f"{error_message}. Retrying attempt {retry_count+1}..."); last_exception = RuntimeError(error_message); error_type_str = tx_result.error_type or "SendConfirmError"; await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + retry_count); continue

            except Exception as loop_exception:
                last_exception = loop_exception; error_message = f"Unexpected error buy attempt {retry_count}: {loop_exception}"; self.logger.error(error_message, exc_info=True); error_type_str = type(loop_exception).__name__
                if "Failed to build buy instruction" in str(loop_exception): break
                if retry_count < self.max_retries: await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + retry_count * 2)

        self.logger.error(f"Failed buy {token_info.symbol} after {retry_count}/{self.max_retries} attempts: {error_message}")
        # Use error_type when returning failure
        if last_exception and not error_type_str: error_type_str = type(last_exception).__name__
        # Pass error_type correctly
        return TradeResult(success=False, error_message=error_message, initial_sol_liquidity=initial_sol_reserves, error_type=error_type_str)