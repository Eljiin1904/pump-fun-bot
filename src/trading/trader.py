# src/trading/trader.py (Clean Syntax & Integrated Fixes)

import os
import sys
import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from typing import Optional, Set, Dict, Any, List

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
from solana.rpc.commitment import Confirmed, Processed

# --- Path Setup ---
# (Ensure this correctly points to your project root)
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(script_dir))
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- Project Imports ---
try:
    from src.core.client import SolanaClient
    from src.core.wallet import Wallet
    from src.core.priority_fee.manager import PriorityFeeManager
    from src.core.curve import BondingCurveManager, LAMPORTS_PER_SOL, BondingCurveState
    from src.monitoring.listener_factory import ListenerFactory
    from src.trading.base import TokenInfo, TradeResult, TokenState # Ensure TokenState defined/imported
    from src.trading.buyer import TokenBuyer
    from src.trading.seller import TokenSeller
    # Import RaydiumSeller conditionally based on previous checks
    try:
        from src.trading.raydium_seller import RaydiumSeller
        RAYDIUM_SELLER_AVAILABLE = True
    except ImportError:
        RaydiumSeller = None # Define as None if import fails
        RAYDIUM_SELLER_AVAILABLE = False
    from src.data.raydium_data import get_raydium_pool_data
    from src.utils.logger import get_logger
    from src.utils.audit_logger import TradeAuditLogger # Ensure correct name
except ImportError as e:
    print(f"[Critical Import Error in trader.py] {e}")
    sys.exit(1)

logger = get_logger(__name__)
# SOL_MINT_ADDRESS = Pubkey.from_string("So11111111111111111111111111111111111111112") # Use from pubkeys?

class PumpTrader:
    """Orchestrates the two-stage trading process (bonding curve -> Raydium)."""

    # --- Use the signature from your v3 code ---
    def __init__(self,
                 client: SolanaClient,
                 rpc_endpoint: str,
                 wss_endpoint: str,
                 private_key: str,
                 enable_dynamic_fee: bool,
                 enable_fixed_fee: bool,
                 fixed_fee: int,
                 extra_fee: int,
                 hard_cap: int,
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
                 sol_threshold_for_raydium: float = 300.0):

        logger.info("Initializing PumpTrader...")
        # --- Use standard indentation and assignment ---
        self.solana_client = client
        self.wss_endpoint = wss_endpoint
        self.listener_type = listener_type

        try:
            self.wallet = Wallet(private_key)
            logger.info(f"Wallet initialized: {self.wallet.pubkey}")
        except Exception as e:
            raise ValueError("Invalid private key for Wallet") from e

        # Extract raw client safely using public property
        try:
            raw_async_client = self.solana_client.async_client # Assumes async_client property exists
            if not raw_async_client: raise RuntimeError("async_client property returned None")
        except AttributeError: raise RuntimeError("SolanaClient missing 'async_client' property")
        except Exception as e: raise RuntimeError(f"Error getting raw async_client: {e}")

        # Initialize PFM (No rpc_endpoint passed)
        try:
            self.priority_fee_manager = PriorityFeeManager(
                client=raw_async_client,
                enable_dynamic_fee=enable_dynamic_fee,
                enable_fixed_fee=enable_fixed_fee,
                fixed_fee=fixed_fee,
                extra_fee=extra_fee,
                hard_cap=hard_cap
            )
            logger.info("PriorityFeeManager initialized.")
        except Exception as e:
            raise RuntimeError("Failed to initialize PriorityFeeManager") from e

        self.curve_manager = BondingCurveManager(self.solana_client)
        self.audit_logger = TradeAuditLogger() # Ensure class name is correct

        # Assign configuration parameters
        self.rpc_endpoint = rpc_endpoint # Store if needed
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
        self.rug_max_creator_hold_pct = rug_max_creator_hold_pct / 100.0
        self.rug_check_price_drop = rug_check_price_drop
        self.rug_price_drop_pct = rug_price_drop_pct / 100.0
        self.rug_check_liquidity_drop = rug_check_liquidity_drop
        self.rug_liquidity_drop_pct = rug_liquidity_drop_pct / 100.0
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close = cleanup_force_close
        self.cleanup_priority_fee = cleanup_priority_fee
        self.SOL_THRESHOLD_FOR_RAYDIUM = int(sol_threshold_for_raydium * LAMPORTS_PER_SOL)

        # Initialize trading components
        try:
            logger.debug("Initializing TokenBuyer...")
            self.buyer = TokenBuyer(client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager, fee_manager=self.priority_fee_manager, buy_amount=self.buy_amount, slippage=self.buy_slippage, max_retries=self.max_buy_retries, confirm_timeout=self.confirm_timeout)
            logger.debug("Initializing TokenSeller...")
            self.seller = TokenSeller(client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager, fee_manager=self.priority_fee_manager, slippage=self.sell_slippage, max_retries=self.max_retries)
            logger.debug("Initializing RaydiumSeller...")
            if RAYDIUM_SELLER_AVAILABLE and RaydiumSeller: # Use flag and class
                self.raydium_seller = RaydiumSeller(client=self.solana_client, wallet=self.wallet, fee_manager=self.priority_fee_manager, sell_slippage_bps=int(self.sell_slippage * 10000), max_retries=self.max_retries)
            else:
                self.raydium_seller = None
                logger.warning("RaydiumSeller not available or import failed, Raydium selling disabled.")
        except Exception as e_init:
            raise RuntimeError(f"Failed trading component init: {e_init}") from e_init

        # Initialize state variables
        self.marry_mode_flag = False
        self.yolo_mode_flag = False
        self.token_queue = asyncio.Queue()
        self.token_states: Dict[str, TokenState] = {}
        self.token_infos: Dict[str, TokenInfo] = {}
        self.token_buy_results: Dict[str, TradeResult] = {}
        self.traded_mints_session: Set[Pubkey] = set()
        self.profit_targets: Dict[str, bool] = {}
        self.profit_target_times: Dict[str, float] = {}
        self.peak_reserves: Dict[str, int] = {}
        self.drop_trigger_after_profit: float = 0.05
        self.drop_window_sec: float = 30.0
        self.active_monitoring_tasks: Dict[str, asyncio.Task] = {}
        self.shutdown_event = asyncio.Event()
        self.listener_task: Optional[asyncio.Task] = None
        self.processor_task: Optional[asyncio.Task] = None

        logger.info("PumpTrader initialization complete.")


    async def start_tasks(self, match_string: Optional[str] = None, bro_address: Optional[str] = None,
                          marry_mode: bool = False, yolo_mode: bool = False):
        """Initializes listener and starts main processing loop."""
        self.marry_mode_flag = marry_mode
        self.yolo_mode_flag = yolo_mode
        logger.info(f"Starting trader. Marry: {marry_mode}, YOLO: {yolo_mode}, Match: '{match_string}', Bro: '{bro_address}'")

        try:
            # 1. Create listener instance
            listener = ListenerFactory.create_listener(
                listener_type=self.listener_type,
                client=self.solana_client,
                wss_endpoint=self.wss_endpoint
            )
            # 2. Create listener task (calling start)
            self.listener_task = asyncio.create_task(
                listener.start(
                    token_queue=self.token_queue,
                    match_string=match_string,
                    creator_address=bro_address
                ),
                name="TokenListener"
            )
            logger.info(f"Listener task ({self.listener_type}) started.")
        except Exception as e:
            logger.error(f"Failed to create or start listener: {e}", exc_info=True)
            return # Cannot continue if listener fails

        # Start processor task
        self.processor_task = asyncio.create_task(self._process_token_queue(), name="TokenProcessor")

        # Wait loop
        try:
            shutdown_waiter = asyncio.create_task(self.shutdown_event.wait(), name="ShutdownWaiter")
            tasks_to_wait_on = [t for t in [self.listener_task, self.processor_task, shutdown_waiter] if t]
            if not tasks_to_wait_on:
                logger.warning("No primary tasks were created.")
                return

            logger.info("Trader running. Waiting for events or shutdown...")
            # Wait for the first task to complete
            done, pending = await asyncio.wait(
                 tasks_to_wait_on,
                 return_when=asyncio.FIRST_COMPLETED
             )

            # Process the task(s) that finished
            for task in done:
                 task_name = task.get_name() if hasattr(task, 'get_name') else "Unknown Task"
                 if task_name == "ShutdownWaiter":
                     logger.info("Shutdown event triggered.")
                 else: # Listener or Processor finished unexpectedly
                     try:
                         task.result() # Raise exception if task failed
                         logger.warning(f"Task {task_name} finished unexpectedly without apparent error.")
                     except asyncio.CancelledError:
                         logger.info(f"Task {task_name} was cancelled.")
                     except Exception as e_done:
                         logger.error(f"Task {task_name} failed: {e_done}", exc_info=True)
                         # Trigger shutdown if a critical task fails
                         await self.stop()

        except asyncio.CancelledError:
            logger.info("start_tasks received cancellation signal.")
        finally:
            # Ensure cleanup happens
            logger.info("start_tasks exiting, ensuring stop...")
            await self.stop() # Call stop method for graceful shutdown
            logger.info("start_tasks cleanup complete.")


    # --- Other Methods ---
    # --- PASTE THE FULL, CLEAN IMPLEMENTATIONS of ALL your other methods ---
    # --- from your v3_stage2 trader.py HERE. Ensure correct syntax ---
    # --- and indentation. Use the method signatures from V3. ---

    async def _process_token_queue(self):
        # !!! PASTE YOUR CLEAN V3 _process_token_queue CODE HERE !!!
        logger.info("!!! _process_token_queue needs V3 implementation !!!")
        await asyncio.sleep(3600) # Remove this line after pasting

    async def _handle_new_token(self, token_info: TokenInfo):
        # !!! PASTE YOUR CLEAN V3 _handle_new_token CODE HERE !!!
        logger.info(f"!!! _handle_new_token needs V3 implementation for {token_info.symbol} !!!")
        await asyncio.sleep(1) # Remove this line

    async def _monitor_bonding_curve_token(self, token_info: TokenInfo, initial_trade_result: Optional[TradeResult] = None): # Added optional arg
        # !!! PASTE YOUR CLEAN V3 _monitor_bonding_curve_token CODE HERE !!!
        logger.info(f"!!! _monitor_bonding_curve_token needs V3 implementation for {token_info.symbol} !!!")
        await asyncio.sleep(1) # Remove this line

    async def _monitor_raydium_token(self, token_info: TokenInfo, initial_trade_result: Optional[TradeResult] = None): # Added optional arg
        # !!! PASTE YOUR CLEAN V3 _monitor_raydium_token CODE HERE !!!
        logger.info(f"!!! _monitor_raydium_token needs V3 implementation for {token_info.symbol} !!!")
        await asyncio.sleep(1) # Remove this line

    async def _log_trade(self, action: str, token_info: TokenInfo, price: Optional[float], amount: Optional[float], tx_hash: Optional[str]):
         # !!! PASTE YOUR CLEAN V3 _log_trade CODE HERE !!!
         # Example using audit logger:
         try: buy_res=self.token_buy_results.get(str(token_info.mint)) if 'sell' in action else None; cur_res=TradeResult(True,price,amount,tx_hash); await self.audit_logger.log_trade_event(event_type=action.upper(), token_info=token_info, trade_result=cur_res, buy_trade_result=buy_res)
         except Exception as e: logger.error(f"Audit log fail: {e}")

    # !!! PASTE ALL OTHER NECESSARY V3 METHODS HERE (_check_*, _cleanup_*, _save_token_info, etc.) !!!

    async def stop(self):
        """Gracefully stops the trader."""
        # !!! PASTE YOUR CLEAN V3 stop CODE HERE !!!
        logger.info("Stopping trader (IMPLEMENTATION NEEDED)")
        self.shutdown_event.set()
        # Add cancellation logic for tasks
        # Add client closing logic
        logger.info("Dummy stop method executed.")


# --- END OF FILE ---