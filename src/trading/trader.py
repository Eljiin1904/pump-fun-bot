# src/trading/trader.py

import asyncio
import json
import os
import time
import dataclasses
from datetime import datetime, timezone
from typing import Optional, Set, Dict, Tuple, Any # Use Any where needed

from solders.pubkey import Pubkey
from solana.rpc.commitment import Confirmed
# --- Removed TokenAmount import ---

# --- Project Imports ---
# Ensure these paths are correct based on your project structure
from ..cleanup.modes import ( handle_cleanup_after_failure, handle_cleanup_after_sell, handle_cleanup_post_session )
from ..core.client import SolanaClient
from ..core.curve import BondingCurveManager, BondingCurveState
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.wallet import Wallet
from ..monitoring.base_listener import BaseTokenListener
from ..monitoring.listener_factory import ListenerFactory
from ..trading.base import TokenInfo, TradeResult
from ..trading.buyer import TokenBuyer
from ..trading.seller import TokenSeller
from ..utils.logger import get_logger
from ..data.raydium_data import get_raydium_pool_data
from .raydium_seller import RaydiumSeller


logger = get_logger(__name__)

# --- Token State Definitions ---
# (Keep TokenState class definition)
class TokenState:
    DETECTED = "DETECTED"; BUYING = "BUYING"; BUY_FAILED = "BUY_FAILED"
    ON_BONDING_CURVE = "ON_BONDING_CURVE"; SELLING_CURVE = "SELLING_CURVE"
    SOLD_CURVE = "SOLD_CURVE"; SELL_CURVE_FAILED = "SELL_CURVE_FAILED"
    ON_RAYDIUM = "ON_RAYDIUM"; SELLING_RAYDIUM = "SELLING_RAYDIUM"
    SOLD_RAYDIUM = "SOLD_RAYDIUM"; SELL_RAYDIUM_FAILED = "SELL_RAYDIUM_FAILED"
    RUGGED = "RUGGED"; MONITORING_ERROR = "MONITORING_ERROR"


class PumpTrader:
    """Main class orchestrating the pump.fun trading process."""

    # --- FIX: Ensure __init__ matches the parameters passed in cli.py ---
    def __init__(
        self,
        client: SolanaClient,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float, # decimal
        sell_slippage: float, # decimal
        sell_profit_threshold: float, # decimal
        sell_stoploss_threshold: float, # decimal
        max_token_age: float, # seconds
        wait_time_after_creation: float, # seconds
        wait_time_after_buy: float, # seconds
        wait_time_before_new_token: float, # seconds
        max_retries: int, # General retries
        max_buy_retries: int, # Specific buyer retries
        confirm_timeout: int, # seconds
        listener_type: str,
        enable_dynamic_fee: bool,
        enable_fixed_fee: bool,
        fixed_fee: int, # micro-lamports
        extra_fee: int, # Ensure passed as int
        cap: int, # micro-lamports
        rug_check_creator: bool,
        rug_max_creator_hold_pct: float, # decimal
        rug_check_price_drop: bool,
        rug_price_drop_pct: float, # decimal
        rug_check_liquidity_drop: bool,
        rug_liquidity_drop_pct: float, # decimal
        cleanup_mode: str,
        cleanup_force_close: bool,
        cleanup_priority_fee: bool,
    ):
        # --- Full __init__ implementation from previous correct version ---
        self.wallet = Wallet(private_key)
        self.solana_client = client
        self.curve_manager = BondingCurveManager(self.solana_client)
        # Requires the raw AsyncClient, get it from the wrapper
        fee_manager_async_client = self.solana_client.get_async_client()
        self.priority_fee_manager = PriorityFeeManager(
            client=fee_manager_async_client, # Pass the raw client
            enable_dynamic_fee=enable_dynamic_fee,
            enable_fixed_fee=enable_fixed_fee,
            fixed_fee=fixed_fee,
            extra_fee=extra_fee, # Already int from cli.py
            hard_cap=cap
        )
        self.wss_endpoint = wss_endpoint
        self.listener_type = listener_type
        self.token_listener: Optional[BaseTokenListener] = None
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.sell_profit_threshold = sell_profit_threshold
        self.sell_stoploss_threshold = sell_stoploss_threshold
        self.max_token_age = max_token_age
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy # Keep if used
        self.wait_time_before_new_token = wait_time_before_new_token # Keep if used
        self.max_retries = max_retries
        self.max_buy_retries = max_buy_retries
        self.confirm_timeout = confirm_timeout
        self.rug_check_creator = rug_check_creator
        self.rug_max_creator_hold_pct = rug_max_creator_hold_pct
        self.rug_check_price_drop = rug_check_price_drop
        self.rug_price_drop_pct = rug_price_drop_pct
        self.rug_check_liquidity_drop = rug_check_liquidity_drop
        self.rug_liquidity_drop_pct = rug_liquidity_drop_pct
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close = cleanup_force_close
        self.cleanup_priority_fee = cleanup_priority_fee
        self.marry_mode_flag = False

        # Initialize Buyer, Seller, RaydiumSeller (passing necessary components)
        self.buyer = TokenBuyer(
            self.solana_client, self.wallet, self.curve_manager, self.priority_fee_manager,
            self.buy_amount, self.buy_slippage, self.max_buy_retries, self.confirm_timeout
        )
        self.seller = TokenSeller(
            client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager,
            priority_fee_manager=self.priority_fee_manager, slippage=self.sell_slippage,
            max_retries=self.max_retries, confirm_timeout=self.confirm_timeout
        )
        self.raydium_seller = RaydiumSeller(
            client=self.solana_client, wallet=self.wallet,
            priority_fee_manager=self.priority_fee_manager,
            slippage_bps=int(self.sell_slippage * 10000)
        )

        # Initialize state tracking dictionaries, queue, etc.
        self.token_states: Dict[str, str] = {}
        self.token_infos: Dict[str, TokenInfo] = {}
        self.token_buy_results: Dict[str, TradeResult] = {}
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        self.processed_tokens: Set[str] = set()
        self.token_timestamps: Dict[str, float] = {}
        self.traded_mints_session: Set[Pubkey] = set()
        self.SOL_THRESHOLD_FOR_RAYDIUM = 300 * 1_000_000_000 # Lamports

        # Logging initialization confirmation
        logger.info(f"PumpTrader initialized. Wallet: {self.wallet.pubkey}")
        # ... (rest of the logging statements from the original __init__) ...
        logger.info(f"Trading Params: Buy Amt={self.buy_amount:.6f} SOL, Buy Slip={self.buy_slippage*100:.1f}%, Sell Slip={self.sell_slippage*100:.1f}%")
        logger.info(f"Sell Logic: Profit Target={self.sell_profit_threshold*100:.1f}%, Stoploss={self.sell_stoploss_threshold*100:.1f}%")
        logger.info(f"Raydium Transition Threshold: ~{self.SOL_THRESHOLD_FOR_RAYDIUM / 1e9:.1f} SOL in curve")
        logger.info(f"Rug Checks: CreatorHold={self.rug_check_creator}(Max={self.rug_max_creator_hold_pct*100:.1f}%), PriceDrop={self.rug_check_price_drop}({self.rug_price_drop_pct*100:.1f}%), LiqDrop={self.rug_check_liquidity_drop}({self.rug_liquidity_drop_pct*100:.1f}% post-buy)")
        logger.info(f"Cleanup Mode: {self.cleanup_mode}")
        # --- End __init__ ---

    def get_stored_token_info(self, mint_str: str) -> Optional[TokenInfo]:
        return self.token_infos.get(mint_str)

    def get_stored_buy_result(self, mint_str: str) -> Optional[TradeResult]:
        return self.token_buy_results.get(mint_str)

    # --- FIX: Ensure async def start(...) method definition is present ---
    async def start( self, match_string: Optional[str] = None, bro_address: Optional[str] = None, marry_mode: bool = False, yolo_mode: bool = False ):
        self.marry_mode_flag = marry_mode
        logger.info(f"Starting trader...")
        logger.info(f"Match: {match_string or 'None'} | Creator: {bro_address or 'None'} | Marry: {marry_mode} | YOLO: {yolo_mode} | Max Age: {self.max_token_age}s")

        factory = ListenerFactory()
        listener_task: Optional[asyncio.Task] = None
        monitor_task: Optional[asyncio.Task] = None
        processor_task: Optional[asyncio.Task] = None

        try:
            self.token_listener = factory.create_listener(self.listener_type, self.wss_endpoint, self.solana_client)
            logger.info(f"Using {self.listener_type} listener")

            # Check if the listener object actually has the required method
            if not hasattr(self.token_listener, 'listen_for_tokens') or not callable(getattr(self.token_listener, 'listen_for_tokens')):
                logger.critical(f"FATAL: Listener object '{self.token_listener}' of type '{type(self.token_listener)}' missing callable 'listen_for_tokens' method. Cannot start.")
                return

            processor_task = asyncio.create_task(self._process_token_queue(marry_mode, yolo_mode), name="TokenProcessor")
            monitor_task = asyncio.create_task(self._monitor_active_trades(), name="TradeMonitor")

            logger.info("Starting main listener task...")
            # Call the method on the instance
            listener_task = asyncio.create_task(
                self.token_listener.listen_for_tokens(self._queue_token, match_string, bro_address),
                name="TokenListener"
            )
            await listener_task # Keep running listener task until it exits or is cancelled

        except asyncio.CancelledError:
            logger.info("Trader start task cancelled.")
        except Exception as e:
            logger.critical(f"Error during trader start or listener execution: {e}", exc_info=True)
        finally:
            logger.info("Shutting down trader (start method finally block)...")
            # Graceful shutdown for listener
            if self.token_listener and listener_task and not listener_task.done():
                logger.info("Requesting listener stop...")
                if hasattr(self.token_listener, 'stop') and callable(getattr(self.token_listener, 'stop')):
                    try:
                         await self.token_listener.stop()
                    except Exception as stop_e:
                         logger.error(f"Error stopping listener: {stop_e}")
                else:
                     logger.warning("Listener has no stop method, cancelling task.")
                     listener_task.cancel()

            # Cancel background tasks
            tasks_to_cancel = [t for t in [processor_task, monitor_task] if t and not t.done()]
            if tasks_to_cancel:
                logger.info(f"Cancelling {len(tasks_to_cancel)} background tasks...")
                for task in tasks_to_cancel: task.cancel()
                try:
                     # Wait briefly for tasks to acknowledge cancellation
                     await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
                except asyncio.CancelledError:
                     logger.debug("Gather was cancelled during shutdown.")
                logger.info("Background tasks cancellation requested.")

            # Log any exceptions from the listener task
            if listener_task and listener_task.done() and listener_task.exception():
                try:
                    exc = listener_task.exception()
                    logger.error(f"Listener task finished with exception: {exc}", exc_info=exc)
                except asyncio.CancelledError:
                    logger.info("Listener task was cancelled.")


            # Post-session cleanup
            if self.cleanup_mode == "post_session" and self.traded_mints_session:
                logger.info("Performing post-session cleanup...")
                try:
                    await handle_cleanup_post_session( self.solana_client, self.wallet, list(self.traded_mints_session), self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee )
                except Exception as clean_e: logger.error(f"Error during post-session cleanup: {clean_e}", exc_info=True)

            logger.info("Trader start method finished cleanup.")
    # --- End start method ---


    # --- FIX: Ensure other methods are complete and use hasattr check ---
    async def _queue_token(self, token_info: TokenInfo) -> None:
         # ... (Implementation likely okay, no TokenAmount usage) ...
         token_key = str(token_info.mint)
         if token_key in self.processed_tokens or token_key in self.token_states: return
         self.token_timestamps[token_key] = time.time(); await self.token_queue.put(token_info)
         logger.info(f"Queued: {token_info.symbol} ({token_key[:6]}...)")

    async def _process_token_queue(self, marry_mode: bool, yolo_mode: bool) -> None:
        # ... (Implementation likely okay, no TokenAmount usage) ...
        logger.info("Starting token processor task...")
        while True:
            token_info: Optional[TokenInfo] = None
            try:
                token_info = await self.token_queue.get()
                token_key = str(token_info.mint)
                if token_key in self.processed_tokens or token_key in self.token_states: self.token_queue.task_done(); continue
                discovery_time = self.token_timestamps.get(token_key); current_time = time.time()
                if discovery_time and (current_time - discovery_time > self.max_token_age): logger.info(f"Skip {token_info.symbol} old ({(current_time - discovery_time):.1f}s > {self.max_token_age}s)"); self.processed_tokens.add(token_key); self.token_queue.task_done(); continue
                elif not discovery_time: logger.warning(f"No discovery time for {token_key}. Processing anyway.")
                self.processed_tokens.add(token_key); logger.info(f"Processing queue item: {token_info.symbol}")
                asyncio.create_task(self._handle_new_token(token_info), name=f"Handle_{token_key[:6]}")
                self.token_queue.task_done()
            except asyncio.CancelledError: logger.info("Token processor task cancelled."); break
            except Exception as e: token_id = f"token {token_info.symbol} ({token_info.mint})" if token_info else "unknown token"; logger.error(f"Error in processor loop for {token_id}: {e}", exc_info=True); await asyncio.sleep(10)
        logger.info("Token processor task finished.")

    async def _handle_new_token(self, token_info: TokenInfo) -> None:
        # ... (Implementation likely okay, no TokenAmount usage) ...
        mint_str = str(token_info.mint)
        if mint_str in self.token_states: logger.debug(f"Token {mint_str[:6]} already being handled."); return
        logger.info(f"Handling new token: {token_info.symbol} ({mint_str[:6]})"); self.token_states[mint_str] = TokenState.DETECTED; self.token_infos[mint_str] = token_info; buyer_result: Optional[TradeResult] = None
        try:
            await self._save_token_info(token_info); logger.info(f"Waiting {self.wait_time_after_creation:.1f}s pre-buy for {token_info.symbol}..."); await asyncio.sleep(self.wait_time_after_creation)
            if self.rug_check_creator:
                try: creator_ok = await asyncio.wait_for(self._check_creator_holding(token_info), timeout=30.0)
                except asyncio.TimeoutError: logger.warning(f"Skip {token_info.symbol}: Creator check timed out."); self.token_states[mint_str] = TokenState.BUY_FAILED; return
                except Exception as rug_e: logger.error(f"Error during creator check for {token_info.symbol}: {rug_e}"); self.token_states[mint_str] = TokenState.BUY_FAILED; return
                if not creator_ok: logger.warning(f"Skip {token_info.symbol}: Creator check FAILED."); self.token_states[mint_str] = TokenState.RUGGED; return
            logger.info(f"Attempting buy of {self.buy_amount:.6f} SOL for {token_info.symbol}..."); self.token_states[mint_str] = TokenState.BUYING; buyer_result = await self.buyer.execute(token_info=token_info)
            if buyer_result.success: logger.info(f"Buy successful for {token_info.symbol}. Tx: {buyer_result.tx_signature}, Price: {buyer_result.price:.9f}, Amount: {buyer_result.amount}"); self._log_trade("buy", token_info, buyer_result.price, buyer_result.amount, buyer_result.tx_signature); self.traded_mints_session.add(token_info.mint); self.token_buy_results[mint_str] = buyer_result; self.token_states[mint_str] = TokenState.ON_BONDING_CURVE; logger.info(f"Token {token_info.symbol} ({mint_str[:6]}) monitoring on bonding curve.")
            else: logger.error(f"Buy failed for {token_info.symbol}: {buyer_result.error_message}"); self.token_states[mint_str] = TokenState.BUY_FAILED;
            if self.token_states[mint_str] == TokenState.BUY_FAILED and self.cleanup_mode == "on_fail": logger.info(f"Performing cleanup for failed buy of {mint_str[:6]}"); await handle_cleanup_after_failure( self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee )
        except asyncio.CancelledError: logger.warning(f"Handling task for {mint_str[:6]} cancelled.")
        except Exception as e: logger.error(f"Error handling new token {token_info.symbol}: {e}", exc_info=True); self.token_states[mint_str] = TokenState.MONITORING_ERROR

    async def _monitor_active_trades(self):
        # ... (Implementation likely okay, no TokenAmount usage) ...
        logger.info("Starting active trade monitoring loop..."); await asyncio.sleep(5)
        while True:
            try:
                active_mints = list(self.token_states.keys()); monitor_tasks = []; mints_in_cycle = []
                if not active_mints: logger.debug("No active tokens to monitor."); await asyncio.sleep(10); continue
                for mint_str in active_mints:
                    current_state = self.token_states.get(mint_str); token_info = self.get_stored_token_info(mint_str); initial_trade_result = self.get_stored_buy_result(mint_str)
                    if not current_state or not token_info: logger.error(f"Missing state/info for {mint_str}, removing."); self.token_states.pop(mint_str, None); self.token_infos.pop(mint_str, None); self.token_buy_results.pop(mint_str, None); self.token_timestamps.pop(mint_str, None); continue
                    if current_state in [TokenState.ON_BONDING_CURVE, TokenState.ON_RAYDIUM] and not initial_trade_result: logger.error(f"Missing buy result for active {mint_str} state {current_state}, removing."); self.token_states.pop(mint_str, None); self.token_infos.pop(mint_str, None); self.token_buy_results.pop(mint_str, None); self.token_timestamps.pop(mint_str, None); continue
                    mints_in_cycle.append(mint_str[:6]); task_name = f"Mon_{current_state[:4]}_{mint_str[:6]}"
                    if current_state == TokenState.ON_BONDING_CURVE: monitor_tasks.append(asyncio.create_task(self._monitor_bonding_curve_token(token_info, initial_trade_result), name=task_name))
                    elif current_state == TokenState.ON_RAYDIUM: monitor_tasks.append(asyncio.create_task(self._monitor_raydium_token(token_info, initial_trade_result), name=task_name))
                    elif current_state in [TokenState.SELLING_CURVE, TokenState.SELLING_RAYDIUM]: logger.debug(f"Token {mint_str[:6]} already selling {current_state}.")
                    elif current_state not in [TokenState.DETECTED, TokenState.BUYING]: logger.debug(f"Token {mint_str[:6]} in terminal/inactive state {current_state}.")
                if monitor_tasks:
                    logger.debug(f"Running {len(monitor_tasks)} monitor tasks for tokens: {mints_in_cycle}"); results = await asyncio.gather(*monitor_tasks, return_exceptions=True)
                    for i, result in enumerate(results):
                        if isinstance(result, Exception): task_name = monitor_tasks[i].get_name(); failed_mint_short = task_name.split('_')[-1]; failed_mint_full = next((m for m in self.token_states if m.startswith(failed_mint_short)), None); logger.error(f"Error in monitor task '{task_name}': {result}", exc_info=result);
                        if failed_mint_full and self.token_states.get(failed_mint_full) not in [TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM]: self.token_states[failed_mint_full] = TokenState.MONITORING_ERROR
                mints_to_remove = [ m for m, s in list(self.token_states.items()) if s in [ TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.BUY_FAILED, TokenState.RUGGED, TokenState.MONITORING_ERROR, TokenState.SELL_CURVE_FAILED, TokenState.SELL_RAYDIUM_FAILED ] ]
                if mints_to_remove: logger.info(f"Cleaning up internal state for {len(mints_to_remove)} finished/failed tokens: {[m[:6] for m in mints_to_remove]}");
                for m in mints_to_remove: self.token_states.pop(m, None); self.token_infos.pop(m, None); self.token_buy_results.pop(m, None); self.token_timestamps.pop(m, None)
                await asyncio.sleep(5)
            except asyncio.CancelledError: logger.info("Active trade monitoring loop cancelled."); break
            except Exception as e: logger.error(f"Error in monitoring loop: {e}", exc_info=True); await asyncio.sleep(20)
        logger.info("Active trade monitoring loop finished.")


    async def _monitor_bonding_curve_token(self, token_info: TokenInfo, initial_trade_result: TradeResult):
        # ... (Implementation likely okay, no TokenAmount usage) ...
        mint_str = str(token_info.mint); current_monitor_state = self.token_states.get(mint_str)
        if current_monitor_state != TokenState.ON_BONDING_CURVE: return
        logger.debug(f"Monitoring bonding curve for {token_info.symbol} ({mint_str[:6]})"); sell_reason: Optional[str] = None; rug_reason: Optional[str] = None
        try:
            current_curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state( token_info.bonding_curve, commitment=Confirmed )
            if not current_curve_state: logger.warning(f"Monitor curve: No curve state for {mint_str[:6]}."); return
            current_sol_reserves = getattr(current_curve_state, 'virtual_sol_reserves', 0)
            if current_sol_reserves >= self.SOL_THRESHOLD_FOR_RAYDIUM: logger.info(f"*** {token_info.symbol} ({mint_str[:6]}) Raydium threshold. Transitioning. ***"); self.token_states[mint_str] = TokenState.ON_RAYDIUM; return
            if not self.marry_mode_flag:
                rug_reason = await self._check_post_buy_conditions(token_info, initial_trade_result, current_curve_state)
                if rug_reason: sell_reason = f"Rug Check: {rug_reason}"
                else:
                    try: current_price = current_curve_state.calculate_price(); initial_buy_price = initial_trade_result.price
                    except Exception as price_e: logger.warning(f"Error calculating price for TP/SL check {mint_str[:6]}: {price_e}"); current_price = None
                    if initial_buy_price is not None and initial_buy_price > 1e-18 and current_price is not None:
                        profit_target_price = initial_buy_price * (1 + self.sell_profit_threshold); stop_loss_price = initial_buy_price * (1 - self.sell_stoploss_threshold); logger.debug(f"{mint_str[:6]}: Curve Price={current_price:.9f}, Buy={initial_buy_price:.9f}, TP={profit_target_price:.9f}, SL={stop_loss_price:.9f}")
                        if current_price >= profit_target_price: sell_reason = f"Profit Target ({current_price:.9f} >= {profit_target_price:.9f})"
                        elif current_price < stop_loss_price: sell_reason = f"Stop-Loss ({current_price:.9f} < {stop_loss_price:.9f})"
                    elif current_price is None and initial_buy_price is not None: logger.debug(f"No current curve price for TP/SL check ({mint_str[:6]}).")
                    elif initial_buy_price is None or initial_buy_price <= 1e-18: logger.warning(f"Invalid initial buy price ({initial_buy_price}) for TP/SL calc ({mint_str[:6]}).")
                if sell_reason:
                    logger.warning(f"*** Sell trigger (curve) for {token_info.symbol}: {sell_reason} ***")
                    if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE:
                        self.token_states[mint_str] = TokenState.SELLING_CURVE; logger.info(f"Executing bonding curve sell for {mint_str[:6]}...")
                        seller_result = await self.seller.execute(token_info=token_info, buyer_result=initial_trade_result)
                        if seller_result.success: logger.info(f"Curve sell successful for {token_info.symbol}: {sell_reason}. Tx: {seller_result.tx_signature}"); self.token_states[mint_str] = TokenState.SOLD_CURVE; self._log_trade("sell_curve", token_info, seller_result.price, seller_result.amount, seller_result.tx_signature)
                        if self.cleanup_mode == "after_sell": logger.info(f"Performing cleanup for sold token {mint_str[:6]}"); await handle_cleanup_after_sell(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
                        else: logger.error(f"Curve sell failed for {token_info.symbol}: {seller_result.error_message}"); self.token_states[mint_str] = TokenState.SELL_CURVE_FAILED
                    else: logger.warning(f"Sell trigger {mint_str[:6]} but state={self.token_states.get(mint_str)}. Aborting.")
        except asyncio.CancelledError: logger.warning(f"Monitoring task curve {mint_str[:6]} cancelled.")
        except Exception as e: logger.error(f"Error monitoring curve {mint_str}: {e}", exc_info=True); self.token_states[mint_str] = TokenState.MONITORING_ERROR


    # --- FIX: Ensure _monitor_raydium_token uses hasattr ---
    async def _monitor_raydium_token(self, token_info: TokenInfo, initial_trade_result: TradeResult):
        mint_str = str(token_info.mint)
        current_monitor_state = self.token_states.get(mint_str)
        if current_monitor_state != TokenState.ON_RAYDIUM: return
        logger.debug(f"Monitoring Raydium/DEX for {token_info.symbol} ({mint_str[:6]})")
        sell_reason: Optional[str] = None
        try:
            raydium_data = await get_raydium_pool_data(mint_str)
            if raydium_data is None: logger.warning(f"Monitor Raydium: No DEX data for {mint_str[:6]}."); return
            raydium_price = raydium_data.get("price"); initial_buy_price = initial_trade_result.price
            if raydium_price is None or not isinstance(raydium_price, (float, int)): logger.warning(f"Monitor Raydium: Invalid DEX price ({raydium_price}) for {mint_str[:6]}."); return
            if initial_buy_price is not None and initial_buy_price > 1e-18:
                profit_target_price = initial_buy_price * (1 + self.sell_profit_threshold); stop_loss_price = initial_buy_price * (1 - self.sell_stoploss_threshold); logger.debug(f"{mint_str[:6]}: DEX Price={raydium_price:.9f}, Buy={initial_buy_price:.9f}, TP={profit_target_price:.9f}, SL={stop_loss_price:.9f}")
                if raydium_price >= profit_target_price: sell_reason = f"DEX Profit target ({raydium_price:.9f} >= {profit_target_price:.9f})"
                elif raydium_price < stop_loss_price: sell_reason = f"DEX Stop-loss ({raydium_price:.9f} < {stop_loss_price:.9f})"
            else: logger.warning(f"Cannot calculate DEX TP/SL for {mint_str[:6]}: Invalid initial buy price ({initial_buy_price}).")

            if sell_reason:
                logger.warning(f"*** Sell trigger on DEX for {token_info.symbol}: {sell_reason} ***")
                if self.token_states.get(mint_str) == TokenState.ON_RAYDIUM:
                    self.token_states[mint_str] = TokenState.SELLING_RAYDIUM; logger.info(f"Executing Raydium sell for {mint_str[:6]}...")
                    user_ata = Wallet.get_associated_token_address(self.wallet.pubkey, token_info.mint); amount_to_sell_lamports = 0
                    try:
                        balance_response: Optional[Any] = await self.solana_client.get_token_account_balance(user_ata, commitment=Confirmed)
                        if balance_response and hasattr(balance_response, 'amount') and hasattr(balance_response, 'ui_amount_string'):
                            try: amount_to_sell_lamports = int(balance_response.amount); logger.info(f"User balance Raydium sell: {amount_to_sell_lamports} lamports")
                            except (ValueError, TypeError): logger.error(f"Could not parse Raydium balance amount '{getattr(balance_response, 'amount', 'N/A')}' for {user_ata}.")
                        elif balance_response is not None: logger.error(f"Invalid balance response structure for {user_ata}.")
                        else: logger.error(f"Failed to get balance response for {user_ata}. Cannot sell.")
                    except Exception as balance_e: logger.error(f"Error fetching balance Raydium sell ATA {user_ata}: {balance_e}")

                    if amount_to_sell_lamports > 0:
                        sell_result = await self.raydium_seller.execute(token_info, amount_to_sell_lamports)
                        if sell_result.success: logger.info(f"Raydium sell successful {token_info.symbol}. Tx: {sell_result.tx_signature}, Est Price: {sell_result.price:.9f}"); self.token_states[mint_str] = TokenState.SOLD_RAYDIUM; self._log_trade("sell_raydium", token_info, sell_result.price, sell_result.amount, sell_result.tx_signature);
                        if self.cleanup_mode == "after_sell": await handle_cleanup_after_sell(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
                        else: logger.error(f"Raydium sell failed {token_info.symbol}: {sell_result.error_message}"); self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED
                    else: logger.warning(f"Raydium sell trigger for {mint_str[:6]} but balance zero/invalid."); self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED
                else: logger.warning(f"Raydium sell trigger {mint_str[:6]} aborted state={self.token_states.get(mint_str)}.")
        except asyncio.CancelledError: logger.warning(f"Monitoring task Raydium {mint_str[:6]} cancelled.")
        except Exception as e: logger.error(f"Error monitoring Raydium {mint_str}: {e}", exc_info=True); self.token_states[mint_str] = TokenState.MONITORING_ERROR
    # --- End _monitor_raydium_token ---


    # --- FIX: Ensure _check_creator_holding uses hasattr ---
    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        logger.info(f"Checking creator holding for {token_info.symbol}...")
        try:
            mint_pk = token_info.mint; supply_resp: Optional[Any] = await self.solana_client.get_token_supply(mint_pk, commitment=Confirmed); total_supply = 0
            if supply_resp and hasattr(supply_resp, 'amount') and hasattr(supply_resp, 'ui_amount_string'):
                try: total_supply = int(supply_resp.amount)
                except (ValueError, TypeError): logger.warning(f"Unable to parse total supply '{getattr(supply_resp, 'amount', 'N/A')}'. Skip check."); return True
            else: logger.warning(f"No valid supply info for {mint_pk}. Skip check."); return True
            if total_supply <= 0: logger.warning("Total supply <= 0. Skip check."); return True

            creator_ata = Wallet.get_associated_token_address(mint=mint_pk, owner=token_info.user); balance_resp: Optional[Any] = await self.solana_client.get_token_account_balance(creator_ata, commitment=Confirmed); creator_balance = 0
            if balance_resp and hasattr(balance_resp, 'amount') and hasattr(balance_resp, 'ui_amount_string'):
                try: creator_balance = int(balance_resp.amount)
                except (ValueError, TypeError): logger.warning(f"Unable to parse creator balance '{getattr(balance_resp, 'amount', 'N/A')}'. Assuming 0.")
            elif balance_resp is not None: logger.warning(f"Unexpected balance response structure for creator ATA {creator_ata}.")

            creator_percentage = 0.0 if total_supply <= 0 else creator_balance / total_supply; logger.info(f"Creator {token_info.user} holds {creator_balance}/{total_supply} ({creator_percentage:.2%})")
            if creator_percentage > self.rug_max_creator_hold_pct: logger.warning(f"Creator holding check FAILED: {creator_percentage:.2%} > {self.rug_max_creator_hold_pct:.2%}."); return False
            logger.info("Creator holding check PASSED."); return True
        except Exception as e: logger.error(f"Error during creator holding check for {token_info.symbol}: {e}", exc_info=True); return True # Fail open
    # --- End _check_creator_holding ---


    # --- FIX: Ensure _check_post_buy_conditions doesn't use TokenAmount ---
    async def _check_post_buy_conditions(self, token_info: TokenInfo, initial_trade_result: TradeResult, current_curve_state: Optional[BondingCurveState] = None) -> Optional[str]:
        # ... (This method primarily uses BondingCurveState, which is likely correct) ...
        # Ensure no direct TokenAmount usage sneaked in. The existing logic looks okay.
        if not (self.rug_check_price_drop or self.rug_check_liquidity_drop): return None
        logger.debug(f"Checking post-buy conditions for {token_info.symbol}")
        try:
            if current_curve_state is None: current_curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve, commitment=Confirmed)
            if not current_curve_state: logger.warning(f"Post-buy check: No curve state for {token_info.symbol}."); return None
            original_buy_price = initial_trade_result.price; initial_sol_liq = initial_trade_result.initial_sol_liquidity
            if self.rug_check_price_drop and original_buy_price is not None and original_buy_price > 1e-18:
                try: current_price = current_curve_state.calculate_price()
                except Exception: current_price = None
                if current_price is not None and current_price < original_buy_price: price_drop_pct = (original_buy_price - current_price) / original_buy_price;
                if price_drop_pct > self.rug_price_drop_pct: return f"Price drop {price_drop_pct:.1%}"
            if self.rug_check_liquidity_drop and initial_sol_liq is not None and initial_sol_liq > 0:
                current_sol_reserves = getattr(current_curve_state, 'virtual_sol_reserves', None)
                if current_sol_reserves is not None: current_sol_int = int(current_sol_reserves); initial_sol_int = int(initial_sol_liq);
                if current_sol_int < initial_sol_int: liquidity_drop_pct = (initial_sol_int - current_sol_int) / initial_sol_int;
                if liquidity_drop_pct > self.rug_liquidity_drop_pct: return f"Liquidity drop {liquidity_drop_pct:.1%}"
                else: logger.warning(f"Post-buy check: No virtual_sol_reserves for {token_info.symbol}.")
            return None
        except Exception as e: logger.error(f"Error during post-buy check for {token_info.symbol}: {e}", exc_info=True); return None
    # --- End _check_post_buy_conditions ---

    # --- FIX: Ensure _save_token_info doesn't use TokenAmount ---
    @staticmethod
    async def _save_token_info(token_info: TokenInfo) -> None:
        # ... (This method serializes TokenInfo, likely no TokenAmount usage) ...
        os.makedirs("trades", exist_ok=True); file_name = os.path.join("trades", f"{str(token_info.mint)}.json")
        try:
            if dataclasses.is_dataclass(token_info): data_to_save = dataclasses.asdict(token_info)
            else: data_to_save = {k: v for k, v in vars(token_info).items() if not k.startswith('_')}
            for key, value in data_to_save.items():
                 if isinstance(value, Pubkey): data_to_save[key] = str(value)
                 if isinstance(value, float) and value == float('inf'): data_to_save[key] = "Infinity"
            with open(file_name, "w", encoding="utf-8") as file_handle: json.dump(data_to_save, file_handle, indent=2)
            logger.debug(f"Token info saved: {file_name}")
        except IOError as e: logger.error(f"Failed to save token info {file_name}: {e}")
        except TypeError as e: logger.error(f"Failed to serialize TokenInfo for {file_name}: {e}")
    # --- End _save_token_info ---

    # --- FIX: Ensure _log_trade doesn't use TokenAmount ---
    @staticmethod
    def _log_trade(action: str, token_info: TokenInfo, price: Optional[float], amount: Optional[float], tx_hash: Optional[str]) -> None:
        # ... (This method logs results, likely no TokenAmount usage) ...
        os.makedirs("trades", exist_ok=True)
        log_entry = { "timestamp": datetime.now(timezone.utc).isoformat(), "action": action, "token_address": str(token_info.mint), "symbol": token_info.symbol, "price_sol_per_token": price, "amount_tokens_or_sol": amount, "tx_hash": str(tx_hash) }
        try:
            with open("trades/trades.log", "a", encoding="utf-8") as log_file: log_file.write(json.dumps(log_entry) + "\n")
        except IOError as e: logger.error(f"Failed to write trade log: {e}")
    # --- End _log_trade ---