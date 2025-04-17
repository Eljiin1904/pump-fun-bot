# src/trading/trader.py

import asyncio
import json
import os
import time
import dataclasses
from datetime import datetime, timezone
# *** FIX: Removed unused import 'Tuple' ***
from typing import Optional, Set, Dict, Any, List

from solders.pubkey import Pubkey
from solana.rpc.commitment import Confirmed, Processed

# --- Project Imports ---
from ..cleanup.modes import ( handle_cleanup_after_failure, handle_cleanup_after_sell, handle_cleanup_post_session )
from ..core.client import SolanaClient
from ..core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL, DEFAULT_TOKEN_DECIMALS
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.wallet import Wallet
from ..monitoring.base_listener import BaseTokenListener
from ..monitoring.listener_factory import ListenerFactory
from ..trading.base import TokenInfo, TradeResult
from ..trading.buyer import TokenBuyer
from ..trading.seller import TokenSeller
from ..utils.logger import get_logger

# Conditional Raydium Imports
try:
    from ..data.raydium_data import get_raydium_pool_data
    from .raydium_seller import RaydiumSeller
    RAYDIUM_ENABLED = True
except ImportError:
    try: temp_logger = get_logger(__name__); temp_logger.warning("Raydium components not found, Raydium features disabled.")
    except: print("WARNING: Raydium components not found, Raydium features disabled (logger not init).")
    get_raydium_pool_data = None
    RaydiumSeller = None
    RAYDIUM_ENABLED = False

logger = get_logger(__name__)

# --- Token State Definitions ---
class TokenState:
    DETECTED = "DETECTED"; BUYING = "BUYING"; BUY_FAILED = "BUY_FAILED"
    ON_BONDING_CURVE = "ON_BONDING_CURVE"; SELLING_CURVE = "SELLING_CURVE"
    SOLD_CURVE = "SOLD_CURVE"; SELL_CURVE_FAILED = "SELL_CURVE_FAILED"
    ON_RAYDIUM = "ON_RAYDIUM"; SELLING_RAYDIUM = "SELLING_RAYDIUM"
    SOLD_RAYDIUM = "SOLD_RAYDIUM"; SELL_RAYDIUM_FAILED = "SELL_RAYDIUM_FAILED"
    RUGGED = "RUGGED"; MONITORING_ERROR = "MONITORING_ERROR"
    CLEANUP_FAILED = "CLEANUP_FAILED"


class PumpTrader:
    """Main class orchestrating the pump.fun trading process."""

    # __init__ remains the same as the previously corrected version
    def __init__(
        self,
        client: SolanaClient,
        priority_fee_manager: PriorityFeeManager,
        wallet: Wallet,
        wss_endpoint: str,
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
        rug_check_creator: bool,
        rug_max_creator_hold_pct: float,
        rug_check_price_drop: bool,
        rug_price_drop_pct: float,
        rug_check_liquidity_drop: bool,
        rug_liquidity_drop_pct: float,
        cleanup_mode: str,
        cleanup_force_close: bool,
        cleanup_priority_fee: bool,
        sol_threshold_for_raydium: float = 300.0,
    ):
        self.wallet = wallet
        self.solana_client = client
        self.priority_fee_manager = priority_fee_manager
        self.curve_manager = BondingCurveManager(self.solana_client)
        self.wss_endpoint = wss_endpoint
        self.listener_type = listener_type
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.sell_profit_threshold = sell_profit_threshold
        self.sell_stoploss_threshold = sell_stoploss_threshold
        self.max_token_age = max_token_age
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token
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
        self.SOL_THRESHOLD_FOR_RAYDIUM = int(sol_threshold_for_raydium * LAMPORTS_PER_SOL)
        self.marry_mode_flag = False
        self.buyer = TokenBuyer( client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager, priority_fee_manager=self.priority_fee_manager, amount_sol=self.buy_amount, slippage_decimal=self.buy_slippage, max_retries=self.max_buy_retries, confirm_timeout=self.confirm_timeout )
        self.seller = TokenSeller( client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager, priority_fee_manager=self.priority_fee_manager, slippage=self.sell_slippage, max_retries=self.max_retries, confirm_timeout=self.confirm_timeout )
        self.raydium_seller: Optional[RaydiumSeller] = None
        if RAYDIUM_ENABLED and RaydiumSeller: self.raydium_seller = RaydiumSeller( client=self.solana_client, wallet=self.wallet, priority_fee_manager=self.priority_fee_manager, slippage_bps=int(self.sell_slippage * 10000) )
        self.token_listener: Optional[BaseTokenListener] = None; self.token_states: Dict[str, str] = {}; self.token_infos: Dict[str, TokenInfo] = {}; self.token_buy_results: Dict[str, TradeResult] = {}; self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue(); self.processed_tokens: Set[str] = set(); self.token_timestamps: Dict[str, float] = {}; self.active_monitoring_tasks: Dict[str, asyncio.Task] = {}; self.traded_mints_session: Set[Pubkey] = set()
        logger.info(f"PumpTrader initialized. Wallet: {self.wallet.pubkey}")
        logger.info(f"Trading Params: Buy Amt={self.buy_amount:.6f} SOL, Buy Slip={self.buy_slippage*100:.1f}%, Sell Slip={self.sell_slippage*100:.1f}%")
        logger.info(f"Sell Logic: Profit Target={self.sell_profit_threshold*100:.1f}%, Stoploss={self.sell_stoploss_threshold*100:.1f}%")
        if RAYDIUM_ENABLED: logger.info(f"Raydium Transition Threshold: ~{self.SOL_THRESHOLD_FOR_RAYDIUM / 1e9:.1f} SOL in curve")
        else: logger.info("Raydium Transition: DISABLED")
        logger.info(f"Rug Checks: CreatorHold={self.rug_check_creator}(Max={self.rug_max_creator_hold_pct*100:.1f}%), PriceDrop={self.rug_check_price_drop}({self.rug_price_drop_pct*100:.1f}%), LiqDrop={self.rug_check_liquidity_drop}({self.rug_liquidity_drop_pct*100:.1f}% post-buy)")
        logger.info(f"Cleanup Mode: {self.cleanup_mode}")

    def get_stored_token_info(self, mint_str: str) -> Optional[TokenInfo]:
        return self.token_infos.get(mint_str)

    def get_stored_buy_result(self, mint_str: str) -> Optional[TradeResult]:
        return self.token_buy_results.get(mint_str)

    # --- start method (Corrected try/finally structure) ---
    async def start( self, match_string: Optional[str] = None, bro_address: Optional[str] = None, marry_mode: bool = False, yolo_mode: bool = False ):
        """Starts the listener and processing loops."""
        self.marry_mode_flag = marry_mode
        logger.info(f"Starting trader...")
        logger.info(f"Match: {match_string or 'None'} | Creator: {bro_address or 'None'} | Marry: {marry_mode} | YOLO: {yolo_mode} | Max Age: {self.max_token_age}s")

        factory = ListenerFactory()
        listener_task: Optional[asyncio.Task] = None
        monitor_task: Optional[asyncio.Task] = None
        processor_task: Optional[asyncio.Task] = None
        created_tasks: List[asyncio.Task] = []
        pending_tasks: Set[asyncio.Task] = set()

        try:
            self.token_listener = factory.create_listener(self.listener_type, self.wss_endpoint, self.solana_client)
            logger.info(f"Using {self.listener_type} listener")
            if not hasattr(self.token_listener, 'listen_for_tokens') or not callable(getattr(self.token_listener, 'listen_for_tokens')):
                logger.critical(f"FATAL: Listener object '{self.token_listener}' missing callable 'listen_for_tokens' method.")
                return

            processor_task = asyncio.create_task(self._process_token_queue(marry_mode, yolo_mode), name="TokenProcessor")
            created_tasks.append(processor_task)
            monitor_task = asyncio.create_task(self._monitor_active_trades(), name="TradeMonitor")
            created_tasks.append(monitor_task)
            logger.info("Starting main listener task...")
            listener_task = asyncio.create_task( self.token_listener.listen_for_tokens(self._queue_token, match_string, bro_address), name="TokenListener" )
            created_tasks.append(listener_task)

            if not created_tasks: logger.warning("No primary tasks were created."); return

            logger.debug(f"Awaiting first completion of {len(created_tasks)} primary tasks...")
            done_tasks, pending_tasks = await asyncio.wait(created_tasks, return_when=asyncio.FIRST_COMPLETED)

            for task in done_tasks:
                task_name = task.get_name() if hasattr(task, 'get_name') else "Unknown Task"
                try: task.result(); logger.info(f"Primary task {task_name} completed first.")
                except asyncio.CancelledError: logger.info(f"Primary task {task_name} was cancelled.")
                except Exception as task_exc: logger.error(f"Primary task {task_name} failed first: {task_exc}", exc_info=True)

        except asyncio.CancelledError: logger.info("Trader start task was cancelled externally.")
        except Exception as e: logger.critical(f"Error during trader startup or task management: {e}", exc_info=True)
        finally:
            logger.info("Shutting down trader (start method finally block)...")
            tasks_to_cancel = list(pending_tasks)
            tasks_to_cancel.extend([t for t in created_tasks if t and not t.done() and t not in pending_tasks])
            tasks_to_cancel = list(set(tasks_to_cancel))
            if tasks_to_cancel:
                logger.info(f"Cancelling {len(tasks_to_cancel)} primary background task(s)...")
                for task in tasks_to_cancel:
                    task_name = task.get_name() if hasattr(task, 'get_name') else "Unknown Task"
                    if not task.done(): logger.debug(f"Cancelling task: {task_name}"); task.cancel()
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True); logger.info("Primary background tasks cancellation complete.")
            else: logger.info("No running primary tasks needed cancellation.")
            if self.token_listener:
                logger.info("Requesting listener stop...");
                if hasattr(self.token_listener, 'stop') and callable(getattr(self.token_listener, 'stop')):
                    try: await asyncio.wait_for(self.token_listener.stop(), timeout=5.0); logger.info("Listener stopped.")
                    except asyncio.TimeoutError: logger.error("Timeout stopping listener.")
                    except Exception as stop_e: logger.error(f"Error stopping listener: {stop_e}", exc_info=True)
                else: logger.warning("Listener has no stop method.")
                if listener_task and not listener_task.done():
                     if not listener_task.cancelled(): listener_task.cancel()
                     await asyncio.gather(listener_task, return_exceptions=True)
            active_sub_tasks = list(self.active_monitoring_tasks.values());
            if active_sub_tasks:
                logger.info(f"Cancelling {len(active_sub_tasks)} active monitoring sub-tasks...")
                for task in active_sub_tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*active_sub_tasks, return_exceptions=True); logger.info("Monitoring sub-tasks cancellation complete."); self.active_monitoring_tasks.clear()
            if self.cleanup_mode == "post_session" and self.traded_mints_session:
                logger.info("Performing post-session cleanup...")
                try:
                    await handle_cleanup_post_session( self.solana_client, self.wallet, list(self.traded_mints_session), self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee )
                    logger.info("Post-session cleanup attempt finished.")
                except Exception as clean_e: logger.error(f"Error during post-session cleanup: {clean_e}", exc_info=True)
            logger.info("Trader start method finished cleanup process.")

    # --- Methods from _queue_token to _log_trade (Minor Fixes Applied) ---

    async def _queue_token(self, token_info: TokenInfo) -> None:
        token_key = str(token_info.mint)
        mint_pubkey = token_info.mint # Defined correctly now
        if token_key in self.processed_tokens or token_key in self.token_states or mint_pubkey in self.traded_mints_session: return
        self.token_timestamps[token_key] = time.time(); await self.token_queue.put(token_info)
        logger.info(f"Queued: {token_info.symbol} ({token_key[:6]}...)")

    async def _process_token_queue(self, marry_mode: bool, yolo_mode: bool) -> None:
        logger.info("Starting token processor task..."); currently_processing_marry: Optional[str] = None; active_yolo_tasks: Set[asyncio.Task] = set(); MAX_YOLO_CONCURRENCY = 5
        while True:
            token_info: Optional[TokenInfo] = None
            try:
                # *** FIX: Corrected Syntax on YOLO wait block ***
                while yolo_mode and len(active_yolo_tasks) >= MAX_YOLO_CONCURRENCY:
                    logger.debug(f"YOLO concurrency limit ({MAX_YOLO_CONCURRENCY}) reached, waiting...")
                    done, active_yolo_tasks = await asyncio.wait(active_yolo_tasks, return_when=asyncio.FIRST_COMPLETED)
                    # Process results/exceptions of completed tasks
                    for task in done:
                        if task.exception():
                            logger.error(f"YOLO Handling task {task.get_name()} failed", exc_info=task.exception())
                # *** END FIX ***

                if marry_mode and currently_processing_marry:
                    current_marry_state = self.token_states.get(currently_processing_marry);
                    terminal_states = [TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.BUY_FAILED, TokenState.RUGGED, TokenState.MONITORING_ERROR, TokenState.SELL_CURVE_FAILED, TokenState.SELL_RAYDIUM_FAILED, TokenState.CLEANUP_FAILED]
                    if current_marry_state and current_marry_state not in terminal_states: await asyncio.sleep(1); continue
                    else: logger.info(f"Marry mode: Previous token {currently_processing_marry[:6]} finished/cleared ({current_marry_state}). Ready for new token."); currently_processing_marry = None

                token_info = await self.token_queue.get(); token_key = str(token_info.mint); mint_pubkey = token_info.mint
                if token_key in self.processed_tokens or token_key in self.token_states or mint_pubkey in self.traded_mints_session: self.token_queue.task_done(); continue

                discovery_time = self.token_timestamps.get(token_key); current_time = time.time()
                if discovery_time and (current_time - discovery_time > self.max_token_age): logger.info(f"Skip {token_info.symbol} old ({(current_time - discovery_time):.1f}s > {self.max_token_age}s)"); self.processed_tokens.add(token_key); self.token_queue.task_done(); continue
                elif not discovery_time: logger.warning(f"No discovery time for {token_key}. Processing but setting time now."); self.token_timestamps[token_key] = current_time

                self.processed_tokens.add(token_key);
                if marry_mode: currently_processing_marry = token_key

                logger.info(f"Processing queue item: {token_info.symbol} ({token_key[:6]})")
                handle_task = asyncio.create_task(self._handle_new_token(token_info), name=f"Handle_{token_key[:6]}")

                if yolo_mode: active_yolo_tasks.add(handle_task); handle_task.add_done_callback(active_yolo_tasks.discard)
                else: await handle_task

                self.token_queue.task_done()
            except asyncio.CancelledError: logger.info("Token processor task cancelled."); break
            except Exception as e:
                token_id_str = f"token {token_info.symbol} ({token_info.mint})" if token_info else "unknown token"
                logger.error(f"Error in token processor loop for {token_id_str}: {e}", exc_info=True)
                if token_info : # Check if item was retrieved before error
                    try: self.token_queue.task_done()
                    except ValueError: pass
                await asyncio.sleep(5)

        if active_yolo_tasks: logger.info(f"Cancelling {len(active_yolo_tasks)} remaining YOLO handling tasks...");
        for task in active_yolo_tasks: task.cancel()
        if active_yolo_tasks: await asyncio.gather(*active_yolo_tasks, return_exceptions=True)
        logger.info("Token processor task finished.")

    async def _handle_new_token(self, token_info: TokenInfo) -> None:
        mint_str = str(token_info.mint)
        if mint_str in self.token_states: logger.debug(f"Token {mint_str[:6]} processing already initiated."); return
        logger.info(f"Handling new token: {token_info.symbol} ({mint_str[:6]})");
        self.token_states[mint_str] = TokenState.DETECTED; self.token_infos[mint_str] = token_info; buyer_result: Optional[TradeResult] = None
        try:
            await self._save_token_info(token_info);
            logger.info(f"Waiting {self.wait_time_after_creation:.1f}s pre-buy for {token_info.symbol}...");
            await asyncio.sleep(self.wait_time_after_creation)
            if self.token_states.get(mint_str) != TokenState.DETECTED: logger.warning(f"Token {mint_str[:6]} state changed during pre-buy wait. Aborting handle."); return

            if self.rug_check_creator:
                try: creator_ok = await asyncio.wait_for(self._check_creator_holding(token_info), timeout=30.0)
                except asyncio.TimeoutError: logger.warning(f"Skip {token_info.symbol}: Creator check timed out."); self.token_states[mint_str] = TokenState.BUY_FAILED; return
                except Exception as rug_e: logger.error(f"Error during creator check for {token_info.symbol}: {rug_e}"); self.token_states[mint_str] = TokenState.BUY_FAILED; return
                if not creator_ok: logger.warning(f"Skip {token_info.symbol}: Creator check FAILED."); self.token_states[mint_str] = TokenState.RUGGED; return

            if self.token_states.get(mint_str) != TokenState.DETECTED: logger.warning(f"Token {mint_str[:6]} state changed before buy. Aborting handle."); return

            logger.info(f"Attempting buy of {self.buy_amount:.6f} SOL for {token_info.symbol}...");
            self.token_states[mint_str] = TokenState.BUYING;
            buyer_result = await self.buyer.execute(token_info=token_info)

            if buyer_result.success and buyer_result.tx_signature:
                 logger.info(f"Buy successful for {token_info.symbol}. Tx: {buyer_result.tx_signature}, Price: {buyer_result.price:.9f}, Amount: {buyer_result.amount}")
                 self._log_trade("buy", token_info, buyer_result.price, buyer_result.amount, buyer_result.tx_signature)
                 self.traded_mints_session.add(token_info.mint)
                 self.token_buy_results[mint_str] = buyer_result
                 self.token_states[mint_str] = TokenState.ON_BONDING_CURVE
                 logger.info(f"Token {token_info.symbol} ({mint_str[:6]}) monitoring on bonding curve.")
            else:
                 logger.error(f"Buy failed for {token_info.symbol}: {buyer_result.error_message}")
                 self.token_states[mint_str] = TokenState.BUY_FAILED

            if self.token_states.get(mint_str) == TokenState.BUY_FAILED and self.cleanup_mode == "on_fail":
                 logger.info(f"Performing cleanup for failed buy of {mint_str[:6]}")
                 try:
                      # *** FIX: Remove unused assignment ***
                      await handle_cleanup_after_failure( self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee )
                      # if not clean_success: self.token_states[mint_str] = TokenState.CLEANUP_FAILED
                 except Exception as clean_e:
                      logger.error(f"Error during on_fail cleanup for {mint_str[:6]}: {clean_e}", exc_info=True)
                      self.token_states[mint_str] = TokenState.CLEANUP_FAILED

        except asyncio.CancelledError:
            logger.warning(f"Handling task for {mint_str[:6]} cancelled.")
            self.token_states[mint_str] = TokenState.MONITORING_ERROR
        except Exception as e:
            logger.error(f"Error handling new token {token_info.symbol}: {e}", exc_info=True)
            self.token_states[mint_str] = TokenState.MONITORING_ERROR


    async def _monitor_active_trades(self):
        logger.info("Starting active trade monitoring loop..."); await asyncio.sleep(5)
        while True:
            try:
                mints_to_monitor = {m: s for m, s in self.token_states.items() if s in [TokenState.ON_BONDING_CURVE, TokenState.ON_RAYDIUM]}
                if not mints_to_monitor: logger.debug("No active tokens to monitor."); await asyncio.sleep(10); continue

                logger.debug(f"Checking {len(mints_to_monitor)} active tokens: {[m[:6] for m in mints_to_monitor]}");
                tasks_to_run: List[asyncio.Task] = []
                for mint_str, current_state in mints_to_monitor.items():
                    if mint_str in self.active_monitoring_tasks and not self.active_monitoring_tasks[mint_str].done(): continue

                    if mint_str in self.active_monitoring_tasks: self.active_monitoring_tasks.pop(mint_str, None) # Clean up done task ref

                    token_info = self.get_stored_token_info(mint_str); initial_trade_result = self.get_stored_buy_result(mint_str)
                    if not token_info or not initial_trade_result:
                         logger.error(f"Missing info/buy_result for active token {mint_str}. Removing.");
                         self.token_states.pop(mint_str, None); self.token_infos.pop(mint_str, None);
                         self.token_buy_results.pop(mint_str, None); self.token_timestamps.pop(mint_str, None);
                         self.active_monitoring_tasks.pop(mint_str, None)
                         continue

                    task_name = f"Mon_{current_state[:4]}_{mint_str[:6]}"; monitor_coro = None
                    if current_state == TokenState.ON_BONDING_CURVE: monitor_coro = self._monitor_bonding_curve_token(token_info, initial_trade_result)
                    elif current_state == TokenState.ON_RAYDIUM and RAYDIUM_ENABLED: monitor_coro = self._monitor_raydium_token(token_info, initial_trade_result)
                    elif current_state == TokenState.ON_RAYDIUM and not RAYDIUM_ENABLED: logger.warning(f"Token {mint_str[:6]} in ON_RAYDIUM state but Raydium disabled."); self.token_states[mint_str] = TokenState.MONITORING_ERROR; continue

                    if monitor_coro:
                        task = asyncio.create_task(monitor_coro, name=task_name)
                        self.active_monitoring_tasks[mint_str] = task
                        tasks_to_run.append(task)
                        task.add_done_callback(lambda t, m=mint_str: self.active_monitoring_tasks.pop(m, None))


                if tasks_to_run: logger.debug(f"Created/relaunched {len(tasks_to_run)} monitor tasks.")

                terminal_states = [ TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.BUY_FAILED, TokenState.RUGGED, TokenState.MONITORING_ERROR, TokenState.SELL_CURVE_FAILED, TokenState.SELL_RAYDIUM_FAILED, TokenState.CLEANUP_FAILED ]
                mints_to_remove = [ m for m, s in list(self.token_states.items()) if s in terminal_states ]
                if mints_to_remove:
                    logger.info(f"Cleaning up internal state for {len(mints_to_remove)} finished/failed tokens: {[m[:6] for m in mints_to_remove]}")
                    for m in mints_to_remove:
                        self.token_states.pop(m, None); self.token_infos.pop(m, None);
                        self.token_buy_results.pop(m, None); self.token_timestamps.pop(m, None);
                        lingering_task = self.active_monitoring_tasks.pop(m, None);
                        if lingering_task and not lingering_task.done(): logger.warning(f"Cancelling lingering monitor task for removed mint {m[:6]}."); lingering_task.cancel()

                await asyncio.sleep(5)
            except asyncio.CancelledError: logger.info("Active trade monitoring loop cancelled."); break
            except Exception as e: logger.error(f"Error in monitoring loop: {e}", exc_info=True); await asyncio.sleep(20)
        logger.info("Active trade monitoring loop finished.")


    async def _monitor_bonding_curve_token(self, token_info: TokenInfo, initial_trade_result: TradeResult):
        mint_str = str(token_info.mint)
        initial_buy_price: Optional[float] = None
        if initial_trade_result and initial_trade_result.price is not None: initial_buy_price = initial_trade_result.price

        current_monitor_state = self.token_states.get(mint_str)
        if current_monitor_state != TokenState.ON_BONDING_CURVE: return

        sell_reason: Optional[str] = None
        try:
            current_curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state( token_info.bonding_curve, commitment=Processed )
            if not current_curve_state: logger.warning(f"Monitor curve: No curve state for {mint_str[:6]}."); return

            current_sol_reserves = getattr(current_curve_state, 'virtual_sol_reserves', 0)
            if current_sol_reserves >= self.SOL_THRESHOLD_FOR_RAYDIUM and RAYDIUM_ENABLED:
                logger.info(f"*** {token_info.symbol} ({mint_str[:6]}) crossed Raydium threshold. Transitioning. ***")
                if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE: self.token_states[mint_str] = TokenState.ON_RAYDIUM
                return

            if self.marry_mode_flag: return

            # *** FIX: Remove unused rug_reason variable ***
            rug_check_result = await self._check_post_buy_conditions(token_info, initial_trade_result, current_curve_state)
            if rug_check_result:
                 sell_reason = f"Rug Check: {rug_check_result}"
                 logger.warning(f"Rug condition detected for {mint_str[:6]}: {rug_check_result}")
            else:
                try: current_price = current_curve_state.calculate_price()
                except Exception as price_e: logger.warning(f"Error calculating price for TP/SL check {mint_str[:6]}: {price_e}"); current_price = None

                if initial_buy_price is not None and initial_buy_price > 1e-18 and current_price is not None:
                    profit_target_price = initial_buy_price * (1 + self.sell_profit_threshold)
                    stop_loss_price = initial_buy_price * (1 - self.sell_stoploss_threshold)
                    logger.debug(f"{mint_str[:6]}: Curve Price={current_price:.9f}, Buy={initial_buy_price:.9f}, TP={profit_target_price:.9f}, SL={stop_loss_price:.9f}")
                    if current_price >= profit_target_price: sell_reason = f"Profit Target ({current_price:.9f} >= {profit_target_price:.9f})"
                    elif current_price < stop_loss_price: sell_reason = f"Stop-Loss ({current_price:.9f} < {stop_loss_price:.9f})"

            if sell_reason:
                logger.warning(f"*** Sell trigger (curve) for {token_info.symbol}: {sell_reason} ***")
                if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE:
                    self.token_states[mint_str] = TokenState.SELLING_CURVE
                    logger.info(f"Executing bonding curve sell for {mint_str[:6]}...")
                    seller_result = await self.seller.execute(token_info=token_info, buyer_result=initial_trade_result)

                    # *** FIX: Corrected cleanup structure ***
                    if seller_result.success:
                        logger.info(f"Curve sell successful for {token_info.symbol}: {sell_reason}. Tx: {seller_result.tx_signature}")
                        self.token_states[mint_str] = TokenState.SOLD_CURVE
                        self._log_trade("sell_curve", token_info, seller_result.price, seller_result.amount, seller_result.tx_signature)
                        if self.cleanup_mode == "after_sell":
                            logger.info(f"Performing cleanup for sold token {mint_str[:6]}")
                            try:
                                await handle_cleanup_after_sell(
                                    self.solana_client, self.wallet, token_info.mint,
                                    self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee
                                )
                            except Exception as clean_e:
                                logger.error(f"Error during after_sell cleanup for {mint_str[:6]}: {clean_e}", exc_info=True)
                                self.token_states[mint_str] = TokenState.CLEANUP_FAILED
                    else:
                        logger.error(f"Curve sell failed for {token_info.symbol}: {seller_result.error_message}")
                        self.token_states[mint_str] = TokenState.SELL_CURVE_FAILED

                else:
                    logger.warning(f"Sell trigger {mint_str[:6]} detected but state changed to {self.token_states.get(mint_str)} before execution. Aborting sell.")
        except asyncio.CancelledError:
            logger.warning(f"Monitoring task curve {mint_str[:6]} cancelled.")
        except Exception as e:
            logger.error(f"Error monitoring curve {mint_str}: {e}", exc_info=True)
            if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE:
                self.token_states[mint_str] = TokenState.MONITORING_ERROR


    async def _monitor_raydium_token(self, token_info: TokenInfo, initial_trade_result: TradeResult):
        if not RAYDIUM_ENABLED or not self.raydium_seller: return
        mint_str = str(token_info.mint); initial_buy_price: Optional[float] = None
        if initial_trade_result and initial_trade_result.price is not None: initial_buy_price = initial_trade_result.price

        current_monitor_state = self.token_states.get(mint_str)
        if current_monitor_state != TokenState.ON_RAYDIUM: return

        sell_reason: Optional[str] = None
        try:
            if get_raydium_pool_data is None: logger.warning("Raydium pool data function not available."); return
            raydium_data = await get_raydium_pool_data(mint_str)
            if raydium_data is None: logger.debug(f"Monitor Raydium: No DEX data found for {mint_str[:6]} in this check."); return
            raydium_price = raydium_data.get("price");
            if raydium_price is None or not isinstance(raydium_price, (float, int)) or raydium_price <= 0: logger.warning(f"Monitor Raydium: Invalid DEX price ({raydium_price}) for {mint_str[:6]}."); return

            if self.marry_mode_flag: return

            if initial_buy_price is not None and initial_buy_price > 1e-18:
                profit_target_price = initial_buy_price * (1 + self.sell_profit_threshold)
                stop_loss_price = initial_buy_price * (1 - self.sell_stoploss_threshold)
                logger.debug(f"{mint_str[:6]}: DEX Price={raydium_price:.9f}, Buy={initial_buy_price:.9f}, TP={profit_target_price:.9f}, SL={stop_loss_price:.9f}")
                if raydium_price >= profit_target_price: sell_reason = f"DEX Profit target ({raydium_price:.9f} >= {profit_target_price:.9f})"
                elif raydium_price < stop_loss_price: sell_reason = f"DEX Stop-loss ({raydium_price:.9f} < {stop_loss_price:.9f})"

            if sell_reason:
                logger.warning(f"*** Sell trigger on DEX for {token_info.symbol}: {sell_reason} ***")
                if self.token_states.get(mint_str) == TokenState.ON_RAYDIUM:
                    self.token_states[mint_str] = TokenState.SELLING_RAYDIUM
                    logger.info(f"Executing Raydium sell for {mint_str[:6]}...")
                    user_ata = Wallet.get_associated_token_address(self.wallet.pubkey, token_info.mint)
                    amount_to_sell_lamports: int = 0 # Initialize
                    try:
                        balance_lamports = await self.solana_client.get_token_balance_lamports(user_ata, commitment=Confirmed)
                        if balance_lamports is not None: amount_to_sell_lamports = balance_lamports
                        else: logger.error(f"Failed to get balance for {user_ata}. Cannot determine sell amount."); self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED; return
                    except Exception as balance_e: logger.error(f"Error fetching balance for Raydium sell ATA {user_ata}: {balance_e}"); self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED; return

                    if amount_to_sell_lamports > 0 and self.raydium_seller:
                        sell_result = await self.raydium_seller.execute(token_info, amount_to_sell_lamports)

                        # *** FIX: Corrected cleanup structure ***
                        if sell_result.success:
                            logger.info(f"Raydium sell successful {token_info.symbol}. Tx: {sell_result.tx_signature}, Est Price: {sell_result.price:.9f}")
                            self.token_states[mint_str] = TokenState.SOLD_RAYDIUM
                            self._log_trade("sell_raydium", token_info, sell_result.price, sell_result.amount, sell_result.tx_signature);
                            if self.cleanup_mode == "after_sell":
                                logger.info(f"Performing cleanup for sold token {mint_str[:6]}")
                                try:
                                    await handle_cleanup_after_sell(
                                        self.solana_client, self.wallet, token_info.mint,
                                        self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee
                                    )
                                except Exception as clean_e:
                                    logger.error(f"Error during after_sell cleanup for Raydium sell {mint_str[:6]}: {clean_e}", exc_info=True)
                                    # self.token_states[mint_str] = TokenState.CLEANUP_FAILED
                        else: # Sell failed
                            logger.error(f"Raydium sell failed {token_info.symbol}: {sell_result.error_message}")
                            self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED

                    elif amount_to_sell_lamports <= 0 :
                        logger.warning(f"Raydium sell trigger for {mint_str[:6]} but balance is zero.")
                        self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED
                    elif not self.raydium_seller:
                        logger.error(f"Raydium sell trigger for {mint_str[:6]} but RaydiumSeller not initialized.")
                        self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED
                else:
                    logger.warning(f"Raydium sell trigger {mint_str[:6]} detected but state changed to {self.token_states.get(mint_str)} before execution. Aborting sell.")
        except asyncio.CancelledError:
            logger.warning(f"Monitoring task Raydium {mint_str[:6]} cancelled.")
        except Exception as e:
            logger.error(f"Error monitoring Raydium {mint_str}: {e}", exc_info=True)
            if self.token_states.get(mint_str) == TokenState.ON_RAYDIUM:
                 self.token_states[mint_str] = TokenState.MONITORING_ERROR


    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        logger.info(f"Checking creator holding for {token_info.symbol}...")
        try:
            mint_pk = token_info.mint; creator_pk = token_info.user
            total_supply = await self.solana_client.get_token_supply_lamports(mint_pk, commitment=Confirmed)
            if total_supply is None: logger.warning(f"No valid supply info for {mint_pk}. Skip creator check."); return True
            if total_supply <= 0: logger.warning("Total supply <= 0. Skip creator check."); return True

            creator_ata = Wallet.get_associated_token_address(mint=mint_pk, owner=creator_pk);
            creator_balance = await self.solana_client.get_token_balance_lamports(creator_ata, commitment=Processed)
            if creator_balance is None: logger.warning(f"Unable to get creator balance for {creator_ata} (owner: {creator_pk}). Assuming 0."); creator_balance = 0

            # Avoid division by zero
            if total_supply == 0: logger.warning(f"Total supply is zero for {mint_pk}, cannot calculate holding percentage."); return True;
            creator_percentage = creator_balance / total_supply;

            logger.info(f"Creator {creator_pk} holds {creator_balance}/{total_supply} ({creator_percentage:.2%})")
            if creator_percentage > self.rug_max_creator_hold_pct:
                 logger.warning(f"Creator holding check FAILED: {creator_percentage:.2%} > {self.rug_max_creator_hold_pct:.2%}."); return False
            logger.info("Creator holding check PASSED."); return True
        except Exception as e:
             logger.error(f"Error during creator holding check for {token_info.symbol}: {e}", exc_info=True)
             return True # Fail open


    async def _check_post_buy_conditions(self, token_info: TokenInfo, initial_trade_result: TradeResult, current_curve_state: Optional[BondingCurveState] = None) -> Optional[str]:
        if not (self.rug_check_price_drop or self.rug_check_liquidity_drop): return None
        # logger.debug(f"Checking post-buy conditions for {token_info.symbol}") # Can be noisy
        try:
            if current_curve_state is None: current_curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve, commitment=Processed)
            if not current_curve_state: logger.warning(f"Post-buy check: No curve state for {token_info.symbol}."); return None

            original_buy_price = initial_trade_result.price; initial_sol_liq = initial_trade_result.initial_sol_liquidity

            # Price Drop Check
            if self.rug_check_price_drop and original_buy_price is not None and original_buy_price > 1e-18:
                try: current_price = current_curve_state.calculate_price()
                except Exception: current_price = None
                if current_price is not None and current_price < original_buy_price:
                    # Avoid division by zero
                    if original_buy_price == 0: price_drop_pct = 1.0
                    else: price_drop_pct = (original_buy_price - current_price) / original_buy_price

                    if price_drop_pct > self.rug_price_drop_pct:
                         logger.warning(f"RUG CHECK: Price drop {price_drop_pct:.1%} > {self.rug_price_drop_pct:.1%} for {token_info.symbol}")
                         return f"Price drop {price_drop_pct:.1%}"

            # Liquidity Drop Check
            if self.rug_check_liquidity_drop and initial_sol_liq is not None and initial_sol_liq > 0:
                current_sol_reserves = getattr(current_curve_state, 'virtual_sol_reserves', None)
                if current_sol_reserves is not None:
                    current_sol_int = int(current_sol_reserves); initial_sol_int = int(initial_sol_liq);
                    if current_sol_int < initial_sol_int:
                         liquidity_drop_pct = (initial_sol_int - current_sol_int) / initial_sol_int; # Already checked initial_sol_int > 0
                         if liquidity_drop_pct > self.rug_liquidity_drop_pct:
                             logger.warning(f"RUG CHECK: Liquidity drop {liquidity_drop_pct:.1%} > {self.rug_liquidity_drop_pct:.1%} for {token_info.symbol} ({current_sol_int} < {initial_sol_int})")
                             return f"Liquidity drop {liquidity_drop_pct:.1%}"
                else: logger.warning(f"Post-buy check: No virtual_sol_reserves found for {token_info.symbol}.")

            return None
        except Exception as e:
             logger.error(f"Error during post-buy check for {token_info.symbol}: {e}", exc_info=True)
             return None


    # --- _save_token_info method (Corrected) ---
    @staticmethod
    async def _save_token_info(token_info: TokenInfo) -> None:
        os.makedirs("trades", exist_ok=True); file_name = os.path.join("trades", f"{str(token_info.mint)}.json")
        try:
            data_to_save: Dict[str, Any] = {}
            # *** FIX: Check if it IS a dataclass before calling asdict ***
            if dataclasses.is_dataclass(token_info):
                data_to_save = dataclasses.asdict(token_info)
            else: # Fallback if it's not
                data_to_save = {k: v for k, v in vars(token_info).items() if not k.startswith('_')}

            # Convert Pubkeys and handle potential non-JSON serializable types
            for key, value in data_to_save.items():
                 if isinstance(value, Pubkey): data_to_save[key] = str(value)
                 elif isinstance(value, float) and value == float('inf'): data_to_save[key] = "Infinity"
            with open(file_name, "w", encoding="utf-8") as file_handle:
                json.dump(data_to_save, file_handle, indent=2)
            logger.debug(f"Token info saved: {file_name}")
        except IOError as e: logger.error(f"Failed to save token info {file_name}: {e}")
        except TypeError as e: logger.error(f"Failed to serialize TokenInfo for {file_name}: {e}")


    # --- _log_trade method (Keep as is) ---
    @staticmethod
    def _log_trade(action: str, token_info: TokenInfo, price: Optional[float], amount: Optional[float], tx_hash: Optional[str]) -> None:
        os.makedirs("trades", exist_ok=True)
        log_entry = { "timestamp": datetime.now(timezone.utc).isoformat(), "action": action, "token_address": str(token_info.mint), "symbol": token_info.symbol, "price_sol_per_token": price, "amount_tokens_or_sol": amount, "tx_hash": str(tx_hash) }
        try:
            with open("trades/trades.log", "a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except IOError as e: logger.error(f"Failed to write trade log: {e}")