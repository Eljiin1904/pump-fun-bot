# src/trading/buyer.py
import asyncio
from typing import Optional, List
from solders.instruction import Instruction
from solders.pubkey import Pubkey

from core.pubkeys import PumpAddresses
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL
from src.core.priority_fee.manager import PriorityFeeManager
from src.core.transactions import build_and_send_transaction, TransactionSendResult
from src.core.instruction_builder import InstructionBuilder
from src.trading.base import TokenInfo, TradeResult
from src.utils.logger import get_logger
from solana.rpc.commitment import Confirmed, Processed  # Added Processed for balance checks

logger = get_logger(__name__)


class TokenBuyer:
    def __init__(self,
                 client: SolanaClient,
                 wallet: Wallet,
                 curve_manager: BondingCurveManager,
                 fee_manager: PriorityFeeManager,
                 buy_amount_sol: float,
                 slippage_bps: int,
                 max_retries: int,
                 confirm_timeout_seconds: int,
                 priority_fee_cu_limit: int = 300_000,
                 priority_fee_cu_price: Optional[int] = None
                 ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.fee_manager = fee_manager
        self.buy_amount_sol = buy_amount_sol
        self.buy_amount_lamports = int(buy_amount_sol * LAMPORTS_PER_SOL)
        self.slippage_bps = slippage_bps
        self.max_retries = max_retries
        self.confirm_timeout_seconds = confirm_timeout_seconds
        self.priority_fee_cu_limit = priority_fee_cu_limit
        self.priority_fee_cu_price = priority_fee_cu_price
        logger.info(
            f"TokenBuyer Init: BuyAmt={self.buy_amount_sol:.6f} SOL, SlipBPS={self.slippage_bps}, MaxRetries={self.max_retries}, CU Limit={self.priority_fee_cu_limit}")

    def _calculate_min_tokens_out(self, curve_state: BondingCurveState, token_info: TokenInfo) -> int:
        # ... (implementation from previous response, ensure it's accurate for pump.fun) ...
        if self.buy_amount_lamports <= 0: return 0
        if curve_state.virtual_sol_reserves == 0:
            logger.warning(f"Cannot calculate tokens out for {token_info.symbol}, virtual SOL reserves are zero.")
            return 0
        try:
            ratio = self.buy_amount_lamports / curve_state.virtual_sol_reserves
            estimated_tokens_raw = curve_state.virtual_token_reserves * ((1 + ratio) ** 0.5 - 1)
            estimated_tokens = int(estimated_tokens_raw)
        except ZeroDivisionError:
            logger.error(f"ZeroDivisionError while estimating tokens out for {token_info.symbol}.")
            return 0
        except OverflowError:
            logger.error(f"OverflowError calculating estimated tokens for {token_info.symbol}.")
            return 0
        if estimated_tokens <= 0:
            logger.warning(f"Estimated tokens out is {estimated_tokens} for {token_info.symbol} before slippage.")
            return 0
        min_tokens_out = int(estimated_tokens * (1 - (self.slippage_bps / 10000.0)))
        logger.info(
            f"Buy Estimation for {token_info.symbol}: SOL_in={self.buy_amount_lamports}, Est.Tokens={estimated_tokens}, MinTokensOut (Slip {self.slippage_bps}BPS)={min_tokens_out}")
        return min_tokens_out

    async def _get_token_balance(self, token_account_pubkey: Pubkey) -> int:
        """Helper to get token balance, returns 0 on error or if account doesn't exist."""
        try:
            balance_resp = await self.client.get_token_account_balance(token_account_pubkey,
                                                                       commitment=Processed)  # Use Processed for faster balance checks
            if balance_resp and balance_resp.value:
                return int(balance_resp.value.amount)
        except Exception as e:
            logger.warning(f"Could not fetch balance for {token_account_pubkey}: {e}")
        return 0

    async def execute(self, token_info: TokenInfo) -> TradeResult:
        logger.info(f"Attempting buy for {token_info.symbol} ({token_info.mint_str}) with {self.buy_amount_sol} SOL")

        # ... (initial checks for bonding_curve_address, get curve_state, check is_complete, liquidity from previous response) ...
        if not token_info.bonding_curve_address:  # From previous
            return TradeResult(token_info=token_info, success=False, error="Missing bonding_curve_address")
        curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve_address)
        if not curve_state:
            return TradeResult(token_info=token_info, success=False,
                               error="Failed to fetch bonding curve state pre-buy")
        initial_sol_liquidity_lamports = curve_state.virtual_sol_reserves
        if curve_state.is_complete:
            return TradeResult(token_info=token_info, success=False, error="Bonding curve complete",
                               initial_sol_liquidity=initial_sol_liquidity_lamports)
        # ...

        min_tokens_out = self._calculate_min_tokens_out(curve_state, token_info)
        if min_tokens_out <= 0 and self.buy_amount_lamports > 0:
            return TradeResult(token_info=token_info, success=False, error="Min tokens out calculation failed",
                               initial_sol_liquidity=initial_sol_liquidity_lamports)

        tx_result: Optional[TransactionSendResult] = None
        user_ata_pubkey = InstructionBuilder.get_associated_token_address(self.wallet.pubkey, token_info.mint)

        # Get token balance BEFORE the transaction
        balance_before_lamports = await self._get_token_balance(user_ata_pubkey)
        logger.info(f"Token balance for {token_info.symbol} ({user_ata_pubkey}) BEFORE buy: {balance_before_lamports}")

        for attempt in range(self.max_retries):
            # ... (instruction building logic from previous response, including ATA creation check, priority fees, buy_ix) ...
            logger.info(f"Buy attempt {attempt + 1}/{self.max_retries} for {token_info.symbol}")
            instructions: List[Instruction] = []
            try:
                ata_account_info = await self.client.get_account_info(user_ata_pubkey, commitment=Confirmed)
                if ata_account_info is None or ata_account_info.value is None:
                    logger.info(
                        f"User ATA {user_ata_pubkey} for {token_info.symbol} does not exist. Adding create instruction.")
                    create_ata_ix = InstructionBuilder.get_create_ata_instruction(
                        payer=self.wallet.pubkey, owner=self.wallet.pubkey, mint=token_info.mint,
                        ata_pubkey=user_ata_pubkey
                    )
                    instructions.append(create_ata_ix)

                fee_check_accounts = [
                    PumpAddresses.GLOBAL_STATE, PumpAddresses.FEE_RECIPIENT, token_info.mint,
                    token_info.bonding_curve_address, token_info.associated_bonding_curve_address,
                    user_ata_pubkey, self.wallet.pubkey
                ]
                priority_fee_details = await self.fee_manager.get_priority_fee(accounts_to_check=fee_check_accounts)
                cu_limit_actual = self.priority_fee_cu_limit
                cu_price_actual = self.priority_fee_cu_price if self.priority_fee_cu_price is not None \
                    else (priority_fee_details.fee if priority_fee_details else 1)
                if cu_limit_actual: instructions.append(InstructionBuilder.set_compute_unit_limit(cu_limit_actual))
                if cu_price_actual: instructions.append(InstructionBuilder.set_compute_unit_price(cu_price_actual))

                buy_ix = InstructionBuilder.build_pump_fun_buy_instruction(
                    user_wallet_pubkey=self.wallet.pubkey, mint_pubkey=token_info.mint,
                    bonding_curve_pubkey=token_info.bonding_curve_address,
                    assoc_bonding_curve_token_account_pubkey=token_info.associated_bonding_curve_address,
                    sol_amount_in_lamports=self.buy_amount_lamports,
                    min_token_output_lamports=min_tokens_out
                )
                instructions.append(buy_ix)

                tx_result = await build_and_send_transaction(
                    client=self.client, wallet_keypair=self.wallet.keypair, instructions=instructions,
                    signers=[self.wallet.keypair], log_prefix=f"Buy_{token_info.symbol[:5]}",
                    commitment=Confirmed, confirm_timeout_seconds=self.confirm_timeout_seconds,
                    max_send_retries=1
                )
            # ... (exception handling from previous response) ...
            except Exception as e:
                error_msg = f"Exception during buy attempt {attempt + 1} for {token_info.symbol}: {str(e)}"
                logger.error(error_msg, exc_info=True)
                tx_result = TransactionSendResult(success=False, error=error_msg)  # Ensure tx_result is set for logging
                # No break here, let retry logic handle it unless it's a fatal error type

            if tx_result and tx_result.success and tx_result.signature:
                logger.info(
                    f"BUY_SUCCESS_SENT: {token_info.symbol}. Tx: {tx_result.signature}. Confirming and fetching actual balance...")

                # Wait a brief moment for state to propagate before fetching balance_after
                await asyncio.sleep(2.0)  # Adjust as needed, or use transaction confirmation status for timing

                balance_after_lamports = await self._get_token_balance(user_ata_pubkey)
                logger.info(
                    f"Token balance for {token_info.symbol} ({user_ata_pubkey}) AFTER buy: {balance_after_lamports}")

                actual_tokens_acquired = balance_after_lamports - balance_before_lamports

                if actual_tokens_acquired < 0:  # Should not happen if buy was successful
                    logger.warning(
                        f"Acquired token calculation resulted in negative value ({actual_tokens_acquired}) for {token_info.symbol}. Using min_tokens_out as fallback.")
                    actual_tokens_acquired = min_tokens_out  # Fallback to estimate
                elif actual_tokens_acquired == 0 and min_tokens_out > 0:
                    logger.warning(
                        f"Actual tokens acquired is 0, but min_tokens_out was {min_tokens_out} for {token_info.symbol}. This might indicate an issue or a very small buy. Using min_tokens_out.")
                    actual_tokens_acquired = min_tokens_out  # Fallback if balance check seems off for a successful tx

                logger.info(f"Actual tokens acquired for {token_info.symbol}: {actual_tokens_acquired}")

                return TradeResult(
                    token_info=token_info, signature=tx_result.signature, success=True,
                    initial_sol_liquidity=initial_sol_liquidity_lamports,
                    spent_lamports=self.buy_amount_lamports,
                    acquired_tokens=actual_tokens_acquired
                )
            else:  # tx_result is None or not successful
                error_msg = tx_result.error if tx_result and tx_result.error else "Send transaction failed or no signature."
                logger.warning(f"Buy attempt {attempt + 1} for {token_info.symbol} failed: {error_msg}")
                if "Insufficient funds" in error_msg:
                    logger.error(f"BUY_FAIL: Insufficient funds for {token_info.symbol}. Aborting retries.")
                    return TradeResult(token_info=token_info, success=False, error=error_msg,
                                       initial_sol_liquidity=initial_sol_liquidity_lamports)

            if attempt < self.max_retries - 1:
                await asyncio.sleep(1.0 + (attempt * 0.75))

        final_error = tx_result.error if tx_result and tx_result.error else "Buy failed after max retries."
        logger.error(f"BUY_FAIL: {token_info.symbol} after {self.max_retries} attempts. Last error: {final_error}")
        return TradeResult(
            token_info=token_info, success=False, error=final_error,
            initial_sol_liquidity=initial_sol_liquidity_lamports
        )