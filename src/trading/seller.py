# src/trading/seller.py
import asyncio
from typing import Optional, List
from solders.instruction import Instruction
from solders.pubkey import Pubkey

from core.pubkeys import PumpAddresses
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL
from src.core.priority_fee.manager import PriorityFeeManager
from src.core.transactions import build_and_send_transaction, TransactionSendResult, get_transaction_fee  # NEW
from src.core.instruction_builder import InstructionBuilder
from src.trading.base import TokenInfo, TradeResult
from src.utils.logger import get_logger
from solana.rpc.commitment import Confirmed, Processed  # Added Processed

logger = get_logger(__name__)


class TokenSeller:
    def __init__(
            self,
            client: SolanaClient,
            wallet: Wallet,
            curve_manager: BondingCurveManager,
            fee_manager: PriorityFeeManager,
            slippage_bps: int,
            max_retries: int,
            confirm_timeout_seconds: int,
            priority_fee_cu_limit: int = 200_000,
            priority_fee_cu_price: Optional[int] = None
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.fee_manager = fee_manager
        self.slippage_bps = slippage_bps
        self.max_retries = max_retries
        self.confirm_timeout_seconds = confirm_timeout_seconds
        self.priority_fee_cu_limit = priority_fee_cu_limit
        self.priority_fee_cu_price = priority_fee_cu_price
        logger.info(
            f"TokenSeller Init: SlipBPS={self.slippage_bps}, MaxRetries={self.max_retries}, CU Limit={self.priority_fee_cu_limit}")

    def _calculate_min_sol_out(self, curve_state: BondingCurveState, tokens_to_sell_lamports: int,
                               token_symbol: str) -> int:
        # ... (implementation from previous response) ...
        if tokens_to_sell_lamports <= 0: return 0
        estimated_sol_out_lamports = curve_state.estimate_sol_out_for_tokens(tokens_to_sell_lamports)
        if estimated_sol_out_lamports <= 0:
            logger.warning(f"Sell Estimation for {token_symbol}: Estimated SOL out is {estimated_sol_out_lamports} ...")
            return 0
        min_sol_out = int(estimated_sol_out_lamports * (1 - (self.slippage_bps / 10000.0)))
        logger.info(
            f"Sell Estimation for {token_symbol}: TokensIn={tokens_to_sell_lamports}, Est.SOL_Out={estimated_sol_out_lamports}, MinSOLOut ...={min_sol_out}")
        return min_sol_out

    async def _get_sol_balance(self, account_pubkey: Pubkey) -> int:
        """Helper to get SOL balance, returns 0 on error."""
        try:
            balance_resp = await self.client.get_balance(account_pubkey,
                                                         commitment=Processed)  # Use Processed for faster balance checks
            if balance_resp and balance_resp.value is not None:  # get_balance returns RpcResponseContext(RpcContext, int)
                return balance_resp.value
        except Exception as e:
            logger.warning(f"Could not fetch SOL balance for {account_pubkey}: {e}")
        return 0

    async def execute(self, token_info: TokenInfo, tokens_to_sell_lamports: int) -> TradeResult:
        logger.info(
            f"Attempting sell of {tokens_to_sell_lamports} lamports of {token_info.symbol} ({token_info.mint_str})")

        # ... (initial checks for tokens_to_sell, user_ata_balance, curve_state from previous response) ...
        if tokens_to_sell_lamports <= 0:  # From previous
            return TradeResult(token_info=token_info, success=False, error="Invalid token amount for selling")
        user_ata_pubkey = InstructionBuilder.get_associated_token_address(self.wallet.pubkey, token_info.mint)
        try:  # From previous
            ata_balance_resp = await self.client.get_token_account_balance(user_ata_pubkey, commitment=Processed)
            if ata_balance_resp is None or ata_balance_resp.value is None or int(
                    ata_balance_resp.value.amount) < tokens_to_sell_lamports:
                return TradeResult(token_info=token_info, success=False, error="Insufficient token balance for sell")
        except Exception as e:
            return TradeResult(token_info=token_info, success=False, error="Failed to verify token balance")
        if not token_info.bonding_curve_address:  # From previous
            return TradeResult(token_info=token_info, success=False, error="Missing bonding_curve_address for sell")
        curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve_address)
        if not curve_state:
            return TradeResult(token_info=token_info, success=False,
                               error="Failed to fetch bonding curve state for sell")
        liquidity_at_sell_attempt_lamports = curve_state.virtual_sol_reserves
        # ...

        min_sol_out_lamports = self._calculate_min_sol_out(curve_state, tokens_to_sell_lamports, token_info.symbol)
        if min_sol_out_lamports <= 0 and tokens_to_sell_lamports > 0:
            return TradeResult(token_info=token_info, success=False, error="Min SOL out calculation failed",
                               initial_sol_liquidity=liquidity_at_sell_attempt_lamports)

        tx_result: Optional[TransactionSendResult] = None

        # Get SOL balance BEFORE the transaction
        sol_balance_before_lamports = await self._get_sol_balance(self.wallet.pubkey)
        logger.info(f"SOL balance for {self.wallet.pubkey} BEFORE sell: {sol_balance_before_lamports}")

        for attempt in range(self.max_retries):
            # ... (instruction building logic from previous response: priority fees, sell_ix) ...
            logger.info(f"Sell attempt {attempt + 1}/{self.max_retries} for {token_info.symbol}")
            instructions: List[Instruction] = []
            try:
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

                sell_ix = InstructionBuilder.build_pump_fun_sell_instruction(
                    user_wallet_pubkey=self.wallet.pubkey, mint_pubkey=token_info.mint,
                    bonding_curve_pubkey=token_info.bonding_curve_address,
                    assoc_bonding_curve_token_account_pubkey=token_info.associated_bonding_curve_address,
                    token_amount_in_lamports=tokens_to_sell_lamports,
                    min_sol_output_lamports=min_sol_out_lamports
                )
                instructions.append(sell_ix)

                tx_result = await build_and_send_transaction(
                    client=self.client, wallet_keypair=self.wallet.keypair, instructions=instructions,
                    signers=[self.wallet.keypair], log_prefix=f"Sell_{token_info.symbol[:5]}",
                    commitment=Confirmed, confirm_timeout_seconds=self.confirm_timeout_seconds,
                    max_send_retries=1
                )
            # ... (exception handling from previous response) ...
            except Exception as e:
                error_msg = f"Exception during sell attempt {attempt + 1} for {token_info.symbol}: {str(e)}"
                logger.error(error_msg, exc_info=True)
                tx_result = TransactionSendResult(success=False, error=error_msg)
                # No break here, let retry logic handle it

            if tx_result and tx_result.success and tx_result.signature:
                logger.info(
                    f"SELL_SUCCESS_SENT: {token_info.symbol}. Tx: {tx_result.signature}. Confirming and fetching actual SOL balance...")

                # Wait for state propagation or use confirmation status
                await asyncio.sleep(2.0)

                sol_balance_after_lamports = await self._get_sol_balance(self.wallet.pubkey)
                logger.info(f"SOL balance for {self.wallet.pubkey} AFTER sell: {sol_balance_after_lamports}")

                # Get the actual fee paid for THIS transaction
                tx_fee_lamports = 0
                if tx_result.signature:  # Ensure we have a signature
                    fee_info = await get_transaction_fee(self.client, tx_result.signature)  # Needs this utility
                    if fee_info is not None:
                        tx_fee_lamports = fee_info
                        logger.info(f"Actual fee for sell tx {tx_result.signature}: {tx_fee_lamports} lamports")
                    else:
                        logger.warning(
                            f"Could not retrieve fee for sell tx {tx_result.signature}. SOL gained will be less accurate.")
                        # Fallback: estimate fee (e.g. 5000 lamports base + priority fee if known)
                        # tx_fee_lamports = 5000 + (cu_price_actual * (cu_limit_actual / 1_000_000)) # Rough estimate

                # SOL gained = (Balance After - Balance Before) + Fee Paid for this sell tx
                # Or simpler: SOL Gained from contract = (Balance After + Fee Paid) - Balance Before
                # The balance change includes the fee deduction. So, (Balance After - Balance Before) is (SOL from curve - fee).
                # Thus, SOL from curve = (Balance After - Balance Before) + fee.
                sol_gained_from_curve = (sol_balance_after_lamports - sol_balance_before_lamports) + tx_fee_lamports

                if sol_gained_from_curve < 0:  # Should not happen if sell was successful and fee is accounted for
                    logger.warning(
                        f"Acquired SOL calculation resulted in negative value ({sol_gained_from_curve}) for {token_info.symbol}. Using min_sol_out as fallback.")
                    sol_gained_from_curve = min_sol_out_lamports  # Fallback to estimate
                elif sol_gained_from_curve == 0 and min_sol_out_lamports > 0:
                    logger.warning(
                        f"Actual SOL acquired is 0, but min_sol_out was {min_sol_out_lamports} for {token_info.symbol}. Using min_sol_out.")
                    sol_gained_from_curve = min_sol_out_lamports

                logger.info(f"Actual SOL acquired from curve for {token_info.symbol}: {sol_gained_from_curve}")

                return TradeResult(
                    token_info=token_info, signature=tx_result.signature, success=True,
                    initial_sol_liquidity=liquidity_at_sell_attempt_lamports,
                    acquired_sol=sol_gained_from_curve,
                    sold_tokens=tokens_to_sell_lamports
                )
            else:  # tx_result is None or not successful
                error_msg = tx_result.error if tx_result and tx_result.error else "Send transaction failed or no signature."
                logger.warning(f"Sell attempt {attempt + 1} for {token_info.symbol} failed: {error_msg}")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(1.0 + (attempt * 0.75))

        final_error = tx_result.error if tx_result and tx_result.error else "Sell failed after max retries."
        logger.error(f"SELL_FAIL: {token_info.symbol} after {self.max_retries} attempts. Last error: {final_error}")
        return TradeResult(
            token_info=token_info, success=False, error=final_error,
            initial_sol_liquidity=liquidity_at_sell_attempt_lamports
        )