# src/trading/trader.py

import asyncio
import json
import os
import time
import dataclasses # Keep as it's used in _save_token_info
# --- Import timezone for aware datetime ---
from datetime import datetime, timezone
# --- End Import ---
from typing import Optional, Set, Dict, Tuple # Added Tuple for type hints

# --- Solana/Solders Imports ---
from solders.pubkey import Pubkey
from solana.rpc.commitment import Confirmed # Use the Enum
from solana.rpc.types import TokenAmount # Keep explicit import

# --- Project Relative Imports ---
from ..cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from ..core.client import SolanaClient
from ..core.curve import BondingCurveManager, BondingCurveState # Use updated curve.py
from ..core.priority_fee.manager import PriorityFeeManager
# PumpAddresses removed as unused
from ..core.wallet import Wallet
from ..monitoring.base_listener import BaseTokenListener # Keep base class import
from ..monitoring.listener_factory import ListenerFactory
from ..trading.base import TokenInfo, TradeResult # Ensure TradeResult includes initial_sol_liquidity
from ..trading.buyer import TokenBuyer # Ensure buyer returns initial_sol_liquidity
from ..trading.seller import TokenSeller # Ensure seller accepts confirm_timeout
from ..utils.logger import get_logger

# --- Data and Raydium Seller Imports ---
# Ensure these files exist and contain the correct implementations/placeholders
from ..data.raydium_data import get_raydium_pool_data
from .raydium_seller import RaydiumSeller

logger = get_logger(__name__)

# --- Token State Definitions ---
class TokenState:
    DETECTED = "DETECTED"; BUYING = "BUYING"; BUY_FAILED = "BUY_FAILED"
    ON_BONDING_CURVE = "ON_BONDING_CURVE"; SELLING_CURVE = "SELLING_CURVE"; SOLD_CURVE = "SOLD_CURVE"
    SELL_CURVE_FAILED = "SELL_CURVE_FAILED"; ON_RAYDIUM = "ON_RAYDIUM"; SELLING_RAYDIUM = "SELLING_RAYDIUM"
    SOLD_RAYDIUM = "SOLD_RAYDIUM"; SELL_RAYDIUM_FAILED = "SELL_RAYDIUM_FAILED"; RUGGED = "RUGGED"
    MONITORING_ERROR = "MONITORING_ERROR"

class PumpTrader:
    """Main class orchestrating the pump.fun trading process with two stages."""

    # --- __init__ Method (Removed unused rpc_endpoint) ---
    def __init__(
        self, client: SolanaClient, # Removed rpc_endpoint
        wss_endpoint: str, private_key: str, buy_amount: float, buy_slippage: float,
        sell_slippage: float, sell_profit_threshold: float, sell_stoploss_threshold: float,
        max_token_age: float, wait_time_after_creation: float, wait_time_after_buy: float,
        wait_time_before_new_token: float, max_retries: int, max_buy_retries: int,
        confirm_timeout: int, listener_type: str, enable_dynamic_fee: bool, enable_fixed_fee: bool,
        fixed_fee: int, extra_fee: float, cap: int, rug_check_creator: bool,
        rug_max_creator_hold_pct: float, rug_check_price_drop: bool, rug_price_drop_pct: float,
        rug_check_liquidity_drop: bool, rug_liquidity_drop_pct: float, cleanup_mode: str,
        cleanup_force_close: bool, cleanup_priority_fee: bool
    ):
        self.wallet = Wallet(private_key)
        self.solana_client = client # Store client wrapper
        self.curve_manager = BondingCurveManager(self.solana_client)
        # Pass async client to fee manager - Requires get_async_client() on SolanaClient wrapper
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client.get_async_client(), enable_dynamic_fee=enable_dynamic_fee,
            enable_fixed_fee=enable_fixed_fee, fixed_fee=fixed_fee,
            extra_fee=extra_fee, hard_cap=cap
        )
        self.wss_endpoint = wss_endpoint
        self.listener_type = listener_type
        self.token_listener: Optional[BaseTokenListener] = None
        # Store parameters
        self.buy_amount=buy_amount; self.buy_slippage=buy_slippage; self.sell_slippage=sell_slippage;
        self.sell_profit_threshold=sell_profit_threshold; self.sell_stoploss_threshold=sell_stoploss_threshold;
        self.max_token_age=max_token_age; self.wait_time_after_creation=wait_time_after_creation;
        self.wait_time_after_buy=wait_time_after_buy; self.wait_time_before_new_token=wait_time_before_new_token;
        self.max_retries=max_retries; self.max_buy_retries=max_buy_retries; self.confirm_timeout = confirm_timeout;
        self.rug_check_creator=rug_check_creator; self.rug_max_creator_hold_pct=rug_max_creator_hold_pct;
        self.rug_check_price_drop=rug_check_price_drop; self.rug_price_drop_pct=rug_price_drop_pct;
        self.rug_check_liquidity_drop=rug_check_liquidity_drop; self.rug_liquidity_drop_pct=rug_liquidity_drop_pct;
        self.cleanup_mode=cleanup_mode; self.cleanup_force_close=cleanup_force_close; self.cleanup_priority_fee=cleanup_priority_fee;
        self.marry_mode_flag = False
        # Init Buyer/Seller
        self.buyer = TokenBuyer(self.solana_client, self.wallet, self.curve_manager, self.priority_fee_manager, self.buy_amount, self.buy_slippage, self.max_buy_retries, self.confirm_timeout)
        self.seller = TokenSeller(client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager, priority_fee_manager=self.priority_fee_manager, slippage=self.sell_slippage, max_retries=self.max_buy_retries, confirm_timeout=self.confirm_timeout)
        # State dicts
        self.token_states: Dict[str, str] = {}; self.token_infos: Dict[str, TokenInfo] = {}; self.token_buy_results: Dict[str, TradeResult] = {}
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue(); self.processed_tokens: Set[str] = set(); self.token_timestamps: Dict[str, float] = {}; self.traded_mints_session: Set[Pubkey] = set()
        self.SOL_THRESHOLD_FOR_RAYDIUM = 300 * 1_000_000_000
        logger.info(f"PumpTrader initialized. Wallet: {self.wallet.pubkey}")
        logger.info(f"Trading Params: Buy Amt={self.buy_amount:.6f} SOL, Buy Slip={self.buy_slippage*100:.1f}%, Sell Slip={self.sell_slippage*100:.1f}%")
        logger.info(f"Sell Logic: Profit Target={self.sell_profit_threshold*100:.1f}%, Stoploss={self.sell_stoploss_threshold*100:.1f}%")
        logger.info(f"Raydium Transition Threshold: ~{self.SOL_THRESHOLD_FOR_RAYDIUM / 1e9:.1f} SOL in curve")
        logger.info(f"Rug Checks: CreatorHold={self.rug_check_creator}(Max={self.rug_max_creator_hold_pct*100:.1f}%), PriceDrop={self.rug_check_price_drop}({self.rug_price_drop_pct*100:.1f}%), LiqDrop={self.rug_check_liquidity_drop}({self.rug_liquidity_drop_pct*100:.1f}% post-buy)")
        logger.info(f"Cleanup Mode: {self.cleanup_mode}")


    # --- Helper methods (Unchanged) ---
    def get_stored_token_info(self, mint_str: str) -> Optional[TokenInfo]: return self.token_infos.get(mint_str)
    def get_stored_buy_result(self, mint_str: str) -> Optional[TradeResult]: return self.token_buy_results.get(mint_str)

    # --- CORRECTED start Method ---
    async def start(
        self, match_string: Optional[str] = None, bro_address: Optional[str] = None,
        marry_mode: bool = False, yolo_mode: bool = False
    ):
        self.marry_mode_flag = marry_mode
        logger.info(f"Starting trader...")
        logger.info(f"Match: {match_string or 'None'} | Creator: {bro_address or 'None'} | Marry: {marry_mode} | YOLO: {yolo_mode} | Max Age: {self.max_token_age}s")

        factory = ListenerFactory()
        listener_task: Optional[asyncio.Task] = None
        monitor_task: Optional[asyncio.Task] = None
        processor_task: Optional[asyncio.Task] = None

        try:
            # Initialize Listener
            self.token_listener = factory.create_listener(self.listener_type, self.wss_endpoint, self.solana_client)
            logger.info(f"Using {self.listener_type} listener")
            if not hasattr(self.token_listener, 'listen_for_tokens'):
                 logger.critical(f"Listener object missing 'listen_for_tokens'. Cannot start.")
                 return # Exit if listener is invalid

            # Start Background Tasks
            processor_task = asyncio.create_task(self._process_token_queue(marry_mode, yolo_mode), name="TokenProcessor")
            monitor_task = asyncio.create_task(self._monitor_active_trades(), name="TradeMonitor")

            # Start and Await the Main Listener Task
            logger.info("Starting main listener task...")
            listener_task = asyncio.create_task(
                self.token_listener.listen_for_tokens(self._queue_token, match_string, bro_address),
                name="TokenListener"
            )

            # Keep running by awaiting the listener task directly.
            await listener_task

        except asyncio.CancelledError:
            logger.info("Trader start task cancelled.")
        except Exception as e:
            logger.critical(f"Error during trader start or listener execution: {e}", exc_info=True)
        finally:
            logger.info("Shutting down trader (start method finally block)...")

            # Gracefully stop the listener first
            if self.token_listener and listener_task and not listener_task.done():
                 logger.info("Requesting listener stop...")
                 if hasattr(self.token_listener, 'stop'):
                      await self.token_listener.stop()
                 else:
                      listener_task.cancel()

            # Cancel other background tasks
            tasks_to_cancel = [t for t in [processor_task, monitor_task] if t and not t.done()]
            if tasks_to_cancel:
                 logger.info(f"Cancelling {len(tasks_to_cancel)} background tasks...")
                 for task in tasks_to_cancel: task.cancel()
                 await asyncio.sleep(0.5); logger.info("Background tasks cancellation requested.")

            # Check listener task exception after stop/cancel attempt
            if listener_task and listener_task.done() and listener_task.exception():
                 exc = listener_task.exception()
                 logger.error(f"Listener task finished with exception: {exc}", exc_info=exc)

            # Perform post-session cleanup
            if self.cleanup_mode == "post_session" and self.traded_mints_session:
                logger.info("Performing post-session cleanup...")
                try:
                    await handle_cleanup_post_session(
                        self.solana_client, self.wallet, list(self.traded_mints_session),
                        self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee
                    )
                except Exception as clean_e: logger.error(f"Error post-session cleanup: {clean_e}", exc_info=True)

            logger.info("Trader start method finished cleanup.")
            # Note: The main 'cli.py' finally block will handle async_client closure

    # --- _queue_token Method (Full Implementation) ---
    async def _queue_token(self, token_info: TokenInfo) -> None:
        token_key = str(token_info.mint)
        if token_key in self.processed_tokens or token_key in self.token_states: return
        self.token_timestamps[token_key] = time.time()
        await self.token_queue.put(token_info)
        logger.info(f"Queued: {token_info.symbol} ({token_key[:6]}...)")


    # --- _process_token_queue Method (Full Implementation) ---
    async def _process_token_queue(self, marry_mode: bool, yolo_mode: bool) -> None:
        logger.info("Starting token processor task...")
        while True:
             token_info: Optional[TokenInfo] = None
             try:
                 token_info = await self.token_queue.get(); token_key = str(token_info.mint)
                 if token_key in self.processed_tokens or token_key in self.token_states: self.token_queue.task_done(); continue
                 discovery_time = self.token_timestamps.get(token_key)
                 # Check age directly
                 if discovery_time and (time.time() - discovery_time > self.max_token_age):
                      logger.info(f"Skip {token_info.symbol} old (>{self.max_token_age}s)")
                      self.processed_tokens.add(token_key); self.token_queue.task_done(); continue
                 elif not discovery_time: logger.warning(f"No discovery time {token_key}. Proc.")

                 self.processed_tokens.add(token_key); logger.info(f"Processing queue item: {token_info.symbol}")
                 asyncio.create_task(self._handle_new_token(token_info), name=f"Handle_{token_key[:6]}")
                 self.token_queue.task_done()
                 if not yolo_mode: logger.info("YOLO disabled. Exit processor."); break
             except asyncio.CancelledError: logger.info("Token processor task cancelled."); break
             except Exception as e:
                  token_id = f"token {token_info.symbol} ({token_info.mint})" if token_info else "unknown"; logger.error(f"Error in processor loop for {token_id}: {e}", exc_info=True)
                  if token_info: self.token_queue.task_done(); await asyncio.sleep(1)
        logger.info("Token processor task finished.")


    # --- _handle_new_token Method (Full Implementation) ---
    async def _handle_new_token(self, token_info: TokenInfo) -> None:
        mint_str = str(token_info.mint)
        if mint_str in self.token_states: logger.debug(f"Token {mint_str[:6]} handled. Ignore."); return
        logger.info(f"Handling new token: {token_info.symbol} ({mint_str[:6]})")
        self.token_states[mint_str] = TokenState.DETECTED; self.token_infos[mint_str] = token_info
        buyer_result: Optional[TradeResult] = None # Keep buyer_result variable
        try:
            await self._save_token_info(token_info)
            logger.info(f"Wait {self.wait_time_after_creation:.1f}s pre-buy {token_info.symbol}...")
            await asyncio.sleep(self.wait_time_after_creation)
            if self.rug_check_creator:
                try:
                    creator_ok = await asyncio.wait_for(self._check_creator_holding(token_info), timeout=30.0)
                    if not creator_ok: logger.warning(f"Skip {token_info.symbol}: Creator check fail."); self.token_states[mint_str] = TokenState.RUGGED; return
                except asyncio.TimeoutError: logger.warning(f"Skip {token_info.symbol}: Creator check timeout."); self.token_states[mint_str] = TokenState.BUY_FAILED; return
                except Exception as rug_e: logger.error(f"Error creator check {token_info.symbol}: {rug_e}"); self.token_states[mint_str] = TokenState.BUY_FAILED; return
            logger.info(f"Attempt buy {self.buy_amount:.6f} SOL {token_info.symbol}..."); self.token_states[mint_str] = TokenState.BUYING
            buyer_result = await self.buyer.execute(token_info=token_info) # Assign result
            if buyer_result.success:
                logger.info(f"OK Buy {token_info.symbol}. Tx: {buyer_result.tx_signature}. Px: {buyer_result.price:.9f}. Amt: {buyer_result.amount}")
                self._log_trade("buy", token_info, buyer_result.price, buyer_result.amount, buyer_result.tx_signature)
                self.traded_mints_session.add(token_info.mint)
                self.token_buy_results[mint_str] = buyer_result # Use the result here
                self.token_states[mint_str] = TokenState.ON_BONDING_CURVE; logger.info(f"Token {token_info.symbol} ({mint_str[:6]}) monitoring on curve.")
            else:
                logger.error(f"FAIL Buy {token_info.symbol}: {buyer_result.error_message}"); self.token_states[mint_str] = TokenState.BUY_FAILED
                if self.cleanup_mode == "on_fail": logger.info(f"Cleanup fail buy {mint_str[:6]}"); await handle_cleanup_after_failure(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
        except asyncio.CancelledError: logger.warning(f"Handle task {mint_str[:6]} cancelled.");
        except Exception as e: logger.error(f"Error handle new {token_info.symbol}: {e}", exc_info=True); self.token_states[mint_str] = TokenState.MONITORING_ERROR


    # --- _monitor_active_trades Method (Full Implementation) ---
    async def _monitor_active_trades(self):
        logger.info("Starting active trade monitoring loop...")
        await asyncio.sleep(15)
        while True:
            try:
                active_mints = list(self.token_states.keys())
                if not active_mints: logger.debug("No active tokens."); await asyncio.sleep(15); continue
                monitor_tasks = []; mints_in_cycle = []
                for mint_str in active_mints:
                    current_state = self.token_states.get(mint_str); token_info = self.get_stored_token_info(mint_str); initial_trade_result = self.get_stored_buy_result(mint_str)
                    if not current_state: continue
                    if not token_info: logger.error(f"Missing info {mint_str}. Remove."); self.token_states.pop(mint_str, None); self.token_infos.pop(mint_str, None); self.token_buy_results.pop(mint_str, None); continue
                    if current_state in [TokenState.ON_BONDING_CURVE, TokenState.ON_RAYDIUM, TokenState.SELLING_CURVE, TokenState.SELLING_RAYDIUM] and not initial_trade_result: logger.error(f"Missing buy result {mint_str} state {current_state}. Remove."); self.token_states.pop(mint_str, None); self.token_infos.pop(mint_str, None); self.token_buy_results.pop(mint_str, None); continue
                    mints_in_cycle.append(mint_str[:6]); task_name = f"Mon_{current_state[:4]}_{mint_str[:6]}"
                    if current_state == TokenState.ON_BONDING_CURVE: monitor_tasks.append(asyncio.create_task(self._monitor_bonding_curve_token(token_info, initial_trade_result), name=task_name))
                    elif current_state == TokenState.ON_RAYDIUM: monitor_tasks.append(asyncio.create_task(self._monitor_raydium_token(token_info, initial_trade_result), name=task_name))
                    elif current_state in [TokenState.SELLING_CURVE, TokenState.SELLING_RAYDIUM]: logger.debug(f"Token {mint_str[:6]} in selling state {current_state}.")
                    elif current_state not in [TokenState.DETECTED, TokenState.BUYING]: logger.debug(f"Token {mint_str[:6]} in terminal state {current_state}. Await cleanup.")
                if monitor_tasks:
                    logger.debug(f"Running {len(monitor_tasks)} monitor tasks: {mints_in_cycle}"); results = await asyncio.gather(*monitor_tasks, return_exceptions=True)
                    for i, result in enumerate(results):
                        if isinstance(result, Exception): task_name = monitor_tasks[i].get_name(); logger.error(f"Error monitor task '{task_name}': {result}", exc_info=result)
                mints_to_remove = [m for m, s in list(self.token_states.items()) if s in [TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.BUY_FAILED, TokenState.RUGGED, TokenState.MONITORING_ERROR, TokenState.SELL_CURVE_FAILED, TokenState.SELL_RAYDIUM_FAILED]]
                if mints_to_remove: logger.info(f"Clean {len(mints_to_remove)} tokens: {[m[:6] for m in mints_to_remove]}"); [self.token_states.pop(m, None) or self.token_infos.pop(m, None) or self.token_buy_results.pop(m, None) for m in mints_to_remove]
                await asyncio.sleep(10)
            except asyncio.CancelledError: logger.info("Monitor loop cancelled."); break
            except Exception as e: logger.error(f"Error in monitor loop: {e}", exc_info=True); await asyncio.sleep(30)
        logger.info("Monitor loop finished.")


    # --- _monitor_bonding_curve_token Method (Initialized vars, Corrected commitment) ---
    async def _monitor_bonding_curve_token(self, token_info: TokenInfo, initial_trade_result: TradeResult):
        mint_str = str(token_info.mint); current_monitor_state = self.token_states.get(mint_str)
        if current_monitor_state != TokenState.ON_BONDING_CURVE: return
        logger.debug(f"Monitoring curve {token_info.symbol} ({mint_str[:6]})")
        sell_reason: Optional[str] = None; rug_reason: Optional[str] = None
        current_price: Optional[float] = None; stop_loss_price: Optional[float] = None
        try:
            current_curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(token_info.bonding_curve, commitment=Confirmed) # Use Enum
            if not current_curve_state: logger.warning(f"No curve state {mint_str[:6]}."); return
            current_sol_reserves = getattr(current_curve_state, 'virtual_sol_reserves', 0)
            if current_sol_reserves >= self.SOL_THRESHOLD_FOR_RAYDIUM: logger.info(f"*** {token_info.symbol} ({mint_str[:6]}) hit Raydium threshold. Transition. ***"); self.token_states[mint_str] = TokenState.ON_RAYDIUM; return
            if not self.marry_mode_flag:
                if self.rug_check_price_drop or self.rug_check_liquidity_drop: rug_reason = await self._check_post_buy_conditions(token_info, initial_trade_result, current_curve_state);
                if rug_reason: sell_reason = f"Rug Check: {rug_reason}";
                if not sell_reason:
                    try:
                        current_price = current_curve_state.calculate_price()
                        if initial_trade_result.price is not None and initial_trade_result.price > 0 and current_price is not None:
                             stop_loss_price = initial_trade_result.price * (1 - self.sell_stoploss_threshold)
                             if current_price < stop_loss_price: sell_reason = f"Stop-Loss ({current_price:.9f} < {stop_loss_price:.9f})"
                        elif current_price is None: logger.warning(f"Could not calc current price SL {mint_str[:6]}.")
                        else: logger.warning(f"Cannot calc SL {mint_str[:6]}: Invalid buy price ({initial_trade_result.price}).")
                    except (ValueError, TypeError) as price_e: logger.warning(f"Error calc price SL {mint_str[:6]}: {price_e}")
                if sell_reason:
                    logger.warning(f"*** Sell trigger curve {token_info.symbol}: {sell_reason} ***")
                    if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE:
                        self.token_states[mint_str] = TokenState.SELLING_CURVE; logger.info(f"Execute curve sell {mint_str[:6]}...")
                        seller_result = await self.seller.execute(token_info=token_info, buyer_result=initial_trade_result)
                        if seller_result.success: logger.info(f"OK Sold curve {token_info.symbol}: {sell_reason}. Tx: {seller_result.tx_signature}"); self.token_states[mint_str] = TokenState.SOLD_CURVE; self._log_trade("sell_curve", token_info, seller_result.price, seller_result.amount, seller_result.tx_signature);
                        if self.cleanup_mode == "after_sell": logger.info(f"Cleanup sold token {mint_str[:6]}"); await handle_cleanup_after_sell(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
                        else: logger.error(f"FAIL Sell curve {token_info.symbol}: {seller_result.error_message}"); self.token_states[mint_str] = TokenState.SELL_CURVE_FAILED
                    else: logger.warning(f"Sell trigger {mint_str[:6]} but state changed. Abort.")
        except asyncio.CancelledError: logger.warning(f"Monitor task curve {mint_str[:6]} cancelled.")
        except Exception as e: logger.error(f"Error monitor curve {mint_str}: {e}", exc_info=True); self.token_states[mint_str] = TokenState.MONITORING_ERROR


    # --- _monitor_raydium_token Method (Initialized vars, Corrected commitment) ---
    async def _monitor_raydium_token(self, token_info: TokenInfo, initial_trade_result: TradeResult):
        mint_str = str(token_info.mint); current_monitor_state = self.token_states.get(mint_str)
        if current_monitor_state != TokenState.ON_RAYDIUM: return
        logger.debug(f"Monitoring Raydium/DEX {token_info.symbol} ({mint_str[:6]})")
        sell_reason: Optional[str] = None; raydium_price: Optional[float] = None
        profit_target_price: Optional[float] = None; stop_loss_price: Optional[float] = None
        try:
            raydium_data = await get_raydium_pool_data(mint_str)
            if raydium_data is None: logger.warning(f"No DEX data {mint_str[:6]}."); return
            raydium_price = raydium_data.get("price")
            if raydium_price is None or not isinstance(raydium_price, float): logger.warning(f"Invalid price {mint_str[:6]}."); return
            # Apply Sell Logic
            if initial_trade_result.price is not None and initial_trade_result.price > 0:
                 profit_target_price = initial_trade_result.price * (1 + self.sell_profit_threshold); stop_loss_price = initial_trade_result.price * (1 - self.sell_stoploss_threshold);
                 logger.debug(f"{mint_str[:6]}: DEX Px={raydium_price:.9f}, TP={profit_target_price:.9f}, SL={stop_loss_price:.9f}")
                 if raydium_price >= profit_target_price: sell_reason = f"DEX Profit target ({raydium_price:.9f} >= {profit_target_price:.9f})"
                 elif raydium_price < stop_loss_price: sell_reason = f"DEX Stop-loss ({raydium_price:.9f} < {stop_loss_price:.9f})"
            else: logger.warning(f"Cannot calc DEX TP/SL {mint_str[:6]}: Invalid buy price.")
            # Trigger Raydium Sell
            if sell_reason:
                logger.warning(f"*** Stage 2 Sell trigger DEX {token_info.symbol}: {sell_reason} ***")
                if self.token_states.get(mint_str) == TokenState.ON_RAYDIUM:
                    self.token_states[mint_str] = TokenState.SELLING_RAYDIUM; logger.info(f"Execute Raydium sell {mint_str[:6]} (placeholder)...")
                    user_ata = Wallet.get_associated_token_address(self.wallet.pubkey, token_info.mint); amount_to_sell_lamports = 0
                    try:
                         balance_response: Optional[TokenAmount] = await self.solana_client.get_token_account_balance(user_ata, commitment=Confirmed) # Use Enum
                         if balance_response and hasattr(balance_response, 'amount') and isinstance(balance_response.amount, str) and balance_response.amount.isdigit():
                              amount_to_sell_lamports = int(balance_response.amount); logger.info(f"Balance for Raydium sell: {amount_to_sell_lamports} lamports")
                         else: logger.error(f"No valid balance {user_ata}")
                    except Exception as balance_e: logger.error(f"Error fetching balance: {balance_e}")
                    if amount_to_sell_lamports > 0:
                        raydium_seller = RaydiumSeller(self.solana_client, self.wallet, slippage_bps=int(self.sell_slippage * 10000))
                        sell_result = await raydium_seller.execute(token_info, amount_to_sell_lamports)
                        if sell_result.success: logger.info(f"OK Sold DEX {token_info.symbol}. Tx: {sell_result.tx_signature}. Est Px: {sell_result.price:.9f}"); self.token_states[mint_str] = TokenState.SOLD_RAYDIUM; self._log_trade("sell_raydium", token_info, sell_result.price, sell_result.amount, sell_result.tx_signature);
                        if self.cleanup_mode == "after_sell": await handle_cleanup_after_sell(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
                        else: logger.error(f"FAIL Raydium sell {token_info.symbol}: {sell_result.error_message}"); self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED
                    else: logger.warning(f"Raydium sell trigger {mint_str[:6]} but balance 0/fail."); self.token_states[mint_str] = TokenState.ON_RAYDIUM
                else: logger.warning(f"Raydium sell trigger {mint_str[:6]} but state changed. Abort.")
        except asyncio.CancelledError: logger.warning(f"Monitor task Raydium {mint_str[:6]} cancelled.")
        except Exception as e: logger.error(f"Error monitoring Raydium {mint_str}: {e}", exc_info=True); self.token_states[mint_str] = TokenState.MONITORING_ERROR


    # --- Rug Check Methods (Corrected TokenAmount access, Commitment Enum) ---
    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        logger.info(f"Checking creator holding {token_info.symbol}...")
        try:
            mint_pk = token_info.mint
            supply_resp: Optional[TokenAmount] = await self.solana_client.get_token_supply(mint_pk, commitment=Confirmed) # Use Enum
            if supply_resp is None or not hasattr(supply_resp, 'amount') or not isinstance(supply_resp.amount, str) or not supply_resp.amount.isdigit(): logger.warning(f"No valid supply {mint_pk}. Skip check (FAIL OPEN)."); return True
            try: total_supply = int(supply_resp.amount)
            except (ValueError, TypeError): logger.warning(f"Cannot parse supply '{supply_resp.amount}'. Skip check (FAIL OPEN)."); return True
            if total_supply <= 0: logger.warning("Total supply <= 0. Skip check."); return True

            creator_ata = Wallet.get_associated_token_address(mint=mint_pk, owner=token_info.user)
            balance_resp: Optional[TokenAmount] = await self.solana_client.get_token_account_balance(creator_ata, commitment=Confirmed) # Use Enum
            creator_balance = 0
            if balance_resp is not None and hasattr(balance_resp, 'amount') and isinstance(balance_resp.amount, str) and balance_resp.amount.isdigit():
                try: creator_balance = int(balance_resp.amount)
                except (ValueError, TypeError): logger.warning(f"Cannot parse creator balance '{balance_resp.amount}'. Assume 0.")

            creator_percentage = 0.0 if creator_balance <= 0 else creator_balance / total_supply
            logger.info(f"Creator {token_info.user} holds {creator_balance}/{total_supply} ({creator_percentage:.2%})")
            if creator_percentage > self.rug_max_creator_hold_pct: logger.warning(f"FAIL creator check: {creator_percentage:.2%} > {self.rug_max_creator_hold_pct:.2%}"); return False
            else: logger.info("Creator check PASSED."); return True
        except Exception as e: logger.error(f"Error creator check: {e}", exc_info=True); return True

    async def _check_post_buy_conditions(self, token_info: TokenInfo, initial_trade_result: TradeResult, current_curve_state: Optional[BondingCurveState] = None) -> Optional[str]:
        if not (self.rug_check_price_drop or self.rug_check_liquidity_drop): return None
        logger.debug(f"Checking post-buy conditions {token_info.symbol}")
        try:
            if current_curve_state is None: current_curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve, commitment=Confirmed) # Use Enum
            if not current_curve_state: logger.warning(f"No state post-buy {token_info.symbol}."); return None
            original_buy_price = initial_trade_result.price; initial_sol_liq = initial_trade_result.initial_sol_liquidity
            # Price Drop Check
            if self.rug_check_price_drop:
                if original_buy_price is not None and original_buy_price > 1e-18:
                    try: current_price = current_curve_state.calculate_price()
                    except Exception: current_price = None
                    if current_price is not None and current_price < original_buy_price: price_drop_pct = (original_buy_price - current_price) / original_buy_price;
                    if price_drop_pct > self.rug_price_drop_pct: return f"Price drop {price_drop_pct:.1%}"
                else: logger.debug(f"Skip post-buy price check: invalid initial price")
            # Liquidity Drop Check
            if self.rug_check_liquidity_drop:
                 if initial_sol_liq is not None and initial_sol_liq > 0:
                      current_sol_reserves = getattr(current_curve_state, 'virtual_sol_reserves', None)
                      if current_sol_reserves is not None:
                           current_sol_int = int(current_sol_reserves); initial_sol_int = int(initial_sol_liq)
                           if current_sol_int < initial_sol_int: liquidity_drop_pct = (initial_sol_int - current_sol_int) / initial_sol_int;
                           if liquidity_drop_pct > self.rug_liquidity_drop_pct: return f"Liquidity drop {liquidity_drop_pct:.1%}"
                      else: logger.warning("No virtual_sol_reserves for post-buy liq check.")
                 else: logger.debug(f"Skip post-buy liq check: invalid initial liq")
            return None
        except Exception as e: logger.error(f"Error post-buy check {token_info.symbol}: {e}", exc_info=True); return None

    # --- Utility Methods (Marked static, corrected datetime) ---
    @staticmethod
    async def _save_token_info(token_info: TokenInfo) -> None:
        os.makedirs("trades", exist_ok=True); file_name = os.path.join("trades", f"{str(token_info.mint)}.json")
        try:
             if dataclasses.is_dataclass(token_info): data_to_save = dataclasses.asdict(token_info)
             else: data_to_save = {k: v for k, v in vars(token_info).items() if not k.startswith('_')}
             for key, value in data_to_save.items():
                  if isinstance(value, Pubkey): data_to_save[key] = str(value)
             with open(file_name, "w", encoding="utf-8") as file_handle: # Corrected var name
                  json.dump(data_to_save, file_handle, indent=2)
             logger.debug(f"Token info saved: {file_name}")
        except IOError as e: logger.error(f"Failed save token info {file_name}: {e}")
        except TypeError as e: logger.error(f"Failed to serialize TokenInfo: {e}")

    @staticmethod
    def _log_trade(action: str, token_info: TokenInfo, price: Optional[float], amount: Optional[float], tx_hash: Optional[str]) -> None:
        os.makedirs("trades", exist_ok=True)
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(), # Use timezone aware
            "action": action, "token_address": str(token_info.mint), "symbol": token_info.symbol,
            "price_sol_per_token": price, "token_amount": amount, "tx_hash": str(tx_hash)
        }
        try:
            with open("trades/trades.log", "a", encoding="utf-8") as log_file: # Corrected var name
                 log_file.write(json.dumps(log_entry) + "\n")
        except IOError as e: logger.error(f"Failed write trade log: {e}")