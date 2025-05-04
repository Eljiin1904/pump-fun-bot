# src/trading/trader.py

import asyncio
import json
import os
import time
import dataclasses
import pprint # Keep for debugging if needed, though _save_token_info uses custom serializer now
from datetime import datetime, timezone
from typing import Optional, Set, Dict, Any, List

from solders.pubkey import Pubkey
# Use Solders commitment types internally where appropriate
from solders.commitment_config import CommitmentLevel # Keep this
# Import specific Solana commitment types if needed for direct RPC calls outside client wrapper
from solana.rpc.commitment import Confirmed, Processed
from solana.exceptions import SolanaRpcException # Import base exception

# --- Project Imports ---
from ..cleanup.modes import handle_cleanup_after_failure, handle_cleanup_after_sell, handle_cleanup_post_session
from ..core.client import SolanaClient # Your client wrapper
from ..core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL
from ..core.priority_fee.manager import PriorityFeeManager # Assuming this exists and works
from ..core.wallet import Wallet
from ..monitoring.base_listener import BaseTokenListener
from ..monitoring.listener_factory import ListenerFactory
from ..trading.base import TokenInfo, TradeResult
from ..trading.buyer import TokenBuyer
from ..trading.seller import TokenSeller
from ..utils.logger import get_logger

# Module-level placeholders & Conditional Raydium Imports
_RaydiumSeller = None
_get_raydium_pool_data = None
_RAYDIUM_ENABLED = False
try:
    from ..data.raydium_data import get_raydium_pool_data as _grpd
    from .raydium_seller import RaydiumSeller as _RS
    _RaydiumSeller = _RS
    _get_raydium_pool_data = _grpd
    _RAYDIUM_ENABLED = True
except ImportError:
    # Simplified warning during module load
    print("WARNING: Raydium components not found or import error, Raydium features disabled.")

# Initialize logger at module level
logger = get_logger(__name__)

# --- Token State Definitions ---
class TokenState:
    DETECTED = "DETECTED"
    BUYING = "BUYING"
    BUY_FAILED = "BUY_FAILED"
    ON_BONDING_CURVE = "ON_BONDING_CURVE"
    SELLING_CURVE = "SELLING_CURVE"
    SOLD_CURVE = "SOLD_CURVE"
    SELL_CURVE_FAILED = "SELL_CURVE_FAILED"
    ON_RAYDIUM = "ON_RAYDIUM"
    SELLING_RAYDIUM = "SELLING_RAYDIUM"
    SOLD_RAYDIUM = "SOLD_RAYDIUM"
    SELL_RAYDIUM_FAILED = "SELL_RAYDIUM_FAILED"
    RUGGED = "RUGGED"
    MONITORING_ERROR = "MONITORING_ERROR"
    CLEANUP_FAILED = "CLEANUP_FAILED"

# --- dataclass_to_serializable_dict helper ---
def dataclass_to_serializable_dict(obj: Any) -> Any:
    """Recursively converts objects for JSON serialization, handling Pubkeys."""
    if isinstance(obj, Pubkey):
        return str(obj)
    elif dataclasses.is_dataclass(obj):
        # Important: Check for obj type before iterating fields if base classes exist
        if type(obj) is object: return {} # Avoid infinite recursion on plain objects
        result = {}
        for field in dataclasses.fields(obj):
            # Skip fields starting with '_' or if marked as non-serializable if needed
            if field.name.startswith('_'): continue
            value = getattr(obj, field.name)
            result[field.name] = dataclass_to_serializable_dict(value)
        return result
    elif isinstance(obj, dict):
        return {k: dataclass_to_serializable_dict(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [dataclass_to_serializable_dict(item) for item in obj]
    elif isinstance(obj, float) and (obj == float('inf') or obj == float('-inf')):
        return str(obj) # Represent infinity as string
    elif isinstance(obj, (int, str, bool, float)) or obj is None:
        return obj # Primitive types are serializable
    # Fallback: Convert unknown types to string representation
    logger.debug(f"Converting unknown type {type(obj)} to string for JSON.")
    return str(obj)


class PumpTrader:
    """ Manages the overall trading process for pump.fun tokens. """

    def __init__(
        self, client: SolanaClient, priority_fee_manager: PriorityFeeManager, wallet: Wallet, wss_endpoint: str,
        buy_amount: float, buy_slippage: float, sell_slippage: float, sell_profit_threshold: float,
        sell_stoploss_threshold: float, max_token_age: float, wait_time_after_creation: float,
        wait_time_after_buy: float, wait_time_before_new_token: float, max_retries: int, max_buy_retries: int,
        confirm_timeout: int, listener_type: str, rug_check_creator: bool, rug_max_creator_hold_pct: float,
        rug_check_price_drop: bool, rug_price_drop_pct: float, rug_check_liquidity_drop: bool,
        rug_liquidity_drop_pct: float, cleanup_mode: str, cleanup_force_close: bool, cleanup_priority_fee: bool,
        sol_threshold_for_raydium: float = 300.0,
    ):
        """ Initializes the PumpTrader instance. """
        # Use self.logger for instance-specific logging
        self.logger = logger # Assign module logger or get instance logger if preferred

        # --- Assign core components ---
        self.wallet = wallet
        self.solana_client = client
        self.priority_fee_manager = priority_fee_manager
        self.curve_manager = BondingCurveManager(self.solana_client)
        self.wss_endpoint = wss_endpoint
        self.listener_type = listener_type

        # --- Trading parameters ---
        self.buy_amount = buy_amount
        # Store slippage as decimals (e.g., 15.0 becomes 0.15)
        self.buy_slippage = buy_slippage / 100.0
        self.sell_slippage = sell_slippage / 100.0
        # Store thresholds as multipliers (e.g., 1500.0 becomes 15.0)
        self.sell_profit_threshold = sell_profit_threshold / 100.0
        self.sell_stoploss_threshold = sell_stoploss_threshold / 100.0

        # --- Timing parameters ---
        self.max_token_age = max_token_age
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token

        # --- Transaction settings ---
        self.max_retries = max_retries
        self.max_buy_retries = max_buy_retries
        self.confirm_timeout = confirm_timeout

        # --- Rug check parameters ---
        self.rug_check_creator = rug_check_creator
        # Store percentage as decimal (e.g., 5.0 from config becomes 0.05)
        self.rug_max_creator_hold_pct = rug_max_creator_hold_pct / 100.0
        self.rug_check_price_drop = rug_check_price_drop
        self.rug_price_drop_pct = rug_price_drop_pct / 100.0
        self.rug_check_liquidity_drop = rug_check_liquidity_drop
        self.rug_liquidity_drop_pct = rug_liquidity_drop_pct / 100.0

        # --- Cleanup ---
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close = cleanup_force_close
        self.cleanup_priority_fee = cleanup_priority_fee

        # --- Raydium ---
        self.SOL_THRESHOLD_FOR_RAYDIUM = int(sol_threshold_for_raydium * LAMPORTS_PER_SOL)
        self.raydium_enabled = _RAYDIUM_ENABLED
        self._raydium_seller_class = _RaydiumSeller
        self.get_raydium_pool_data = _get_raydium_pool_data

        # --- Initialize trading components ---
        self.buyer = TokenBuyer(
            client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager,
            priority_fee_manager=self.priority_fee_manager, amount_sol=self.buy_amount,
            slippage_decimal=self.buy_slippage, max_retries=self.max_buy_retries,
            confirm_timeout=self.confirm_timeout
        )
        self.seller = TokenSeller(
            client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager,
            priority_fee_manager=self.priority_fee_manager, slippage=self.sell_slippage,
            max_retries=self.max_retries, confirm_timeout=self.confirm_timeout
        )
        self.raydium_seller: Optional[Any] = None
        if self.raydium_enabled and self._raydium_seller_class:
            # Raydium seller expects slippage in BPS (Basis Points)
            raydium_slippage_bps = int(self.sell_slippage * 10000)
            self.raydium_seller = self._raydium_seller_class(
                client=self.solana_client, wallet=self.wallet,
                priority_fee_manager=self.priority_fee_manager,
                slippage_bps=raydium_slippage_bps
            )

        # --- Internal state ---
        self.marry_mode_flag = False # Will be set in start()
        self.token_listener: Optional[BaseTokenListener] = None
        self.token_states: Dict[str, str] = {}
        self.token_infos: Dict[str, TokenInfo] = {}
        self.token_buy_results: Dict[str, TradeResult] = {}
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        self.processed_tokens: Set[str] = set()
        self.token_timestamps: Dict[str, float] = {}
        self.active_monitoring_tasks: Dict[str, asyncio.Task] = {}
        self.traded_mints_session: Set[Pubkey] = set()

        # --- Log initialization summary ---
        self.logger.info(f"--- PumpTrader Initialized ---")
        self.logger.info(f"Wallet: {self.wallet.pubkey}")
        self.logger.info(f"Trading: Buy={self.buy_amount:.6f} SOL, BuySlip={self.buy_slippage:.1%}, SellSlip={self.sell_slippage:.1%}")
        self.logger.info(f"Sell Logic: TP={self.sell_profit_threshold:.1%}, SL={self.sell_stoploss_threshold:.1%}")
        if self.raydium_enabled:
            self.logger.info(f"Raydium: Enabled, Threshold=~{self.SOL_THRESHOLD_FOR_RAYDIUM / 1e9:.1f} SOL")
        else:
            self.logger.info("Raydium: Disabled")
        # Correctly log the creator hold threshold used internally
        self.logger.info(f"Rug Checks: Creator={self.rug_check_creator} (Max={self.rug_max_creator_hold_pct:.2%}), PriceDrop={self.rug_check_price_drop} ({self.rug_price_drop_pct:.1%}), LiqDrop={self.rug_check_liquidity_drop} ({self.rug_liquidity_drop_pct:.1%})")
        self.logger.info(f"Cleanup: Mode='{self.cleanup_mode}'")
        self.logger.info(f"-----------------------------")

    def get_stored_token_info(self, mint_str: str) -> Optional[TokenInfo]:
        """ Retrieves stored token information. """
        return self.token_infos.get(mint_str)

    def get_stored_buy_result(self, mint_str: str) -> Optional[TradeResult]:
        """ Retrieves stored buy result information. """
        return self.token_buy_results.get(mint_str)

    async def start(
        self, match_string: Optional[str] = None, bro_address: Optional[str] = None,
        marry_mode: bool = False, yolo_mode: bool = False
    ):
        """ Starts the trader, listener, and processing loops. """
        self.marry_mode_flag = marry_mode
        self.logger.info(f"Starting trader...")
        self.logger.info(f"Match: {match_string or 'None'} | Creator: {bro_address or 'None'} | Marry: {marry_mode} | YOLO: {yolo_mode} | Max Age: {self.max_token_age}s")

        factory = ListenerFactory()
        listener_task: Optional[asyncio.Task] = None
        monitor_task: Optional[asyncio.Task] = None
        processor_task: Optional[asyncio.Task] = None
        created_tasks: List[asyncio.Task] = []
        pending_tasks: Set[asyncio.Task] = set()

        try:
            # Initialize Listener
            self.token_listener = factory.create_listener(self.listener_type, self.wss_endpoint, self.solana_client)
            self.logger.info(f"Using {self.listener_type} listener")
            if not hasattr(self.token_listener, 'listen_for_tokens') or not callable(getattr(self.token_listener, 'listen_for_tokens')):
                self.logger.critical(f"FATAL: Listener object '{self.token_listener}' missing callable 'listen_for_tokens' method.")
                return

            # Create core tasks
            processor_task = asyncio.create_task(self._process_token_queue(marry_mode, yolo_mode), name="TokenProcessor")
            created_tasks.append(processor_task)
            monitor_task = asyncio.create_task(self._monitor_active_trades(), name="TradeMonitor")
            created_tasks.append(monitor_task)

            self.logger.info("Starting main listener task...")
            listener_task = asyncio.create_task(
                self.token_listener.listen_for_tokens(self._queue_token, match_string, bro_address),
                name="TokenListener"
            )
            created_tasks.append(listener_task)

            if not created_tasks:
                self.logger.warning("No primary tasks were created.")
                return

            # Wait for the first task to complete (or fail)
            self.logger.debug(f"Awaiting first completion of {len(created_tasks)} primary tasks...")
            done_tasks, pending_tasks = await asyncio.wait(created_tasks, return_when=asyncio.FIRST_COMPLETED)

            # Log outcome of the first completed task
            for task in done_tasks:
                task_name = task.get_name() if hasattr(task, 'get_name') else "Unknown Task"
                try:
                    task.result() # Raise exception if task failed
                    self.logger.info(f"Primary task {task_name} completed first.")
                except asyncio.CancelledError:
                    self.logger.info(f"Primary task {task_name} was cancelled.")
                except Exception as task_exc:
                    self.logger.error(f"Primary task {task_name} failed first: {task_exc}", exc_info=True)

        except asyncio.CancelledError:
            self.logger.info("Trader start task was cancelled externally.")
        except Exception as e:
            self.logger.critical(f"Error during trader startup or task management: {e}", exc_info=True)
        finally:
            # --- Graceful Shutdown ---
            self.logger.info("Shutting down trader (start method finally block)...")

            # Combine pending and created tasks for cancellation
            tasks_to_cancel = list(pending_tasks)
            tasks_to_cancel.extend([t for t in created_tasks if t and not t.done() and t not in pending_tasks])
            tasks_to_cancel = list(set(tasks_to_cancel)) # Remove duplicates

            # Cancel primary tasks
            if tasks_to_cancel:
                self.logger.info(f"Cancelling {len(tasks_to_cancel)} primary background task(s)...")
                for task in tasks_to_cancel:
                    task_name = task.get_name() if hasattr(task, 'get_name') else "Unknown Task"
                    if not task.done():
                        self.logger.debug(f"Cancelling task: {task_name}")
                        task.cancel()
                # Wait for cancellations to complete
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
                self.logger.info("Primary background tasks cancellation complete.")
            else:
                self.logger.info("No running primary tasks needed cancellation.")

            # Stop listener
            if self.token_listener:
                self.logger.info("Requesting listener stop...")
                if hasattr(self.token_listener, 'stop') and callable(getattr(self.token_listener, 'stop')):
                    try:
                        # Add timeout to listener stop
                        await asyncio.wait_for(self.token_listener.stop(), timeout=5.0)
                        self.logger.info("Listener stopped.")
                    except asyncio.TimeoutError:
                        self.logger.error("Timeout stopping listener.")
                    except Exception as stop_e:
                        self.logger.error(f"Error stopping listener: {stop_e}", exc_info=True)
                else:
                    self.logger.warning("Listener has no stop method.")
                # Ensure listener task is awaited after stop request
                if listener_task and not listener_task.done():
                    if not listener_task.cancelled(): listener_task.cancel()
                    await asyncio.gather(listener_task, return_exceptions=True)


            # Cancel any active monitoring sub-tasks
            active_sub_tasks = list(self.active_monitoring_tasks.values())
            if active_sub_tasks:
                self.logger.info(f"Cancelling {len(active_sub_tasks)} active monitoring sub-tasks...")
                for task in active_sub_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*active_sub_tasks, return_exceptions=True)
                self.logger.info("Monitoring sub-tasks cancellation complete.")
                self.active_monitoring_tasks.clear()

            # Perform final cleanup if configured
            if self.cleanup_mode == "post_session" and self.traded_mints_session:
                self.logger.info("Performing post-session cleanup...")
                try:
                    await handle_cleanup_post_session(
                        self.solana_client, self.wallet, list(self.traded_mints_session),
                        self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee
                    )
                    self.logger.info("Post-session cleanup attempt finished.")
                except Exception as clean_e:
                    self.logger.error(f"Error during post-session cleanup: {clean_e}", exc_info=True)

            self.logger.info("Trader start method finished cleanup process.")

    async def _queue_token(self, token_info: TokenInfo) -> None:
        """ Adds a token to the processing queue if not already processed or active. """
        token_key = str(token_info.mint)
        mint_pubkey = token_info.mint
        # Prevent re-processing
        if token_key in self.processed_tokens or token_key in self.token_states or mint_pubkey in self.traded_mints_session:
            return
        self.token_timestamps[token_key] = time.time() # Record discovery time
        await self.token_queue.put(token_info)
        self.logger.info(f"Queued: {token_info.symbol} ({token_key[:6]}...)")

    async def _process_token_queue(self, marry_mode: bool, yolo_mode: bool) -> None:
        """ Continuously processes tokens from the queue based on mode. """
        self.logger.info("Starting token processor task...")
        currently_processing_marry: Optional[str] = None # Track token in marry mode
        active_yolo_tasks: Set[asyncio.Task] = set()
        MAX_YOLO_CONCURRENCY = 5 # Limit concurrent handling tasks in YOLO mode

        while True: # Main loop processing queue
            token_info: Optional[TokenInfo] = None
            try: # Handle token processing errors gracefully
                # --- Concurrency Limiting (YOLO Mode) ---
                while yolo_mode and len(active_yolo_tasks) >= MAX_YOLO_CONCURRENCY:
                    self.logger.debug(f"YOLO concurrency limit ({MAX_YOLO_CONCURRENCY}) reached, waiting...")
                    # Wait for at least one task to finish
                    done, active_yolo_tasks = await asyncio.wait(
                        active_yolo_tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    # Log exceptions from completed tasks
                    for task in done:
                        if task.exception():
                            task_name = task.get_name() if hasattr(task, 'get_name') else "Unknown YOLO Task"
                            self.logger.error(f"YOLO Handling task {task_name} failed", exc_info=task.exception())

                # --- Marry Mode Check (Single Token Active) ---
                if marry_mode and currently_processing_marry:
                    current_marry_state = self.token_states.get(currently_processing_marry)
                    # Define terminal states where marry mode can process next token
                    terminal_states = [
                        TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.BUY_FAILED,
                        TokenState.RUGGED, TokenState.MONITORING_ERROR, TokenState.SELL_CURVE_FAILED,
                        TokenState.SELL_RAYDIUM_FAILED, TokenState.CLEANUP_FAILED
                    ]
                    # If current token is still active (not terminal), wait
                    if current_marry_state and current_marry_state not in terminal_states:
                        await asyncio.sleep(1) # Short sleep before checking again
                        continue # Skip getting new token
                    else:
                        # Previous token finished, clear the lock
                        self.logger.info(f"Marry mode: Prev token {currently_processing_marry[:6]} finished ({current_marry_state}). Ready.")
                        currently_processing_marry = None

                # --- Get Next Token ---
                token_info = await self.token_queue.get()
                token_key = str(token_info.mint)
                mint_pubkey = token_info.mint

                # --- Skip Checks ---
                if token_key in self.processed_tokens or token_key in self.token_states or mint_pubkey in self.traded_mints_session:
                    self.logger.debug(f"Skipping {token_key[:6]} - already processed or active.")
                    self.token_queue.task_done()
                    continue

                discovery_time = self.token_timestamps.get(token_key)
                current_time = time.time()
                if discovery_time and (current_time - discovery_time > self.max_token_age):
                    age = current_time - discovery_time
                    self.logger.info(f"Skipping {token_info.symbol} ({token_key[:6]}) - too old ({age:.1f}s > {self.max_token_age}s)")
                    self.processed_tokens.add(token_key) # Mark as processed to avoid re-queue
                    self.token_queue.task_done()
                    continue
                elif not discovery_time:
                    self.logger.warning(f"No discovery timestamp found for {token_key}. Setting time now.")
                    self.token_timestamps[token_key] = current_time

                # --- Process Token ---
                self.processed_tokens.add(token_key) # Mark as processed *before* handling starts
                if marry_mode:
                    currently_processing_marry = token_key # Set lock for marry mode

                self.logger.info(f"Processing queue item: {token_info.symbol} ({token_key[:6]})")
                handle_task = asyncio.create_task(self._handle_new_token(token_info), name=f"Handle_{token_key[:6]}")

                if yolo_mode:
                    # Track task for concurrency limiting
                    active_yolo_tasks.add(handle_task)
                    # Remove task from set upon completion
                    handle_task.add_done_callback(active_yolo_tasks.discard)
                else:
                    # If not YOLO, wait for this token handling to complete before next
                    await handle_task

                # Mark queue item as done regardless of handle_task outcome (it runs async in yolo)
                self.token_queue.task_done()

            except asyncio.CancelledError:
                self.logger.info("Token processor task cancelled.")
                break # Exit the main while loop
            except Exception as e:
                token_id_str = f"token {token_info.symbol} ({token_info.mint})" if token_info else "N/A"
                self.logger.error(f"Error in token processor loop for {token_id_str}: {e}", exc_info=True)
                # Ensure task is marked done even if error occurs getting/processing item
                if token_info:
                    try: self.token_queue.task_done()
                    except ValueError: pass # Ignore error if task already done
                await asyncio.sleep(5) # Wait before trying next item

        # Cleanup remaining YOLO tasks on exit
        if active_yolo_tasks:
            self.logger.info(f"Cancelling {len(active_yolo_tasks)} remaining YOLO handling tasks...")
            for task in active_yolo_tasks: task.cancel()
            await asyncio.gather(*active_yolo_tasks, return_exceptions=True)

        self.logger.info("Token processor task finished.")

    async def _handle_new_token(self, token_info: TokenInfo) -> None:
        """ Handles the logic for processing a single new token (checks, buy). """
        mint_str = str(token_info.mint)
        if mint_str in self.token_states:
            self.logger.debug(f"Token {mint_str[:6]} processing already initiated state: {self.token_states.get(mint_str)}. Aborting redundant handler.")
            return

        self.logger.info(f"Handling new token: {token_info.symbol} ({mint_str[:6]})")
        self.token_states[mint_str] = TokenState.DETECTED
        self.token_infos[mint_str] = token_info
        buyer_result: Optional[TradeResult] = None

        try:
            # --- FIX: Run synchronous save_token_info in executor ---
            try:
                loop = asyncio.get_running_loop()
                # Run the static method in the default executor (thread pool)
                # Pass the method and its arguments
                await loop.run_in_executor(
                    None, # Uses default executor
                    PumpTrader._save_token_info, # Call the static method directly
                    token_info
                )
            except Exception as save_e:
                # Log error but continue processing the token for now
                self.logger.error(f"Error saving token info for {mint_str[:6]} in executor: {save_e}", exc_info=True)
            # --- END FIX ---

            # Pre-buy delay
            self.logger.info(f"Waiting {self.wait_time_after_creation:.1f}s pre-buy for {token_info.symbol}...")
            await asyncio.sleep(self.wait_time_after_creation)

            # Re-check state after delay
            current_state_after_wait = self.token_states.get(mint_str)
            if current_state_after_wait != TokenState.DETECTED:
                self.logger.warning(f"Token {mint_str[:6]} state changed during pre-buy wait to {current_state_after_wait}. Aborting handler.")
                return

            # Creator Holding Check
            if self.rug_check_creator:
                creator_check_passed = False
                try:
                    creator_check_passed = await asyncio.wait_for(self._check_creator_holding(token_info), timeout=30.0)
                except asyncio.TimeoutError:
                    self.logger.warning(f"Skipping {token_info.symbol}: Creator check timed out.")
                    self.token_states[mint_str] = TokenState.BUY_FAILED
                    return
                except Exception as rug_e:
                    self.logger.error(f"Error during creator check for {token_info.symbol}: {rug_e}", exc_info=True)
                    self.token_states[mint_str] = TokenState.BUY_FAILED
                    return

                if not creator_check_passed:
                    self.logger.warning(f"Skipping {token_info.symbol} due to failed creator check.")
                    self.token_states[mint_str] = TokenState.RUGGED
                    return

            # Re-check state before buy attempt
            current_state_before_buy = self.token_states.get(mint_str)
            if current_state_before_buy != TokenState.DETECTED:
                self.logger.warning(f"Token {mint_str[:6]} state changed before buy attempt to {current_state_before_buy}. Aborting handler.")
                return

            # --- Execute Buy ---
            self.logger.info(f"Attempting buy of {self.buy_amount:.6f} SOL for {token_info.symbol}...")
            self.token_states[mint_str] = TokenState.BUYING
            buyer_result = await self.buyer.execute(token_info=token_info)

            if buyer_result.success and buyer_result.tx_signature:
                 self.logger.info(f"Buy successful for {token_info.symbol}. Tx: {buyer_result.tx_signature}, Price: {buyer_result.price:.9f}, Amount: {buyer_result.amount}")
                 # Log trade using static method
                 PumpTrader._log_trade("buy", token_info, buyer_result.price, buyer_result.amount, buyer_result.tx_signature)
                 self.traded_mints_session.add(token_info.mint)
                 self.token_buy_results[mint_str] = buyer_result
                 self.token_states[mint_str] = TokenState.ON_BONDING_CURVE
                 self.logger.info(f"Token {token_info.symbol} ({mint_str[:6]}) monitoring started on bonding curve.")
            else:
                 self.logger.error(f"Buy failed for {token_info.symbol}: {buyer_result.error_message}")
                 self.token_states[mint_str] = TokenState.BUY_FAILED

            # Optional: Cleanup immediately on buy failure if configured
            if self.token_states.get(mint_str) == TokenState.BUY_FAILED and self.cleanup_mode == "on_fail":
                 self.logger.info(f"Performing cleanup for failed buy of {mint_str[:6]}")
                 try:
                      await handle_cleanup_after_failure(
                          self.solana_client, self.wallet, token_info.mint,
                          self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee
                      )
                 except Exception as clean_e:
                      self.logger.error(f"Error during on_fail cleanup for {mint_str[:6]}: {clean_e}", exc_info=True)
                      self.token_states[mint_str] = TokenState.CLEANUP_FAILED

        except asyncio.CancelledError:
            self.logger.warning(f"Handling task for {mint_str[:6]} cancelled.")
            if self.token_states.get(mint_str) not in [TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.BUY_FAILED, TokenState.RUGGED]:
                self.token_states[mint_str] = TokenState.MONITORING_ERROR
        except Exception as e:
            self.logger.error(f"Unexpected error handling new token {token_info.symbol}: {e}", exc_info=True)
            if self.token_states.get(mint_str) not in [TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.BUY_FAILED, TokenState.RUGGED]:
                self.token_states[mint_str] = TokenState.MONITORING_ERROR

    # --- Ensure _save_token_info is NOT async and is static ---
    @staticmethod
    def _save_token_info(token_info: TokenInfo) -> None: # Changed to regular def
        """Saves token info to a JSON file synchronously."""
        os.makedirs("trades", exist_ok=True)
        file_name = os.path.join("trades", f"{str(token_info.mint)}.json")
        try:
            # Use the module-level helper function for serialization
            serializable_data = dataclass_to_serializable_dict(token_info)
            with open(file_name, "w", encoding="utf-8") as file_handle:
                json.dump(serializable_data, file_handle, indent=2, ensure_ascii=False)
            logger.debug(f"Token info saved: {file_name}") # Use module logger
        except Exception as e:
            # Log any error during saving
            logger.error(f"Error saving token info {file_name}: {e}", exc_info=True)


    async def _monitor_active_trades(self):
        """ Periodically checks and manages monitoring tasks for active trades. """
        self.logger.info("Starting active trade monitoring loop...")
        await asyncio.sleep(5) # Initial delay

        while True: # Main monitoring loop
            try:
                # Identify tokens needing monitoring
                mints_to_monitor = {
                    m: s for m, s in self.token_states.items()
                    if s in [TokenState.ON_BONDING_CURVE, TokenState.ON_RAYDIUM]
                }

                if not mints_to_monitor:
                    self.logger.debug("No active tokens to monitor.")
                    await asyncio.sleep(10) # Longer sleep if nothing active
                    continue

                self.logger.debug(f"Checking {len(mints_to_monitor)} active tokens: {[m[:6] for m in mints_to_monitor]}")
                tasks_to_run: List[asyncio.Task] = []

                # Launch monitoring tasks if not already running
                for mint_str, current_state in mints_to_monitor.items():
                    # Check if a task exists and is still running
                    if mint_str in self.active_monitoring_tasks and not self.active_monitoring_tasks[mint_str].done():
                        continue # Skip if already running

                    # Clean up reference if task exists but is done
                    if mint_str in self.active_monitoring_tasks:
                        self.active_monitoring_tasks.pop(mint_str, None)

                    # Get necessary info
                    token_info = self.get_stored_token_info(mint_str)
                    initial_trade_result = self.get_stored_buy_result(mint_str)

                    # Validate info exists before starting monitor task
                    if not token_info or not initial_trade_result:
                         self.logger.error(f"Missing info/buy_result for active token {mint_str}. Removing state.")
                         self.token_states.pop(mint_str, None)
                         self.token_infos.pop(mint_str, None)
                         self.token_buy_results.pop(mint_str, None)
                         self.token_timestamps.pop(mint_str, None)
                         self.active_monitoring_tasks.pop(mint_str, None) # Ensure cleanup
                         continue

                    # Determine which monitoring coroutine to run
                    task_name = f"Mon_{current_state[:4]}_{mint_str[:6]}"
                    monitor_coro = None
                    if current_state == TokenState.ON_BONDING_CURVE:
                        monitor_coro = self._monitor_bonding_curve_token(token_info, initial_trade_result)
                    elif current_state == TokenState.ON_RAYDIUM and self.raydium_enabled:
                        monitor_coro = self._monitor_raydium_token(token_info, initial_trade_result)
                    elif current_state == TokenState.ON_RAYDIUM and not self.raydium_enabled:
                         self.logger.warning(f"Token {mint_str[:6]} in ON_RAYDIUM state but Raydium features are disabled.")
                         self.token_states[mint_str] = TokenState.MONITORING_ERROR # Cannot monitor
                         continue

                    # Launch the task
                    if monitor_coro:
                        task = asyncio.create_task(monitor_coro, name=task_name)
                        self.active_monitoring_tasks[mint_str] = task
                        tasks_to_run.append(task)
                        # Add callback to remove from dict upon completion/cancellation
                        task.add_done_callback(lambda t, m=mint_str: self.active_monitoring_tasks.pop(m, None))

                if tasks_to_run:
                    self.logger.debug(f"Created/relaunched {len(tasks_to_run)} monitor tasks.")

                # --- Cleanup internal state for finished tokens ---
                terminal_states = [
                    TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.BUY_FAILED,
                    TokenState.RUGGED, TokenState.MONITORING_ERROR, TokenState.SELL_CURVE_FAILED,
                    TokenState.SELL_RAYDIUM_FAILED, TokenState.CLEANUP_FAILED
                ]
                # Use list comprehension for potentially faster filtering
                mints_to_remove = [
                    m for m, s in self.token_states.items() if s in terminal_states
                ]

                if mints_to_remove:
                    self.logger.info(f"Cleaning up internal state for {len(mints_to_remove)} finished/failed tokens: {[m[:6] for m in mints_to_remove]}")
                    for m in mints_to_remove:
                        self.token_states.pop(m, None)
                        self.token_infos.pop(m, None)
                        self.token_buy_results.pop(m, None)
                        self.token_timestamps.pop(m, None)
                        # Cancel any lingering monitoring task just in case
                        lingering_task = self.active_monitoring_tasks.pop(m, None)
                        if lingering_task and not lingering_task.done():
                            self.logger.warning(f"Cancelling lingering monitor task for removed mint {m[:6]}.")
                            lingering_task.cancel()

                await asyncio.sleep(5) # Interval for checking active trades

            except asyncio.CancelledError:
                self.logger.info("Active trade monitoring loop cancelled.")
                break # Exit loop
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(20) # Longer sleep on unexpected error

        self.logger.info("Active trade monitoring loop finished.")


    # --- Corrected _check_creator_holding (with retries) ---
    # (Paste the corrected _check_creator_holding method from Message #14 here)
    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        """ Checks creator holding percentage with retries for RPC calls. """
        self.logger.info(f"Checking creator holding for {token_info.symbol}...")
        mint_pk = token_info.mint; creator_pk = token_info.user
        total_supply_lamports: Optional[int] = None; creator_balance_lamports: Optional[int] = None
        max_retries = 3; base_delay = 0.5
        for attempt in range(max_retries):
            self.logger.debug(f"Attempt {attempt + 1}/{max_retries} to get supply for {mint_pk}")
            try:
                supply_resp = await self.solana_client.get_token_supply(mint_pk, commitment=Confirmed)
                if supply_resp is not None and hasattr(supply_resp, 'amount'):
                    try: total_supply_lamports = int(supply_resp.amount); break
                    except (ValueError, TypeError): self.logger.warning(f"Could not parse supply amount ('{supply_resp.amount}') for {mint_pk}. Retrying...")
                elif supply_resp is None: self.logger.warning(f"Supply account {mint_pk} not found (attempt {attempt + 1}). Retrying...")
                else: self.logger.warning(f"Unexpected supply response {mint_pk} (attempt {attempt + 1}): {supply_resp}. Retrying...")
            except SolanaRpcException as rpc_e:
                if "could not find account" in str(rpc_e).lower(): self.logger.warning(f"Supply RPC error (not found?) {mint_pk} (attempt {attempt + 1}): {rpc_e}. Retrying...")
                else: self.logger.error(f"RPC Error fetching supply {mint_pk} (attempt {attempt + 1}): {rpc_e}")
            except Exception as e: self.logger.error(f"Unexpected error fetching supply {mint_pk} (attempt {attempt + 1}): {e}", exc_info=True)
            if attempt < max_retries - 1: await asyncio.sleep(base_delay * (1.5**attempt))
            else: self.logger.error(f"Failed get supply {mint_pk} after {max_retries} attempts. Skipping check."); return True
        if total_supply_lamports is None or total_supply_lamports <= 0: self.logger.warning(f"Invalid supply ({total_supply_lamports}) {mint_pk}. Skipping check."); return True
        creator_ata = Wallet.get_associated_token_address(mint=mint_pk, owner=creator_pk)
        for attempt in range(max_retries):
            self.logger.debug(f"Attempt {attempt + 1}/{max_retries} get balance creator ATA {creator_ata}")
            try:
                balance_resp = await self.solana_client.get_token_account_balance(creator_ata, commitment=Processed)
                if balance_resp is not None and hasattr(balance_resp, 'amount'):
                    try: creator_balance_lamports = int(balance_resp.amount); break
                    except (ValueError, TypeError): self.logger.warning(f"Could not parse balance ('{balance_resp.amount}') {creator_ata}. Assuming 0."); creator_balance_lamports = 0; break
                elif balance_resp is None: self.logger.debug(f"Creator ATA {creator_ata} not found (attempt {attempt + 1}). Assuming 0."); creator_balance_lamports = 0; break
                else: self.logger.warning(f"Unexpected balance response {creator_ata} (attempt {attempt + 1}): {balance_resp}. Retrying...")
            except SolanaRpcException as rpc_e:
                 if "could not find account" in str(rpc_e).lower(): self.logger.warning(f"Balance RPC error (not found?) {creator_ata} (attempt {attempt + 1}). Assuming 0."); creator_balance_lamports = 0; break
                 else: self.logger.error(f"RPC Error fetching balance {creator_ata} (attempt {attempt + 1}): {rpc_e}")
            except Exception as e: self.logger.error(f"Unexpected error fetching balance {creator_ata} (attempt {attempt + 1}): {e}", exc_info=True)
            if attempt < max_retries - 1: await asyncio.sleep(base_delay * (1.5**attempt))
            else: self.logger.error(f"Failed get balance {creator_ata} after {max_retries} attempts. Assuming 0."); creator_balance_lamports = 0
        if creator_balance_lamports is None: creator_balance_lamports = 0
        try:
            creator_percentage = creator_balance_lamports / total_supply_lamports if total_supply_lamports > 0 else 0
            self.logger.info(f"Creator {creator_pk} holds {creator_balance_lamports}/{total_supply_lamports} ({creator_percentage:.2%})")
            if creator_percentage > self.rug_max_creator_hold_pct: self.logger.warning(f"Creator holding check FAILED: {creator_percentage:.2%} > {self.rug_max_creator_hold_pct:.2%}."); return False
            self.logger.info("Creator holding check PASSED."); return True
        except Exception as calc_e: self.logger.error(f"Error calculating creator percentage for {token_info.symbol}: {calc_e}", exc_info=True); return True

    # --- _check_post_buy_conditions ---
    # (Keep as provided in Message #14 - assumes self.logger is available)
    async def _check_post_buy_conditions(self, token_info: TokenInfo, initial_trade_result: TradeResult, current_curve_state: Optional[BondingCurveState] = None) -> Optional[str]:
        if not (self.rug_check_price_drop or self.rug_check_liquidity_drop): return None
        try:
            if current_curve_state is None: current_curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve, commitment=Processed)
            if not current_curve_state: self.logger.warning(f"Post-buy check: No curve state for {token_info.symbol}."); return None
            original_buy_price = initial_trade_result.price; initial_sol_liq = initial_trade_result.initial_sol_liquidity
            if self.rug_check_price_drop and original_buy_price is not None and original_buy_price > 1e-18:
                try: current_price = current_curve_state.calculate_price()
                except Exception: current_price = None
                if current_price is not None and current_price < original_buy_price:
                    price_drop_pct = (original_buy_price - current_price) / original_buy_price if original_buy_price else 1.0
                    if price_drop_pct > self.rug_price_drop_pct: self.logger.warning(f"RUG CHECK: Price drop {price_drop_pct:.1%} > {self.rug_price_drop_pct:.1%} for {token_info.symbol}"); return f"Price drop {price_drop_pct:.1%}"
            if self.rug_check_liquidity_drop and initial_sol_liq is not None and initial_sol_liq > 0:
                current_sol_reserves = getattr(current_curve_state, 'virtual_sol_reserves', None)
                if current_sol_reserves is not None:
                    current_sol_int = int(current_sol_reserves); initial_sol_int = int(initial_sol_liq);
                    if current_sol_int < initial_sol_int:
                         liquidity_drop_pct = (initial_sol_int - current_sol_int) / initial_sol_int if initial_sol_int else 1.0;
                         if liquidity_drop_pct > self.rug_liquidity_drop_pct: self.logger.warning(f"RUG CHECK: Liq drop {liquidity_drop_pct:.1%} > {self.rug_liquidity_drop_pct:.1%} for {token_info.symbol} ({current_sol_int} < {initial_sol_int})"); return f"Liquidity drop {liquidity_drop_pct:.1%}"
                # Note: The original code had an `else` here, which might have been unintended. Removed it.
                # Check if the original code intended to log only if reserves *were* found but drop wasn't triggered.
                # else: logger.warning(f"Post-buy check: No virtual_sol_reserves for {token_info.symbol}.") # Original placement
            return None # No rug condition met
        except Exception as e: self.logger.error(f"Error during post-buy check for {token_info.symbol}: {e}", exc_info=True); return None


    # --- _monitor_bonding_curve_token ---
    # (Keep as provided in Message #14 - assumes self.logger is available)
    async def _monitor_bonding_curve_token(self, token_info: TokenInfo, initial_trade_result: TradeResult):
        mint_str = str(token_info.mint)
        initial_buy_price: Optional[float] = initial_trade_result.price if initial_trade_result else None
        current_monitor_state = self.token_states.get(mint_str)
        sell_reason: Optional[str] = None

        # Exit if not in the correct state to monitor
        if current_monitor_state != TokenState.ON_BONDING_CURVE:
            return

        try:
            # Fetch current state
            current_curve_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(
                token_info.bonding_curve, commitment=Processed
            )
            if not current_curve_state:
                self.logger.warning(f"Monitor curve: No curve state for {mint_str[:6]}.")
                return # Cannot monitor without state

            # Check for Raydium transition
            current_sol_reserves = getattr(current_curve_state, 'virtual_sol_reserves', 0)
            if current_sol_reserves >= self.SOL_THRESHOLD_FOR_RAYDIUM and self.raydium_enabled:
                self.logger.info(f"*** {token_info.symbol} ({mint_str[:6]}) crossed Raydium threshold. Transitioning. ***")
                if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE:
                    self.token_states[mint_str] = TokenState.ON_RAYDIUM
                return # Stop curve monitoring

            # Skip sell checks if in Marry mode
            if self.marry_mode_flag:
                return

            # --- Perform Sell Condition Checks (Rug/TP/SL) ---
            rug_check_result = await self._check_post_buy_conditions(token_info, initial_trade_result, current_curve_state)
            if rug_check_result:
                sell_reason = f"Rug Check: {rug_check_result}"
                self.logger.warning(f"Rug condition detected for {mint_str[:6]}: {rug_check_result}")
            else:
                # Check TP/SL only if not rugged
                try:
                    current_price = current_curve_state.calculate_price()
                except Exception as price_e:
                    self.logger.warning(f"Error calculating price for TP/SL check {mint_str[:6]}: {price_e}")
                    current_price = None

                if initial_buy_price is not None and initial_buy_price > 1e-18 and current_price is not None:
                    profit_target_price = initial_buy_price * (1 + self.sell_profit_threshold)
                    stop_loss_price = initial_buy_price * (1 - self.sell_stoploss_threshold)
                    self.logger.debug(f"{mint_str[:6]}: Curve Price={current_price:.9f}, Buy={initial_buy_price:.9f}, TP={profit_target_price:.9f}, SL={stop_loss_price:.9f}")
                    if current_price >= profit_target_price:
                        sell_reason = f"Profit Target ({current_price:.9f} >= {profit_target_price:.9f})"
                    elif current_price < stop_loss_price:
                        sell_reason = f"Stop-Loss ({current_price:.9f} < {stop_loss_price:.9f})"
            # --- End Sell Condition Checks ---

            # --- Execute Sell if Triggered ---
            if sell_reason:
                self.logger.warning(f"*** Sell trigger (curve) for {token_info.symbol}: {sell_reason} ***")
                # Double-check state before executing sell
                if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE:
                    self.token_states[mint_str] = TokenState.SELLING_CURVE
                    self.logger.info(f"Executing bonding curve sell for {mint_str[:6]}...")
                    seller_result = await self.seller.execute(token_info=token_info, buyer_result=initial_trade_result)

                    # --- Process Sell Result ---
                    if seller_result.success:
                        self.logger.info(f"Curve sell successful {token_info.symbol}: {sell_reason}. Tx: {seller_result.tx_signature}")
                        self.token_states[mint_str] = TokenState.SOLD_CURVE
                        self._log_trade("sell_curve", token_info, seller_result.price, seller_result.amount, seller_result.tx_signature)

                        # --- FIX: Corrected Cleanup Block ---
                        if self.cleanup_mode == "after_sell":
                            self.logger.info(f"Performing cleanup for sold token {mint_str[:6]}")
                            try:
                                await handle_cleanup_after_sell(
                                    self.solana_client, self.wallet, token_info.mint,
                                    self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee
                                )
                                self.logger.info(f"Cleanup attempt finished for {mint_str[:6]}.")
                            except Exception as clean_e:
                                # Use clean_e in the log message
                                self.logger.error(f"Error during after_sell cleanup for {mint_str[:6]}: {clean_e}", exc_info=True)
                                # Update state to reflect cleanup failure
                                self.token_states[mint_str] = TokenState.CLEANUP_FAILED
                        # --- END FIX ---

                    else: # Sell failed
                        self.logger.error(f"Curve sell failed {token_info.symbol}: {seller_result.error_message}")
                        self.token_states[mint_str] = TokenState.SELL_CURVE_FAILED
                    # --- End Process Sell Result ---

                else: # State changed before sell could execute
                    self.logger.warning(f"Sell trigger {mint_str[:6]} detected but state changed to {self.token_states.get(mint_str)}. Aborting sell.")
            # --- End Execute Sell ---

        except asyncio.CancelledError:
            self.logger.warning(f"Monitoring task curve {mint_str[:6]} cancelled.")
            # Optionally set state to error if not already terminal
            if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE:
                 self.token_states[mint_str] = TokenState.MONITORING_ERROR
        except Exception as e:
            self.logger.error(f"Error monitoring curve {mint_str}: {e}", exc_info=True)
            # Set error state only if it was still being monitored
            if self.token_states.get(mint_str) == TokenState.ON_BONDING_CURVE:
                self.token_states[mint_str] = TokenState.MONITORING_ERROR


    # --- Corrected _monitor_raydium_token ---
    # (Paste the corrected _monitor_raydium_token method from Message #14 here)
    async def _monitor_raydium_token(self, token_info: TokenInfo, initial_trade_result: TradeResult):
        if not self.raydium_enabled or not self.raydium_seller:
            return # Cannot monitor if disabled or seller not initialized

        mint_str = str(token_info.mint)
        initial_buy_price: Optional[float] = initial_trade_result.price if initial_trade_result else None
        current_monitor_state = self.token_states.get(mint_str)

        # Exit if not in the correct state
        if current_monitor_state != TokenState.ON_RAYDIUM:
            return

        sell_reason: Optional[str] = None
        try:
            # Check if data fetcher is available
            if self.get_raydium_pool_data is None:
                 self.logger.warning("Raydium pool data function not available for monitoring.")
                 return

            # Get pool data
            raydium_data = await self.get_raydium_pool_data(mint_str)
            if raydium_data is None:
                self.logger.debug(f"Monitor Raydium: No DEX data for {mint_str[:6]} this check.")
                return # Pool might not be ready yet

            # Validate price
            raydium_price = raydium_data.get("price")
            if raydium_price is None or not isinstance(raydium_price, (float, int)) or raydium_price <= 0:
                self.logger.warning(f"Monitor Raydium: Invalid DEX price ({raydium_price}) for {mint_str[:6]}.")
                return # Invalid data, try again later

            # Skip sell checks if in Marry mode
            if self.marry_mode_flag:
                return

            # --- Check TP/SL based on Raydium price ---
            if initial_buy_price is not None and initial_buy_price > 1e-18:
                profit_target_price = initial_buy_price * (1 + self.sell_profit_threshold)
                stop_loss_price = initial_buy_price * (1 - self.sell_stoploss_threshold)
                self.logger.debug(f"{mint_str[:6]}: DEX Price={raydium_price:.9f}, Buy={initial_buy_price:.9f}, TP={profit_target_price:.9f}, SL={stop_loss_price:.9f}")
                if raydium_price >= profit_target_price:
                    sell_reason = f"DEX Profit target ({raydium_price:.9f} >= {profit_target_price:.9f})"
                elif raydium_price < stop_loss_price:
                    sell_reason = f"DEX Stop-loss ({raydium_price:.9f} < {stop_loss_price:.9f})"
            # --- End Check TP/SL ---

            # --- Execute Sell if Triggered ---
            if sell_reason:
                self.logger.warning(f"*** Sell trigger on DEX for {token_info.symbol}: {sell_reason} ***")
                # Double-check state
                if self.token_states.get(mint_str) == TokenState.ON_RAYDIUM:
                    self.token_states[mint_str] = TokenState.SELLING_RAYDIUM
                    self.logger.info(f"Executing Raydium sell for {mint_str[:6]}...")
                    user_ata = Wallet.get_associated_token_address(self.wallet.pubkey, token_info.mint)
                    amount_to_sell_lamports: int = 0

                    # Get current balance
                    try:
                        balance_resp = await self.solana_client.get_token_account_balance(user_ata, commitment=Confirmed)
                        if balance_resp is not None and hasattr(balance_resp, 'amount'):
                            try: amount_to_sell_lamports = int(balance_resp.amount)
                            except (ValueError, TypeError): self.logger.error(f"Failed parse balance amount ('{balance_resp.amount}') {user_ata}."); self.token_states[mint_str]=TokenState.SELL_RAYDIUM_FAILED; return
                        else: self.logger.error(f"Failed get balance object/amount {user_ata}."); self.token_states[mint_str]=TokenState.SELL_RAYDIUM_FAILED; return
                    except Exception as balance_e: self.logger.error(f"Error fetching balance Raydium ATA {user_ata}: {balance_e}"); self.token_states[mint_str]=TokenState.SELL_RAYDIUM_FAILED; return

                    # --- FIX START: Corrected Sell Execution Block Structure ---
                    # Check if sell is possible
                    if amount_to_sell_lamports > 0 and self.raydium_seller:
                        # Attempt the sell
                        sell_result = await self.raydium_seller.execute(token_info, amount_to_sell_lamports)

                        # Process the result of the sell attempt
                        if sell_result.success:
                            self.logger.info(f"Raydium sell successful {token_info.symbol}. Tx: {sell_result.tx_signature}, Est Price: {sell_result.price:.9f}")
                            self.token_states[mint_str] = TokenState.SOLD_RAYDIUM
                            self._log_trade("sell_raydium", token_info, sell_result.price, sell_result.amount, sell_result.tx_signature)

                            # Attempt cleanup only after successful sell
                            if self.cleanup_mode == "after_sell":
                                self.logger.info(f"Performing cleanup for Raydium sold token {mint_str[:6]}")
                                try:
                                    await handle_cleanup_after_sell(
                                        self.solana_client, self.wallet, token_info.mint,
                                        self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee
                                    )
                                    self.logger.info(f"Cleanup attempt finished for Raydium sell {mint_str[:6]}.")
                                except Exception as clean_e:
                                    self.logger.error(f"Error during after_sell cleanup for Raydium sell {mint_str[:6]}: {clean_e}", exc_info=True)
                                    # Don't change state from SOLD_RAYDIUM, just log cleanup failure
                                    # self.token_states[mint_str] = TokenState.CLEANUP_FAILED # Optional: Add specific state?
                        else:
                            # Sell attempt failed
                            self.logger.error(f"Raydium sell failed {token_info.symbol}: {sell_result.error_message}")
                            self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED

                    elif amount_to_sell_lamports <= 0:
                        # Cannot sell if balance is zero
                        self.logger.warning(f"Raydium sell trigger for {mint_str[:6]} but balance is zero.")
                        self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED # Treat as failure

                    elif not self.raydium_seller:
                        # Cannot sell if seller component is missing
                        self.logger.error(f"Raydium sell trigger for {mint_str[:6]} but RaydiumSeller not initialized.")
                        self.token_states[mint_str] = TokenState.SELL_RAYDIUM_FAILED
                    # --- FIX END: Corrected Sell Execution Block Structure ---

                else: # State changed before sell could execute
                    self.logger.warning(f"Raydium sell trigger {mint_str[:6]} detected but state changed to {self.token_states.get(mint_str)}. Aborting sell.")
            # --- End Execute Sell ---

        except asyncio.CancelledError:
            self.logger.warning(f"Monitoring task Raydium {mint_str[:6]} cancelled.")
            # Update state if it was being monitored
            if self.token_states.get(mint_str) == TokenState.ON_RAYDIUM:
                 self.token_states[mint_str] = TokenState.MONITORING_ERROR
        except Exception as e:
            self.logger.error(f"Error monitoring Raydium {mint_str}: {e}", exc_info=True)
            # Update state if it was being monitored
            if self.token_states.get(mint_str) == TokenState.ON_RAYDIUM:
                 self.token_states[mint_str] = TokenState.MONITORING_ERROR

    # --- Static methods for saving/logging ---
    # Use self.logger within static methods if needed, or keep module logger
    @staticmethod
    def _save_token_info(token_info: TokenInfo) -> None:
        """Saves token info to a JSON file."""
        os.makedirs("trades", exist_ok=True)
        file_name = os.path.join("trades", f"{str(token_info.mint)}.json")
        try:
            serializable_data = dataclass_to_serializable_dict(token_info)
            with open(file_name, "w", encoding="utf-8") as file_handle:
                json.dump(serializable_data, file_handle, indent=2, ensure_ascii=False)
            logger.debug(f"Token info saved: {file_name}") # Use module logger
        except Exception as e:
            logger.error(f"Error saving token info {file_name}: {e}", exc_info=True)

    @staticmethod
    def _log_trade(action: str, token_info: TokenInfo, price: Optional[float], amount: Optional[float], tx_hash: Optional[str]) -> None:
        """Logs trade details to a file."""
        os.makedirs("trades", exist_ok=True)
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(), "action": action,
            "token_address": str(token_info.mint), "symbol": token_info.symbol,
            "price_sol_per_token": price, "amount_tokens_or_sol": amount,
            "tx_hash": str(tx_hash) if tx_hash else None
        }
        try:
            with open("trades/trades.log", "a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except IOError as e:
            logger.error(f"Failed to write trade log: {e}") # Use module logger