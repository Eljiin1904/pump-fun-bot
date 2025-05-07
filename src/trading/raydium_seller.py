# src/trading/raydium_seller.py (Complete with fixes)

import asyncio
from typing import Optional, List
from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solders.keypair import Keypair

# Core Components
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager
from src.core.transactions import build_and_send_transaction, TransactionSendResult, get_transaction_fee
from src.core.instruction_builder import InstructionBuilder
from src.core.pubkeys import SolanaProgramAddresses
from src.core.curve import LAMPORTS_PER_SOL  # For conversion

# V3 Components & Base Types
from src.tools.raydium_amm import RaydiumSwapWrapper, RaydiumPoolInfo
from src.trading.base import TokenInfo, TradeResult  # TokenState not needed directly here

# Utils
from src.utils.logger import get_logger

# Solana specifics
from solana.rpc.commitment import Confirmed, Processed

logger = get_logger(__name__)
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")


class RaydiumSeller:
    """Handles selling tokens on Raydium V4 pools."""

    def __init__(
            self,
            client: SolanaClient,
            wallet: Wallet,
            swap_wrapper: RaydiumSwapWrapper,
            fee_manager: PriorityFeeManager,
            sell_slippage_bps: int,
            max_retries: int,
            confirm_timeout_seconds: int,
            priority_fee_cu_limit: int = 600_000,
            priority_fee_cu_price: Optional[int] = None
    ):
        self.client = client
        self.wallet = wallet
        self.swap_wrapper = swap_wrapper
        self.fee_manager = fee_manager
        self.slippage_bps = sell_slippage_bps
        self.max_retries = max_retries
        self.confirm_timeout_seconds = confirm_timeout_seconds
        self.priority_fee_cu_limit = priority_fee_cu_limit
        self.priority_fee_cu_price = priority_fee_cu_price
        logger.info(
            f"RaydiumSeller Init: SlipBPS={self.slippage_bps}, MaxRetries={self.max_retries}, CU Limit={self.priority_fee_cu_limit}")

    async def _calculate_min_sol_out(
            self,
            pool_info: RaydiumPoolInfo,
            tokens_to_sell_lamports: int,
            token_info: TokenInfo,  # <<< FIXED: Added parameter
            token_symbol: str
    ) -> int:
        """Calculates min SOL output based on pool reserves and slippage."""
        if tokens_to_sell_lamports <= 0: return 0

        try:
            # Fetch live vault balances for accurate estimation
            base_vault_pk = Pubkey.from_string(pool_info.base_vault)
            quote_vault_pk = Pubkey.from_string(pool_info.quote_vault)

            # Use Processed for faster balance checks during calculation
            base_vault_balance_resp = await self.client.get_token_account_balance(base_vault_pk, commitment=Processed)
            quote_vault_balance_resp = await self.client.get_token_account_balance(quote_vault_pk, commitment=Processed)

            if not (base_vault_balance_resp and base_vault_balance_resp.value and \
                    quote_vault_balance_resp and quote_vault_balance_resp.value):
                logger.warning(
                    f"RaydiumSell Calc: Failed fetch vault balances for pool {pool_info.id}. Cannot estimate.")
                return 0

            base_reserves = int(base_vault_balance_resp.value.amount)
            quote_reserves = int(quote_vault_balance_resp.value.amount)

            estimated_sol_out_lamports = 0
            fee_numerator = 997  # Raydium V4 fee approx (0.25% -> 1000 - 2.5 ~ 997 / 1000)
            fee_denominator = 1000

            # --- FIXED: Use token_info parameter ---
            if pool_info.base_mint == token_info.mint_str:  # Selling base (token) for quote (WSOL)
                numerator = quote_reserves * tokens_to_sell_lamports * fee_numerator
                denominator = (base_reserves * fee_denominator) + (tokens_to_sell_lamports * fee_numerator)
                if denominator != 0: estimated_sol_out_lamports = numerator // denominator
            elif pool_info.quote_mint == token_info.mint_str:  # Selling quote (token) for base (WSOL)
                numerator = base_reserves * tokens_to_sell_lamports * fee_numerator
                denominator = (quote_reserves * fee_denominator) + (tokens_to_sell_lamports * fee_numerator)
                if denominator != 0: estimated_sol_out_lamports = numerator // denominator
            # --- END FIX ---
            else:
                # Log the actual mint string being compared
                logger.error(
                    f"RaydiumSell Calc: Pool {pool_info.id} misconfiguration for token {token_symbol} ({token_info.mint_str}). Base={pool_info.base_mint}, Quote={pool_info.quote_mint}")
                return 0

            if estimated_sol_out_lamports <= 0:
                logger.warning(
                    f"RaydiumSell Estimation for {token_symbol}: Estimated SOL out is {estimated_sol_out_lamports} before slippage.")
                return 0

            # Apply slippage
            min_sol_out = int(estimated_sol_out_lamports * (1 - (self.slippage_bps / 10000.0)))
            logger.info(
                f"RaydiumSell Estimation for {token_symbol}: TokensIn={tokens_to_sell_lamports}, Est.SOL_Out={estimated_sol_out_lamports}, MinSOLOut (Slip {self.slippage_bps}BPS)={min_sol_out}")
            return min_sol_out

        except Exception as e:
            logger.error(f"Error calculating min SOL out for Raydium sell ({token_symbol}): {e}", exc_info=True)
            return 0

    async def _get_sol_balance(self, account_pubkey: Pubkey) -> int:
        """Helper to get SOL balance, returns 0 on error."""
        try:
            balance_resp = await self.client.get_balance(account_pubkey, commitment=Processed)
            if balance_resp and balance_resp.value is not None:
                return balance_resp.value
        except Exception as e:
            logger.warning(f"Could not fetch SOL balance for {account_pubkey}: {e}")
        return 0

    async def execute(self,
                      token_info: TokenInfo,
                      amm_info: RaydiumPoolInfo,
                      tokens_to_sell_lamports: int
                      ) -> TradeResult:
        """Executes a sell order on Raydium."""
        logger.info(
            f"Attempting Raydium sell of {tokens_to_sell_lamports / (10 ** token_info.decimals):.6f} {token_info.symbol} via pool {amm_info.id}")

        if tokens_to_sell_lamports <= 0:
            return TradeResult(token_info=token_info, success=False, error="Invalid token amount for selling")

        # Pre-sell check for user's token balance
        user_ata_pubkey = InstructionBuilder.get_associated_token_address(self.wallet.pubkey, token_info.mint)
        try:
            # Use Confirmed commitment before executing trade based on balance
            ata_balance_resp = await self.client.get_token_account_balance(user_ata_pubkey, commitment=Confirmed)
            if ata_balance_resp is None or ata_balance_resp.value is None or int(
                    ata_balance_resp.value.amount) < tokens_to_sell_lamports:
                current_bal = ata_balance_resp.value.amount if ata_balance_resp and ata_balance_resp.value else "N/A"
                logger.error(
                    f"RAYDIUM_SELL_FAIL: Insufficient token balance in {user_ata_pubkey}. Need: {tokens_to_sell_lamports}, Have: {current_bal}.")
                return TradeResult(token_info=token_info, success=False,
                                   error="Insufficient token balance pre-sell check")
        except Exception as e:
            logger.error(f"RAYDIUM_SELL_FAIL: Failed pre-sell balance check for {user_ata_pubkey}: {e}")
            return TradeResult(token_info=token_info, success=False, error=f"Failed pre-sell balance check: {e}")

        # Calculate min SOL out (passing token_info)
        min_sol_out_lamports = await self._calculate_min_sol_out(
            amm_info,
            tokens_to_sell_lamports,
            token_info,  # <<< FIXED: Pass token_info
            token_info.symbol
        )
        if min_sol_out_lamports <= 0:
            return TradeResult(token_info=token_info, success=False, error="Min SOL out calculation failed or zero")

        # --- Transaction Building and Sending ---
        tx_result: Optional[TransactionSendResult] = None
        sol_balance_before = await self._get_sol_balance(self.wallet.pubkey)
        logger.info(f"SOL balance BEFORE Raydium sell: {sol_balance_before}")

        for attempt in range(self.max_retries):
            logger.info(f"Raydium Sell attempt {attempt + 1}/{self.max_retries} for {token_info.symbol}")
            instructions: List[Instruction] = []
            try:
                # Build Swap Instructions (includes potential ATA creation for WSOL)
                swap_instructions = await self.swap_wrapper.build_swap_instructions(
                    pool_info=amm_info,
                    owner_keypair=self.wallet.keypair,
                    token_in_mint=token_info.mint,
                    token_out_mint=WSOL_MINT,  # Sell for WSOL
                    amount_in_lamports=tokens_to_sell_lamports,
                    min_amount_out_lamports=min_sol_out_lamports
                )
                instructions.extend(swap_instructions)

                # Add Priority Fee Instructions (Prepend)
                fee_check_accounts = [amm_info.amm_id, Pubkey.from_string(amm_info.base_vault),
                                      Pubkey.from_string(amm_info.quote_vault), user_ata_pubkey, self.wallet.pubkey]
                priority_fee_details = await self.fee_manager.get_priority_fee(accounts_to_check=fee_check_accounts)
                cu_limit_actual = self.priority_fee_cu_limit
                cu_price_actual = self.priority_fee_cu_price if self.priority_fee_cu_price is not None else (
                    priority_fee_details.fee if priority_fee_details else 1)
                if cu_price_actual: instructions.insert(0, InstructionBuilder.set_compute_unit_price(cu_price_actual))
                if cu_limit_actual: instructions.insert(0, InstructionBuilder.set_compute_unit_limit(cu_limit_actual))
                # logger.info(f"Using CU Limit: {cu_limit_actual}, CU Price: {cu_price_actual} for Raydium sell attempt {attempt+1}")

                # Send Transaction
                tx_result = await build_and_send_transaction(
                    client=self.client, payer=self.wallet.keypair, instructions=instructions,
                    signers=[self.wallet.keypair], label=f"SellRaydium_{token_info.symbol[:5]}",
                    commitment=Confirmed, confirm_timeout_seconds=self.confirm_timeout_seconds,
                    max_send_retries=1
                )

                # Process Result
                if tx_result and tx_result.success and tx_result.signature:
                    logger.info(
                        f"RAYDIUM_SELL_SUCCESS_SENT: {token_info.symbol}. Tx: {tx_result.signature}. Fetching balances...")
                    await asyncio.sleep(2.0)  # Allow state propagation

                    sol_balance_after = await self._get_sol_balance(self.wallet.pubkey)
                    logger.info(f"SOL balance AFTER Raydium sell: {sol_balance_after}")
                    tx_fee = await get_transaction_fee(self.client, tx_result.signature) or 0
                    logger.info(f"Fee for Raydium sell tx {tx_result.signature}: {tx_fee} lamports")

                    actual_sol_acquired = (sol_balance_after - sol_balance_before) + tx_fee
                    if actual_sol_acquired < 0:
                        actual_sol_acquired = min_sol_out_lamports  # Fallback
                    elif actual_sol_acquired == 0 and min_sol_out_lamports > 0:
                        actual_sol_acquired = min_sol_out_lamports  # Fallback

                    logger.info(
                        f"Actual SOL acquired from Raydium sell for {token_info.symbol}: {actual_sol_acquired} lamports")

                    return TradeResult(
                        token_info=token_info, signature=tx_result.signature, success=True,
                        acquired_sol=actual_sol_acquired, sold_tokens=tokens_to_sell_lamports
                    )
                else:
                    error_msg = tx_result.error if tx_result and tx_result.error else "Send transaction failed or no signature."
                    logger.warning(f"Raydium Sell attempt {attempt + 1} for {token_info.symbol} failed: {error_msg}")
                    if tx_result and tx_result.error_type == "TxError":  # If tx failed on chain, don't retry
                        logger.error(f"Raydium sell tx failed on-chain for {token_info.symbol}. Aborting.")
                        return TradeResult(token_info=token_info, success=False, error=error_msg, error_type="TxError")
                    if "Insufficient funds" in error_msg:
                        logger.error(
                            f"RAYDIUM_SELL_FAIL: Insufficient funds for fee for {token_info.symbol}. Aborting.")
                        return TradeResult(token_info=token_info, success=False, error=error_msg,
                                           error_type="InsufficientFunds")

            except ValueError as e_val:  # Catch specific build errors
                error_msg = f"BuildError during Raydium sell attempt {attempt + 1}: {e_val}"
                logger.error(error_msg, exc_info=False)  # Don't need full trace for expected build errors
                tx_result = TransactionSendResult(success=False, error=error_msg, error_type="BuildError")
                break  # Don't retry build errors
            except Exception as e:
                error_msg = f"Exception during Raydium sell attempt {attempt + 1}: {str(e)}"
                logger.error(error_msg, exc_info=True)
                tx_result = TransactionSendResult(success=False, error=error_msg)

            if attempt < self.max_retries - 1:
                await asyncio.sleep(1.5 + (attempt * 1.0))  # Longer backoff

        # Loop finished without success
        final_error = tx_result.error if tx_result and tx_result.error else "Raydium sell failed after max retries."
        logger.error(
            f"RAYDIUM_SELL_FAIL: {token_info.symbol} after {self.max_retries} attempts. Last error: {final_error}")
        return TradeResult(
            token_info=token_info, success=False, error=final_error,
            error_type=tx_result.error_type if tx_result else "UnknownError"
        )