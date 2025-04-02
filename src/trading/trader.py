# src/trading/trader.py

import asyncio
import json
import os
import time
from dataclasses import dataclass # Make sure dataclass is imported if used for BondingCurveState
from datetime import datetime
from typing import Optional, List, Set, Dict, Awaitable, Callable

from solders.pubkey import Pubkey
from solana.rpc.commitment import Confirmed, Commitment
# --- Import TokenAmount and UiTokenAmount explicitly ---
from solana.rpc.types import TokenAmount

# Use correct relative imports
from ..cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from ..core.client import SolanaClient
from ..core.curve import BondingCurveManager, BondingCurveState # Ensure BondingCurveState is imported
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.pubkeys import PumpAddresses
from ..core.wallet import Wallet
from ..monitoring.base_listener import BaseTokenListener
from ..monitoring.listener_factory import ListenerFactory
from ..trading.base import TokenInfo, TradeResult
from ..trading.buyer import TokenBuyer
from ..trading.seller import TokenSeller
from ..utils.logger import get_logger

logger = get_logger(__name__)

class PumpTrader:
    """Main class orchestrating the pump.fun trading process."""

    # --- Correct __init__ signature (Matches cli.py call) ---
    def __init__(
        self,
        client: SolanaClient,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        sell_profit_threshold: float,
        sell_stoploss_threshold: float,
        max_token_age: float,
        wait_time_after_creation: float,
        wait_time_after_buy: float,
        wait_time_before_new_token: float,
        max_retries: int,
        max_buy_retries: int,
        confirm_timeout: int,
        listener_type: str,
        # Use names matching PriorityFeeManager constructor
        enable_dynamic_fee: bool,
        enable_fixed_fee: bool,
        fixed_fee: int,
        extra_fee: float,
        cap: int, # Accepts 'cap' from cli.py
        # Rug check params
        rug_check_creator: bool,
        rug_max_creator_hold_pct: float,
        rug_check_price_drop: bool,
        rug_price_drop_pct: float,
        rug_check_liquidity_drop: bool,
        rug_liquidity_drop_pct: float,
        # Cleanup params
        cleanup_mode: str,
        cleanup_force_close: bool,
        cleanup_priority_fee: bool,
    ):
        """Initialize the PumpTrader."""
        self.wallet = Wallet(private_key)
        self.solana_client = client
        self.curve_manager = BondingCurveManager(self.solana_client)

        # --- FIX: Pass 'hard_cap=cap' to PriorityFeeManager ---
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_fee,
            enable_fixed_fee=enable_fixed_fee,
            fixed_fee=fixed_fee,
            extra_fee=extra_fee,
            hard_cap=cap  # Pass the received 'cap' value as 'hard_cap'
        )
        # --- END FIX ---

        self.wss_endpoint = wss_endpoint
        self.listener_type = listener_type
        self.token_listener: Optional[BaseTokenListener] = None

        # Store trading parameters (ensure all params passed are stored or used)
        self.buy_amount=buy_amount; self.buy_slippage=buy_slippage; self.sell_slippage=sell_slippage
        self.sell_profit_threshold=sell_profit_threshold; self.sell_stoploss_threshold=sell_stoploss_threshold
        self.max_token_age=max_token_age; self.wait_time_after_creation=wait_time_after_creation
        self.wait_time_after_buy=wait_time_after_buy; self.wait_time_before_new_token=wait_time_before_new_token
        self.max_retries=max_retries; self.max_buy_retries=max_buy_retries; self.confirm_timeout=confirm_timeout
        self.rug_check_creator=rug_check_creator; self.rug_max_creator_hold_pct=rug_max_creator_hold_pct
        self.rug_check_price_drop=rug_check_price_drop; self.rug_price_drop_pct=rug_price_drop_pct
        self.rug_check_liquidity_drop=rug_check_liquidity_drop; self.rug_liquidity_drop_pct=rug_liquidity_drop_pct
        self.cleanup_mode=cleanup_mode; self.cleanup_force_close=cleanup_force_close; self.cleanup_priority_fee=cleanup_priority_fee

        # Initialize buyer and seller
        self.buyer = TokenBuyer(
            self.solana_client, self.wallet, self.curve_manager, self.priority_fee_manager,
            self.buy_amount, self.buy_slippage, self.max_buy_retries, self.confirm_timeout
        )
        self.seller = TokenSeller( # Assuming TokenSeller __init__ matches these args
            client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager,
            priority_fee_manager=self.priority_fee_manager,
            slippage=self.sell_slippage,
            max_retries=self.max_buy_retries # Use general TX retries
        )

        # State variables
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        self.processed_tokens: Set[str] = set()
        self.token_timestamps: Dict[str, float] = {}
        self.traded_mints_session: Set[Pubkey] = set()

        logger.info(f"PumpTrader initialized. Wallet: {self.wallet.pubkey}")
        logger.info(f"Trading Params: Buy Amt={self.buy_amount:.6f} SOL, Buy Slip={self.buy_slippage*100:.1f}%, Sell Slip={self.sell_slippage*100:.1f}%")
        logger.info(f"Sell Logic: Profit Target={self.sell_profit_threshold*100:.1f}%, Stoploss={self.sell_stoploss_threshold*100:.1f}%")
        logger.info(f"Rug Checks: CreatorHold={self.rug_check_creator}(Max={self.rug_max_creator_hold_pct*100:.1f}%), PriceDrop={self.rug_check_price_drop}({self.rug_price_drop_pct*100:.1f}%), LiqDrop={self.rug_check_liquidity_drop}({self.rug_liquidity_drop_pct*100:.1f}%)")
        logger.info(f"Cleanup Mode: {self.cleanup_mode}")


    async def start(
        self,
        match_string: Optional[str] = None,
        bro_address: Optional[str] = None,
        marry_mode: bool = False,
        yolo_mode: bool = False,
    ):
        """Start the trading bot."""
        logger.info(f"Starting pump.fun trader")
        logger.info(f"Match: {match_string or 'None'}"); logger.info(f"Creator: {bro_address or 'None'}"); logger.info(f"Marry: {marry_mode}"); logger.info(f"YOLO: {yolo_mode}"); logger.info(f"Max Age: {self.max_token_age}s")
        factory = ListenerFactory();
        try:
            self.token_listener = factory.create_listener(
                self.listener_type, self.wss_endpoint, self.solana_client
            )
            logger.info(f"Using {self.listener_type} listener")
        except ValueError as e: logger.critical(f"Listener creation fail: {e}"); return
        except Exception as e: logger.critical(f"Error creating listener: {e}", exc_info=True); return

        processor_task = asyncio.create_task(self._process_token_queue(marry_mode, yolo_mode))
        listener_task = None # Define before try block

        try:
            if self.token_listener:
                listener_task = asyncio.create_task(
                     self.token_listener.listen_for_tokens(self._queue_token, match_string, bro_address)
                )
                # Wait for either the listener or processor to finish/error
                done, pending = await asyncio.wait(
                    [processor_task, listener_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel() # Cancel the other task
                # Check for exceptions in the completed task(s)
                for task in done:
                    if task.exception():
                        raise task.exception() # Raise error to trigger finally block
            else:
                logger.error("Token listener failed initialization.")

        except asyncio.CancelledError: logger.info("Main trader task cancelled.")
        except Exception as e: logger.critical(f"Trading stopped: {e!s}", exc_info=True)
        finally:
             logger.info("Shutting down trader...")
             # Cancel tasks safely
             if listener_task and not listener_task.done(): listener_task.cancel()
             if processor_task and not processor_task.done(): processor_task.cancel()
             # Wait briefly for cancellations
             await asyncio.sleep(0.1)

             # Perform cleanup if needed
             if self.cleanup_mode == "post_session" and self.traded_mints_session:
                 logger.info("Performing post-session cleanup...")
                 try: await handle_cleanup_post_session(self.solana_client, self.wallet, list(self.traded_mints_session), self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
                 except Exception as clean_e: logger.error(f"Error during post-session cleanup: {clean_e}", exc_info=True)

             # Client is closed by cli.py's 'async with'
             logger.info("Trader shutdown complete.")


    async def _queue_token(self, token_info: TokenInfo) -> None:
        """Queue a token for processing if not already processed."""
        token_key = str(token_info.mint)
        if token_key in self.processed_tokens: return
        self.token_timestamps[token_key] = time.time()
        await self.token_queue.put(token_info)
        logger.info(f"Queued new token: {token_info.symbol} ({token_info.mint})")


    async def _process_token_queue(self, marry_mode: bool, yolo_mode: bool) -> None:
        """Continuously process tokens from the queue, checking freshness."""
        while True:
             token_info: Optional[TokenInfo] = None
             try:
                 token_info = await self.token_queue.get(); token_key = str(token_info.mint)
                 if token_key in self.processed_tokens: self.token_queue.task_done(); continue
                 discovery_time = self.token_timestamps.get(token_key); token_age = -1.0
                 if discovery_time:
                      token_age = time.time() - discovery_time
                      if token_age > self.max_token_age: logger.info(f"Skipping {token_info.symbol} - old ({token_age:.1f}s)"); self.processed_tokens.add(token_key); self.token_queue.task_done(); continue
                 else: logger.warning(f"No timestamp for {token_key}. Processing."); token_age = 0
                 self.processed_tokens.add(token_key)
                 logger.info(f"Processing token: {token_info.symbol} (age: {token_age:.1f}s)")
                 # Launch handling in a separate task? No, keep sequential for now.
                 await self._handle_token(token_info, marry_mode)
                 self.token_queue.task_done()
                 if not yolo_mode: logger.info("YOLO disabled. Exiting processor."); break
             except asyncio.CancelledError: logger.info("Token processor cancelled."); break
             except Exception as e:
                  token_id = f"token {token_info.symbol} ({token_info.mint})" if token_info else "unknown token"
                  logger.error(f"Error in token processor loop for {token_id}: {e}", exc_info=True)
                  if token_info: self.token_queue.task_done() # Mark task done even on error


    async def _handle_token(self, token_info: TokenInfo, marry_mode: bool) -> None:
        """Handles the lifecycle of trading a single token."""
        buy_success = False; buy_tx_sig = None; buyer_result: Optional[TradeResult] = None
        try:
            await self._save_token_info(token_info)
            logger.info(f"Waiting {self.wait_time_after_creation:.1f}s..."); await asyncio.sleep(self.wait_time_after_creation)
            if self.rug_check_creator:
                if not await self._check_creator_holding(token_info): logger.warning(f"Skipping {token_info.symbol}: Failed creator check."); return

            logger.info(f"Buying {self.buy_amount:.6f} SOL of {token_info.symbol}...");
            buyer_result = await self.buyer.execute(token_info=token_info)
            buy_success = buyer_result.success; buy_tx_sig = buyer_result.tx_signature

            if buy_success: logger.info(f"Bought {token_info.symbol}. Tx: {buy_tx_sig}"); self._log_trade("buy", token_info, buyer_result.price, buyer_result.amount, buy_tx_sig); self.traded_mints_session.add(token_info.mint)
            else:
                logger.error(f"Failed buy {token_info.symbol}: {buyer_result.error_message}")
                if self.cleanup_mode == "on_fail": await handle_cleanup_after_failure(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
                return

            perform_rug_sell = False
            if not marry_mode and buy_success and (self.rug_check_price_drop or self.rug_check_liquidity_drop):
                logger.info(f"Performing post-buy rug checks..."); immediate_sell_reason = await self._check_post_buy_conditions(token_info, buyer_result)
                if immediate_sell_reason: logger.warning(f"Immediate sell trigger: {immediate_sell_reason}"); perform_rug_sell = True

            if marry_mode: logger.info("Marry mode. Skipping sell."); return

            # --- Sell Logic ---
            if perform_rug_sell:
                 logger.info(f"Attempting immediate rug sell for {token_info.symbol}...")
                 seller_result: TradeResult = await self.seller.execute(token_info=token_info, buyer_result=buyer_result)
            else:
                 logger.info(f"Waiting {self.wait_time_after_buy:.1f}s before checking normal sell conditions...");
                 await asyncio.sleep(self.wait_time_after_buy)
                 logger.info(f"Checking normal sell conditions / Selling {token_info.symbol}...")
                 # Seller execute needs to contain the logic to check profit/stoploss vs current price
                 seller_result: TradeResult = await self.seller.execute(token_info=token_info, buyer_result=buyer_result)

            # --- Process Sell Result ---
            if seller_result.success:
                logger.info(f"Sold {token_info.symbol}. Tx: {seller_result.tx_signature}"); self._log_trade("sell", token_info, seller_result.price, seller_result.amount, seller_result.tx_signature)
                if self.cleanup_mode == "after_sell": await handle_cleanup_after_sell(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
            else: logger.error(f"Failed sell {token_info.symbol}: {seller_result.error_message}")

        except Exception as e: logger.error(f"Unhandled error processing {token_info.symbol}: {e}", exc_info=True)


    async def _save_token_info(self, token_info: TokenInfo) -> None:
        """Save token metadata to a file."""
        os.makedirs("trades", exist_ok=True); file_name = os.path.join("trades", f"{str(token_info.mint)}.txt")
        # Use vars() or asdict if TokenInfo is a dataclass for cleaner serialization
        data_to_save = {
            "name": token_info.name, "symbol": token_info.symbol, "uri": token_info.uri,
            "mint": str(token_info.mint), "bondingCurve": str(token_info.bonding_curve),
            "associatedBondingCurve": str(token_info.associated_bonding_curve),
            "user": str(token_info.user), "created_timestamp": token_info.created_timestamp,
            # Add other fields if they exist in TokenInfo
            "website": getattr(token_info, 'website', None),
            "twitter": getattr(token_info, 'twitter', None),
            "telegram": getattr(token_info, 'telegram', None),
            "description": getattr(token_info, 'description', None)
        }
        try:
            # Add encoding='utf-8' for robustness
            with open(file_name, "w", encoding="utf-8") as file: json.dump(data_to_save, file, indent=2)
            logger.info(f"Token info saved: {file_name}")
        except IOError as e: logger.error(f"Failed save token info: {e}")


    def _log_trade(self, action: str, token_info: TokenInfo, price: Optional[float], amount: Optional[float], tx_hash: Optional[str]) -> None:
        """Log trade details to a file."""
        os.makedirs("trades", exist_ok=True)
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(), "action": action,
            "token_address": str(token_info.mint), "symbol": token_info.symbol,
            "price": price, "amount": amount, "tx_hash": str(tx_hash)
        }
        try:
            # Add encoding='utf-8' for robustness
            with open("trades/trades.log", "a", encoding="utf-8") as log_file: log_file.write(json.dumps(log_entry) + "\n")
        except IOError as e: logger.error(f"Failed write trade log: {e}")


    # --- Rug Check Methods (Corrected type handling and units) ---
    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        """Checks if creator holds too much supply. Returns True if OK."""
        logger.info(f"Checking creator holding for {token_info.symbol}...")
        try:
            mint_pk = token_info.mint
            supply_resp: Optional[TokenAmount] = await self.solana_client.get_token_supply(mint_pk, commitment=Confirmed)

            # Check supply response type and parse .amount
            if supply_resp is None or not isinstance(supply_resp, TokenAmount) or not hasattr(supply_resp, 'amount'):
                 logger.warning(f"Cannot get valid supply obj for {mint_pk}. Skip check.")
                 return True # Fail open
            try: total_supply = int(supply_resp.amount)
            except (ValueError, TypeError): logger.warning(f"Cannot parse supply amount '{supply_resp.amount}'. Skip check."); return True
            if total_supply <= 0: logger.warning("Total supply 0 or less. Skip check."); return True

            creator_ata = Wallet.get_associated_token_address(mint=mint_pk, owner=token_info.user)
            balance_resp: Optional[TokenAmount] = await self.solana_client.get_token_account_balance(creator_ata, commitment=Confirmed)
            creator_balance = 0

            # Check balance response type and parse .amount
            if balance_resp is not None and isinstance(balance_resp, TokenAmount) and hasattr(balance_resp, 'amount'):
                try: creator_balance = int(balance_resp.amount)
                except (ValueError, TypeError): logger.warning(f"Cannot parse creator balance '{balance_resp.amount}' for {creator_ata}. Assume 0.")

            # Calculate percentage
            if creator_balance <= 0: creator_percentage = 0.0; logger.info("Creator balance zero or not found.")
            else: creator_percentage = creator_balance / total_supply; logger.info(f"Creator {token_info.user} holds {creator_balance}/{total_supply} ({creator_percentage:.2%})")

            # Compare
            if creator_percentage > self.rug_max_creator_hold_pct:
                 logger.warning(f"FAILED creator check: {creator_percentage:.2%} > {self.rug_max_creator_hold_pct:.2%}")
                 return False
            else:
                 logger.info("Creator holding check PASSED.")
                 return True
        except Exception as e: logger.error(f"Error during creator holding check: {e}", exc_info=True); return True # Fail open


    async def _check_post_buy_conditions(self, token_info: TokenInfo, buyer_result: TradeResult) -> Optional[str]:
        """Checks for rapid price/liquidity drops. Returns reason string if rug detected."""
        try:
            current_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(token_info.bonding_curve, commitment=Confirmed)
            if not current_state: logger.warning("Cannot get curve state post-buy."); return None

            original_buy_price = buyer_result.price # SOL per token
            initial_sol_liquidity_lamports = buyer_result.initial_sol_liquidity # Lamports

            # Price Drop Check
            if self.rug_check_price_drop and original_buy_price is not None and original_buy_price > 1e-18: # Avoid division by zero/tiny numbers
                current_price = current_state.calculate_price()
                if current_price < original_buy_price:
                    price_drop_pct = (original_buy_price - current_price) / original_buy_price
                    logger.debug(f"Post-buy price: Buy={original_buy_price:.9f}, Now={current_price:.9f}, Drop={price_drop_pct:.2%}, Thr={self.rug_price_drop_pct:.2%}")
                    if price_drop_pct > self.rug_price_drop_pct: return f"Price drop {price_drop_pct:.1%}"
                # else: logger.debug("Post-buy price hasn't dropped.")

            # Liquidity Drop Check (Compare Lamports to Lamports)
            if self.rug_check_liquidity_drop and initial_sol_liquidity_lamports is not None and initial_sol_liquidity_lamports > 0:
                 current_sol_reserves_lamports = getattr(current_state, 'virtual_sol_reserves', None)
                 if current_sol_reserves_lamports is not None:
                      current_sol_lamports_int = int(current_sol_reserves_lamports)
                      initial_sol_lamports_int = int(initial_sol_liquidity_lamports)
                      if current_sol_lamports_int < initial_sol_lamports_int:
                          # Ensure denominator isn't zero
                          if initial_sol_lamports_int > 0:
                              liquidity_drop_pct = (initial_sol_lamports_int - current_sol_lamports_int) / initial_sol_lamports_int
                              logger.debug(f"Post-buy liq (lamports): Initial={initial_sol_lamports_int}, Now={current_sol_lamports_int}, Drop={liquidity_drop_pct:.2%}, Thr={self.rug_liquidity_drop_pct:.2%}")
                              if liquidity_drop_pct > self.rug_liquidity_drop_pct: return f"Liquidity drop {liquidity_drop_pct:.1%}"
                          else:
                               logger.debug("Initial liquidity was zero, cannot calculate drop %.")
                      # else: logger.debug("Post-buy liq hasn't dropped.")
                 else: logger.warning("Could not get 'virtual_sol_reserves' for liq check.")
            return None
        except Exception as e: logger.error(f"Error during post-buy check: {e}", exc_info=True); return None