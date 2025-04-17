# src/trading/buyer.py
import asyncio
from typing import Optional, Any, List, Sequence

from solders.pubkey import Pubkey
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.signature import Signature
# Import Instruction type hint
from solders.instruction import Instruction
from solders.transaction_status import TransactionConfirmationStatus
from solana.rpc.commitment import Confirmed, Processed

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
PREFETCH_RETRY_DELAY_SECONDS = 3.0
MAX_PREFETCH_RETRIES = 5
DEFAULT_COMPUTE_UNIT_LIMIT = 200_000

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
        logger.info(f"TokenBuyer Init: Amt={self.amount_sol:.6f}, Slip={self.slippage_decimal * 100:.2f}%, MaxRetries={self.max_retries}, CULimit={self.compute_unit_limit}")

    async def _prefetch_curve_state(self, bonding_curve_pubkey: Pubkey, token_symbol: str) -> Optional[BondingCurveState]:
        # ... (prefetch code remains the same) ...
        for attempt in range(MAX_PREFETCH_RETRIES):
            logger.debug(f"Prefetch attempt {attempt + 1}/{MAX_PREFETCH_RETRIES} for {token_symbol} curve state...")
            try:
                curve_state = await self.curve_manager.get_curve_state(bonding_curve_pubkey, commitment=Processed)
                if curve_state:
                    logger.info(f"Prefetch successful for {token_symbol} curve state.")
                    return curve_state
            except Exception as prefetch_e:
                logger.warning(f"Prefetch attempt {attempt + 1} failed: {prefetch_e}")
            if attempt < MAX_PREFETCH_RETRIES - 1:
                logger.debug(f"Prefetch attempt {attempt + 1} unsuccessful, sleeping for {PREFETCH_RETRY_DELAY_SECONDS}s...")
                await asyncio.sleep(PREFETCH_RETRY_DELAY_SECONDS)
        logger.warning(f"Prefetch failed for {token_symbol} curve state after {MAX_PREFETCH_RETRIES} attempts.")
        return None


    async def execute(self, token_info: TokenInfo) -> TradeResult:
        retry_count = 0
        initial_sol_reserves: Optional[int] = None
        error_message: str = f"Buy failed for {token_info.symbol} after max retries"
        last_exception: Optional[Exception] = None

        user_pubkey = self.wallet.pubkey
        mint_pubkey = token_info.mint
        try:
            bonding_curve_pubkey, _ = BondingCurveManager.find_bonding_curve_pda(mint_pubkey)
            associated_bonding_curve_pubkey, _ = BondingCurveManager.find_associated_bonding_curve_pda(mint_pubkey)
        except Exception as pda_e:
            logger.error(f"BUYER: Failed PDA derivation {mint_pubkey}: {pda_e}")
            return TradeResult(success=False, error_message="Failed PDA derivation")

        user_token_account = Wallet.get_associated_token_address(user_pubkey, mint_pubkey)

        priority_fee_target_accounts: Sequence[Pubkey] = [
            mint_pubkey,
            bonding_curve_pubkey,
            associated_bonding_curve_pubkey,
            user_token_account,
        ]

        initial_curve_state = await self._prefetch_curve_state(bonding_curve_pubkey, token_info.symbol)
        if not initial_curve_state:
            return TradeResult(success=False, error_message="Failed to fetch bonding curve state during prefetch.")
        initial_sol_reserves = initial_curve_state.virtual_sol_reserves

        while retry_count < self.max_retries:
            retry_count += 1
            logger.info(f"Buy attempt {retry_count}/{self.max_retries} for {token_info.symbol} ({mint_pubkey})")
            buy_instruction = None
            last_exception = None

            try:
                # Fetch the curve state
                if retry_count == 1: curve_state = initial_curve_state
                else: curve_state = await self.curve_manager.get_curve_state(bonding_curve_pubkey, commitment=Confirmed)

                if not curve_state:
                    error_message = "Failed to fetch bonding curve state on retry."; logger.warning(f"{error_message} Retrying...");
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue
                if initial_sol_reserves is None: initial_sol_reserves = curve_state.virtual_sol_reserves

                # Calculate token amounts
                est_tokens_out = curve_state.calculate_tokens_out_for_sol(self.amount_lamports)
                min_tokens_out_for_slippage = int(est_tokens_out * (1 - self.slippage_decimal))
                min_token_output_for_ix = max(0, min_tokens_out_for_slippage)
                logger.debug(f"Est TknOut: {est_tokens_out}, MinTknOut (Slippage): {min_token_output_for_ix}, MaxSOLIn: {self.amount_lamports}")
                if min_token_output_for_ix <= 0 and est_tokens_out <= 0:
                    error_message = "Min tokens out calc <= 0."; logger.warning(error_message);
                    if est_tokens_out <= 0: break
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                # Calculate Priority Fee
                compute_unit_price_micro_lamports = 0
                try:
                    fee = await self.priority_fee_manager.calculate_priority_fee(accounts=priority_fee_target_accounts)
                    if fee is not None and fee > 0:
                        compute_unit_price_micro_lamports = int(fee)
                        logger.info(f"Using priority fee: {compute_unit_price_micro_lamports} micro-lamports (targets: {len(priority_fee_target_accounts)} accounts)")
                    elif fee == 0: logger.info("Priority Fee Manager returned 0. Using 0.")
                    else: logger.warning("Priority Fee Manager returned None. Using 0 micro-lamports.")
                except AttributeError: logger.error("PriorityFeeManager instance missing 'calculate_priority_fee' method. Using 0 fee.")
                except Exception as fee_e: logger.error(f"Error calculating priority fee: {fee_e}. Using 0 micro-lamports.", exc_info=True)

                # --- Build Instructions ---
                instructions: List[Instruction] = [ # Use specific type hint
                    set_compute_unit_limit(self.compute_unit_limit),
                    set_compute_unit_price(compute_unit_price_micro_lamports)
                ]

                # --- FIX: Await the coroutine ---
                ata_instructions: List[Instruction] = await InstructionBuilder.get_create_ata_instruction_if_needed(
                    payer_pubkey=user_pubkey,
                    owner=user_pubkey,
                    mint=mint_pubkey,
                    client=self.client.get_async_client() # Pass the raw async client
                )
                # --- END FIX ---

                # Now ata_instructions is a List[Instruction] (possibly empty)
                instructions.extend(ata_instructions)

                buy_instruction = InstructionBuilder.get_pump_buy_instruction(
                    user_pubkey=user_pubkey, mint_pubkey=mint_pubkey,
                    bonding_curve_pubkey=bonding_curve_pubkey,
                    associated_bonding_curve_pubkey=associated_bonding_curve_pubkey,
                    user_token_account=user_token_account,
                    amount_lamports=self.amount_lamports,
                    min_token_output=min_token_output_for_ix
                )
                if buy_instruction is None:
                    error_message = "Failed to build buy instruction"; logger.error(error_message + ". Stopping retries."); break

                instructions.append(buy_instruction)

                # --- Send and Confirm Transaction ---
                tx_result: TransactionResult = await build_and_send_transaction(
                    client=self.client, payer=self.wallet.payer,
                    instructions=instructions, label=f"Buy_{token_info.symbol[:5]}"
                )

                if tx_result.success and tx_result.signature:
                    logger.info(f"Buy tx sent: {tx_result.signature}. Confirming...")
                    signature_obj = Signature.from_string(tx_result.signature)
                    confirmation_status = await self.client.confirm_transaction(
                        signature_obj, timeout_secs=self.confirm_timeout,
                        commitment=TransactionConfirmationStatus.Confirmed
                    )
                    status_str = str(confirmation_status) if confirmation_status else "Timeout/Failed"
                    is_confirmed = confirmation_status is not None and \
                                   confirmation_status >= TransactionConfirmationStatus.Confirmed

                    if is_confirmed:
                        logger.info(f"Buy tx CONFIRMED: {tx_result.signature}")
                        est_tokens_out_initial = initial_curve_state.calculate_tokens_out_for_sol(self.amount_lamports)
                        amount_tokens_bought_estimate_ui = 0.0
                        buy_price_sol_per_token_estimate = 0.0
                        token_decimals = token_info.decimals if token_info.decimals is not None else DEFAULT_TOKEN_DECIMALS

                        if est_tokens_out_initial > 0: amount_tokens_bought_estimate_ui = est_tokens_out_initial / (10 ** token_decimals)
                        if amount_tokens_bought_estimate_ui > 0: buy_price_sol_per_token_estimate = self.amount_sol / amount_tokens_bought_estimate_ui

                        return TradeResult(success=True, tx_signature=tx_result.signature, price=buy_price_sol_per_token_estimate, amount=amount_tokens_bought_estimate_ui, initial_sol_liquidity=initial_sol_reserves)
                    else:
                        error_message = f"Buy tx {tx_result.signature} confirmation failed or timed out. Status: {status_str}"; logger.warning(error_message);
                        await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS); continue

                elif tx_result.error_type:
                    error_message = f"Buy tx failed ({tx_result.error_type}): {tx_result.error_message or 'No message'}"; logger.warning(f"{error_message}. Retrying...");
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 1); continue
                else:
                    error_message = "Buy tx send failed with unknown error."; logger.error(error_message);
                    await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 1); continue

            except TypeError as type_err:
                last_exception = type_err; error_message = f"TypeError during buy attempt {retry_count}: {type_err}"; logger.error(error_message, exc_info=True)
                if "token_program_id" in str(type_err) or "missing required positional argument" in str(type_err) or "'coroutine' object is not iterable" in str(type_err):
                    logger.error("Stopping retries due to unrecoverable TypeError (check instruction building/await).")
                    error_message = f"Buy failed due to TypeError: {type_err}"; break
                if retry_count < self.max_retries: await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 2)
                else: break

            except Exception as loop_exception:
                last_exception = loop_exception; error_message = f"Unexpected error during buy attempt {retry_count}: {loop_exception}"; logger.error(error_message, exc_info=True)
                if buy_instruction is None and "Failed to build buy instruction" in error_message: break
                if retry_count < self.max_retries: await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS + 2)

        logger.error(f"Failed buy {token_info.symbol} after {retry_count}/{self.max_retries} attempts: {error_message}")
        return TradeResult(success=False, error_message=error_message, initial_sol_liquidity=initial_sol_reserves)