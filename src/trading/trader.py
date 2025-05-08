# src/trading/trader.py (Complete v3 Version with Start Signature Fix)

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, Set, Dict, List, Coroutine, Callable, Any

from solders.pubkey import Pubkey

# --- Core Project Imports ---
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager
from src.core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL
from src.core.instruction_builder import InstructionBuilder
from src.core.transactions import build_and_send_transaction, TransactionSendResult  # Import result type too
from src.core.pubkeys import SolanaProgramAddresses  # For cleanup maybe

# --- Monitoring Imports ---
from src.monitoring.listener_factory import ListenerFactory, BaseListener

# --- Trading Imports ---
from src.trading.base import TokenInfo, TradeResult, TokenState  # Enum should have Raydium states
from src.trading.buyer import TokenBuyer
from src.trading.seller import TokenSeller
# --- V3 Imports ---
from src.tools.raydium_amm import RaydiumAMMInfoFetcher, RaydiumSwapWrapper, RaydiumPoolInfo
from src.trading.raydium_seller import RaydiumSeller
# --- End V3 Imports ---

# --- Utility Imports ---
from src.utils.logger import get_logger
from src.utils.audit_logger import AuditLogger

# --- Cleanup Imports ---
from src.cleanup.modes import (
    handle_cleanup_after_sell,
    handle_cleanup_after_failure,
    handle_cleanup_post_session
)
# --- End Cleanup Imports ---

# --- Solana Imports ---
from solana.rpc.commitment import Confirmed, Processed

logger = get_logger(__name__)

# --- Default Values ---
DEFAULT_MAX_TOKEN_AGE_SECONDS = 45.0
DEFAULT_WAIT_TIME_AFTER_CREATION_SECONDS = 3.0
DEFAULT_BUY_SLIPPAGE_BPS = 1500
DEFAULT_SELL_SLIPPAGE_BPS = 2500
DEFAULT_RAYDIUM_SELL_SLIPPAGE_BPS = 100
DEFAULT_TAKE_PROFIT_MULTIPLIER = 15.0
DEFAULT_STOP_LOSS_PERCENTAGE = 70.0  # This is the loss %, so sell at 1.0 - 0.7 = 0.3 * buy_value
DEFAULT_RUG_MAX_CREATOR_HOLD_PCT = 20.0
DEFAULT_RUG_PRICE_DROP_PCT = 40.0
DEFAULT_RUG_LIQUIDITY_DROP_PCT = 50.0
DEFAULT_SOL_THRESHOLD_FOR_RAYDIUM = 300.0
DEFAULT_RAYDIUM_POOL_FETCH_RETRIES = 5
DEFAULT_RAYDIUM_POOL_FETCH_DELAY_SECONDS = 10.0
DEFAULT_MONITORING_INTERVAL_SECONDS = 5.0
DEFAULT_CLEANUP_MODE = 'after_sell'
DEFAULT_CLEANUP_WITH_PRIORITY_FEE = False


class PumpTrader:
    """
    Orchestrates v3 trading: bonding curve -> Raydium transition, monitoring, selling.
    Handles run_once, marry, and yolo modes. Integrates cleanup logic.
    """

    def __init__(self,
                 client: SolanaClient,
                 wallet: Wallet,
                 priority_fee_manager: PriorityFeeManager,
                 config: Dict[str, Any]):  # Takes the full config dict

        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.config = config  # Store the loaded configuration
        logger.info(f"Initializing PumpTrader (v3) for wallet: {self.wallet.pubkey}")

        # --- Core Components ---
        self.curve_manager = BondingCurveManager(self.client)
        self.audit_logger = AuditLogger()

        # --- Configuration Parameters (Extract from config dict) ---
        self.buy_amount_sol: float = float(config.get('BUY_AMOUNT_SOL', 0.01))
        self.max_token_age_seconds: float = float(config.get('MAX_TOKEN_AGE_SECONDS', DEFAULT_MAX_TOKEN_AGE_SECONDS))
        self.wait_time_after_creation_seconds: float = float(
            config.get('WAIT_TIME_AFTER_CREATION_SECONDS', DEFAULT_WAIT_TIME_AFTER_CREATION_SECONDS))

        buy_slippage_bps: int = int(config.get('BUY_SLIPPAGE_BPS', DEFAULT_BUY_SLIPPAGE_BPS))
        sell_slippage_bps: int = int(config.get('SELL_SLIPPAGE_BPS', DEFAULT_SELL_SLIPPAGE_BPS))
        raydium_sell_slippage_bps: int = int(config.get('RAYDIUM_SELL_SLIPPAGE_BPS', DEFAULT_RAYDIUM_SELL_SLIPPAGE_BPS))

        self.take_profit_multiplier: float = float(config.get('TAKE_PROFIT_MULTIPLIER', DEFAULT_TAKE_PROFIT_MULTIPLIER))
        # Calculate the multiplier for stop loss (e.g., 70% loss means sell if value <= 0.3 * buy_value)
        self.stop_loss_value_multiplier: float = 1.0 - (
                    float(config.get('STOP_LOSS_PERCENTAGE', DEFAULT_STOP_LOSS_PERCENTAGE)) / 100.0)

        # Rug check configs
        self.rug_check_creator_enabled: bool = bool(config.get('RUG_CHECK_CREATOR_ENABLED', True))
        # Convert percentage from config to decimal for comparison
        self.rug_max_creator_hold_pct: float = float(
            config.get('RUG_MAX_CREATOR_HOLD_PCT', DEFAULT_RUG_MAX_CREATOR_HOLD_PCT)) / 100.0
        self.rug_check_price_drop_enabled: bool = bool(config.get('RUG_CHECK_PRICE_DROP_ENABLED', True))
        self.rug_price_drop_pct_from_peak: float = float(
            config.get('RUG_PRICE_DROP_PCT', DEFAULT_RUG_PRICE_DROP_PCT)) / 100.0
        self.rug_check_liquidity_drop_enabled: bool = bool(config.get('RUG_CHECK_LIQUIDITY_DROP_ENABLED', True))
        self.rug_liquidity_drop_pct_from_peak: float = float(
            config.get('RUG_LIQUIDITY_DROP_PCT', DEFAULT_RUG_LIQUIDITY_DROP_PCT)) / 100.0

        self.monitoring_interval_seconds: float = float(
            config.get('MONITORING_INTERVAL_SECONDS', DEFAULT_MONITORING_INTERVAL_SECONDS))

        # V3 Specific Configs
        self.raydium_enabled: bool = bool(config.get('RAYDIUM_ENABLED', True))
        self.sol_threshold_for_raydium_lamports: int = int(
            float(config.get('SOL_THRESHOLD_FOR_RAYDIUM', DEFAULT_SOL_THRESHOLD_FOR_RAYDIUM)) * LAMPORTS_PER_SOL)
        self.raydium_pool_fetch_retries = int(
            config.get('RAYDIUM_POOL_FETCH_RETRIES', DEFAULT_RAYDIUM_POOL_FETCH_RETRIES))
        self.raydium_pool_fetch_delay = float(
            config.get('RAYDIUM_POOL_FETCH_DELAY_SECONDS', DEFAULT_RAYDIUM_POOL_FETCH_DELAY_SECONDS))

        # Cleanup Config
        self.cleanup_mode: str = str(config.get('CLEANUP_MODE', DEFAULT_CLEANUP_MODE))
        self.cleanup_use_priority_fee: bool = bool(
            config.get('CLEANUP_WITH_PRIORITY_FEE', DEFAULT_CLEANUP_WITH_PRIORITY_FEE))

        # --- Trading Components ---
        self.buyer = TokenBuyer(
            client=self.client, wallet=self.wallet, curve_manager=self.curve_manager,
            fee_manager=self.priority_fee_manager, buy_amount_sol=self.buy_amount_sol,
            slippage_bps=buy_slippage_bps,
            max_retries=int(config.get('MAX_BUY_RETRIES', 3)),
            confirm_timeout_seconds=int(config.get('CONFIRM_TIMEOUT_SECONDS', 60)),
            priority_fee_cu_limit=int(config.get('BUY_COMPUTE_UNIT_LIMIT', 300_000)),
            priority_fee_cu_price=config.get('BUY_COMPUTE_UNIT_PRICE')  # Can be None
        )
        self.seller = TokenSeller(
            client=self.client, wallet=self.wallet, curve_manager=self.curve_manager,
            fee_manager=self.priority_fee_manager, slippage_bps=sell_slippage_bps,
            max_retries=int(config.get('MAX_SELL_RETRIES', 2)),
            confirm_timeout_seconds=int(config.get('CONFIRM_TIMEOUT_SECONDS', 60)),
            priority_fee_cu_limit=int(config.get('SELL_COMPUTE_UNIT_LIMIT', 200_000)),
            priority_fee_cu_price=config.get('SELL_COMPUTE_UNIT_PRICE')
        )
        self.raydium_fetcher = RaydiumAMMInfoFetcher()
        self.raydium_swap_wrapper = RaydiumSwapWrapper(self.client)
        self.raydium_seller = RaydiumSeller(
            client=self.client, wallet=self.wallet, swap_wrapper=self.raydium_swap_wrapper,
            fee_manager=self.priority_fee_manager,
            sell_slippage_bps=raydium_sell_slippage_bps,
            max_retries=int(config.get('MAX_RAYDIUM_SELL_RETRIES', 3)),
            confirm_timeout_seconds=int(config.get('CONFIRM_TIMEOUT_SECONDS', 90)),
            priority_fee_cu_limit=int(config.get('RAYDIUM_SELL_COMPUTE_UNIT_LIMIT', 600_000)),
            priority_fee_cu_price=config.get('RAYDIUM_SELL_COMPUTE_UNIT_PRICE')
        )

        # --- State Variables ---
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        self.processed_mints_session: Set[Pubkey] = set()
        self.buy_results: Dict[str, TradeResult] = {}
        self.active_token_states: Dict[str, TokenState] = {}
        self.token_peak_values_sol: Dict[str, float] = {}
        self.token_peak_liquidity_lamports: Dict[str, int] = {}
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.session_mints: Set[Pubkey] = set()
        self._shutdown_event = asyncio.Event()

        # --- Concurrency & Mode Flags ---
        self.yolo_mode: bool = False
        self.marry_mode: bool = False
        self.run_once_mode: bool = True

        self.listener: Optional[BaseListener] = None
        self.listener_task: Optional[asyncio.Task] = None
        self.processor_task: Optional[asyncio.Task] = None

        logger.info("PumpTrader (v3) initialized successfully.")

    # --- CORRECTED start method signature ---
    async def start(self,
                    listener_type: str,  # <<< ADDED
                    wss_endpoint: str,  # <<< ADDED
                    match_string: Optional[str],
                    creator_address: Optional[str],
                    yolo_mode: bool,
                    marry_mode: bool):
        """Starts the trader's listener and processing loops."""
        # --- Store or use arguments ---
        self.yolo_mode = yolo_mode
        self.marry_mode = marry_mode
        self.run_once_mode = not (self.yolo_mode or self.marry_mode)

        logger.info(
            f"Starting PumpTrader (v3). YOLO: {self.yolo_mode}, Marry: {self.marry_mode}, RunOnce: {self.run_once_mode}. "
            f"Listener: {listener_type}. Filters: Match='{match_string}', Creator='{creator_address}'"
        )

        try:
            # --- Use arguments to create listener ---
            logs_processor_client = self.client if listener_type == "logs" else None
            self.listener = ListenerFactory.create_listener(
                listener_type=listener_type,  # Use arg
                client=self.client,
                wss_endpoint=wss_endpoint,  # Use arg
                logs_event_processor_client=logs_processor_client
            )
            # --- End Listener Creation Update ---

            self.listener_task = asyncio.create_task(
                self.listener.listen_for_tokens(
                    token_callback=self._enqueue_token,
                    match_string=match_string,
                    creator_address_filter=creator_address
                ),
                name="TokenListener"
            )
            logger.info(f"Token Listener ({listener_type}) task started.")

            self.processor_task = asyncio.create_task(self._process_token_queue(), name="TokenProcessor")
            logger.info("Token Processor task started.")

            await self._shutdown_event.wait()  # Keep running until stop() is called

        except asyncio.CancelledError:
            logger.info("Trader start task was cancelled.")
        except Exception as e:
            logger.critical(f"CRITICAL ERROR during trader startup/runtime: {e}", exc_info=True)
            self._shutdown_event.set()  # Trigger shutdown on critical startup error
        finally:
            logger.info("Trader start task finishing. Initiating shutdown sequence...")
            await self.stop()
            logger.info("Trader (v3) fully stopped.")

    async def _enqueue_token(self, token_info: TokenInfo):
        """Callback to add a newly discovered token to the processing queue."""
        mint_str = token_info.mint_str
        if token_info.mint in self.processed_mints_session:
            return

        now = time.time()
        age_seconds = (
                    now - token_info.creation_timestamp) if token_info.creation_timestamp else self.max_token_age_seconds + 1

        if age_seconds > self.max_token_age_seconds:
            logger.debug(
                f"Token {token_info.symbol} too old ({age_seconds:.1f}s > {self.max_token_age_seconds:.1f}s). Skipping.")
            self.processed_mints_session.add(token_info.mint)
            return

        await self.token_queue.put(token_info)
        self.processed_mints_session.add(token_info.mint)
        await self._update_token_state(mint_str, TokenState.NEW)
        logger.debug(f"Queued: {token_info.symbol} ({mint_str}), Age: {age_seconds:.1f}s")

    async def _process_token_queue(self):
        """Processes tokens from the queue based on operational mode."""
        logger.info("Token processor loop started.")
        try:
            while not self._shutdown_event.is_set():
                try:
                    token_info = await asyncio.wait_for(self.token_queue.get(), timeout=1.0)
                    mint_str = token_info.mint_str
                except asyncio.TimeoutError:
                    continue

                if self._shutdown_event.is_set(): break
                if mint_str in self.active_tasks:
                    logger.warning(
                        f"Token {token_info.symbol} is already associated with an active task. Skipping queue item.")
                    continue

                logger.debug(f"Processing token from queue: {token_info.symbol} ({mint_str})")  # Changed to debug

                handle_task = asyncio.create_task(self._handle_new_token(token_info), name=f"Handle_{mint_str[:8]}")
                self.active_tasks[mint_str] = handle_task

                if self.yolo_mode:
                    pass  # Task launched, continue loop
                elif self.marry_mode:
                    await handle_task  # Wait for this one to finish/fail/transition
                    if self._shutdown_event.is_set(): break
                else:  # Run once mode (default)
                    await handle_task
                    logger.info("Run-once mode: Finished processing one token. Initiating shutdown.")
                    self._shutdown_event.set()
                    break

                    # Optional: Clean up completed tasks map periodically in YOLO mode
                # if self.yolo_mode and len(self.active_tasks) > 50: 
                #     self.active_tasks = { m:t for m, t in self.active_tasks.items() if not t.done() }

        except asyncio.CancelledError:
            logger.info("Token processor task cancelled.")
        except Exception as e:
            logger.error(f"CRITICAL Exception in token processor loop: {e}", exc_info=True)
        finally:
            logger.info("Token processor loop stopped.")

    async def _handle_new_token(self, token_info: TokenInfo):
        """Handles pre-buy checks, buying, and initiates monitoring or cleanup on failure."""
        mint_str = token_info.mint_str
        mint_pk = token_info.mint
        self.session_mints.add(mint_pk)

        try:
            await self._update_token_state(mint_str, TokenState.PRE_BUY_CHECK)
            logger.info(f"Handling new token: {token_info.symbol} ({mint_str})")

            await asyncio.sleep(self.wait_time_after_creation_seconds)

            if self.rug_check_creator_enabled:
                if not await self._check_creator_holding(token_info):
                    await self._update_token_state(mint_str, TokenState.RUGGED, "Creator holding too high")
                    return

            await self._update_token_state(mint_str, TokenState.BUYING)
            logger.info(f"Attempting to buy {token_info.symbol}...")
            buy_result = await self.buyer.execute(token_info)

            await self.audit_logger.log_trade_event("BUY_ATTEMPT", token_info, buy_result)

            if not buy_result.success or not buy_result.signature:
                logger.error(f"Buy failed for {token_info.symbol}: {buy_result.error}")
                final_state = TokenState.BUY_FAILED
                await handle_cleanup_after_failure(
                    client=self.client, wallet=self.wallet, mint=mint_pk,
                    priority_fee_manager=self.priority_fee_manager,
                    cleanup_mode_setting=self.cleanup_mode,
                    use_priority_fee_setting=self.cleanup_use_priority_fee
                )
                await self._update_token_state(mint_str, final_state, buy_result.error)
                return

                # --- Buy Successful Logic ---
            self.buy_results[mint_str] = buy_result
            self.token_peak_values_sol[mint_str] = self.buy_amount_sol
            self.token_peak_liquidity_lamports[mint_str] = buy_result.initial_sol_liquidity or 0
            acquired_tokens_ui = (buy_result.acquired_tokens or 0) / (10 ** token_info.decimals)
            logger.info(
                f"Buy successful for {token_info.symbol}! Tx: {buy_result.signature}, Acquired: ~{acquired_tokens_ui:.4f} tokens.")
            await self._update_token_state(mint_str, TokenState.ON_BONDING_CURVE)

            monitor_task = asyncio.create_task(self._monitor_bonding_curve_token(token_info),
                                               name=f"MonitorCurve_{mint_str[:8]}")
            self.active_tasks[mint_str] = monitor_task

        except asyncio.CancelledError:
            logger.info(f"Handling of token {token_info.symbol} was cancelled.")
            await self._update_token_state(mint_str, TokenState.MONITORING_ERROR, "Task cancelled")
        except Exception as e:
            logger.error(f"Unhandled exception while handling token {token_info.symbol}: {e}", exc_info=True)
            await self._update_token_state(mint_str, TokenState.MONITORING_ERROR, f"Handle error: {e}")
            # Call cleanup on generic error too
            await handle_cleanup_after_failure(
                client=self.client, wallet=self.wallet, mint=mint_pk,
                priority_fee_manager=self.priority_fee_manager,
                cleanup_mode_setting=self.cleanup_mode, use_priority_fee_setting=self.cleanup_use_priority_fee
            )
        finally:
            current_state = self.active_token_states.get(mint_str)
            ongoing_states = {TokenState.ON_BONDING_CURVE, TokenState.ON_RAYDIUM, TokenState.SELLING_ON_CURVE,
                              TokenState.SELLING_ON_RAYDIUM}
            if current_state not in ongoing_states:
                if mint_str in self.active_tasks and self.active_tasks[mint_str].done():
                    del self.active_tasks[mint_str]
                    logger.debug(
                        f"Removed task reference for {mint_str} from active_tasks due to completion/early failure in _handle_new_token.")

    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        # ... (Implementation from previous response) ...
        logger.info(f"Checking creator ({token_info.creator}) holding for {token_info.symbol}...")
        try:
            mint_supply_resp = await self.client.get_token_supply(token_info.mint)
            if not mint_supply_resp or mint_supply_resp.value is None: logger.warning(
                f"Could not get supply for {token_info.mint_str}. Skipping check."); return True
            total_supply_lamports = int(mint_supply_resp.value.amount)
            if total_supply_lamports == 0: logger.warning(
                f"Total supply for {token_info.mint_str} is 0. Skipping check."); return True
            creator_ata = InstructionBuilder.get_associated_token_address(token_info.creator, token_info.mint)
            creator_balance_resp = await self.client.get_token_account_balance(creator_ata, commitment=Processed)
            creator_tokens_lamports = int(
                creator_balance_resp.value.amount) if creator_balance_resp and creator_balance_resp.value else 0
            creator_hold_pct = (creator_tokens_lamports / total_supply_lamports) if total_supply_lamports > 0 else 0
            logger.info(
                f"Creator check {token_info.symbol}: Creator holds {creator_hold_pct * 100:.2f}% ({creator_tokens_lamports}/{total_supply_lamports})")
            if creator_hold_pct > self.rug_max_creator_hold_pct: logger.warning(
                f"RUG_CHECK FAIL (Creator Holding): {token_info.symbol} holding {creator_hold_pct * 100:.2f}% > limit {self.rug_max_creator_hold_pct * 100:.2f}%."); return False
            logger.info(f"Creator check PASSED for {token_info.symbol}.");
            return True
        except Exception as e:
            logger.error(f"Error checking creator holding for {token_info.symbol}: {e}", exc_info=False); return True

    async def _monitor_bonding_curve_token(self, token_info: TokenInfo):
        """Monitors on bonding curve, checks for Raydium transition or sell conditions."""
        mint_str = token_info.mint_str
        buy_result = self.buy_results.get(mint_str)
        # Need buy_result to know how many tokens we hold
        if not buy_result or buy_result.acquired_tokens is None:  # Check acquired_tokens specifically
            logger.error(f"CurveMonitor cannot start for {token_info.symbol}: Missing buy result or acquired tokens.")
            await self._update_token_state(mint_str, TokenState.MONITORING_ERROR,
                                           "Missing buy result/tokens for monitor")
            return

        # Use the actual SOL amount spent for buy value calculation
        initial_buy_value_sol = (buy_result.spent_lamports or 0) / LAMPORTS_PER_SOL
        user_tokens_held_lamports = buy_result.acquired_tokens  # Already checked not None

        logger.info(
            f"Monitoring {token_info.symbol} ({mint_str}) on Bonding Curve. Holding: {user_tokens_held_lamports / (10 ** token_info.decimals):.4f} tokens. Initial value: {initial_buy_value_sol:.6f} SOL.")

        try:
            while not self._shutdown_event.is_set() and self.active_token_states.get(
                    mint_str) == TokenState.ON_BONDING_CURVE:
                await asyncio.sleep(self.monitoring_interval_seconds)

                curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve_address)
                if not curve_state:
                    logger.warning(
                        f"CurveMonitor: Could not get curve state for {token_info.symbol}. Retrying next cycle.")
                    continue

                # --- Check for Raydium Transition ---
                # Use REAL SOL reserves for checking the threshold
                if self.raydium_enabled and curve_state.real_sol_reserves >= self.sol_threshold_for_raydium_lamports:
                    logger.info(
                        f"RAYDIUM_TRANSITION Triggered: {token_info.symbol}. Curve Real SOL {curve_state.real_sol_reserves / LAMPORTS_PER_SOL:.2f} >= Threshold {self.sol_threshold_for_raydium_lamports / LAMPORTS_PER_SOL:.2f}")
                    await self._update_token_state(mint_str, TokenState.ON_RAYDIUM)  # Update state first

                    logger.info(f"Starting Raydium monitoring task for {token_info.symbol}...")
                    raydium_monitor_task = asyncio.create_task(self._monitor_raydium_token(token_info),
                                                               name=f"MonitorRaydium_{mint_str[:8]}")
                    self.active_tasks[mint_str] = raydium_monitor_task  # Replace curve monitor task reference
                    return  # Exit bonding curve monitoring gracefully

                # --- Continue with Curve Monitoring if no transition ---
                if curve_state.is_complete:
                    logger.info(
                        f"CurveMonitor: Bonding curve for {token_info.symbol} completed. Attempting final sell.")
                    await self._sell_on_curve(token_info, user_tokens_held_lamports, "Curve Completed")
                    return  # Exit monitoring

                # --- Calculations, Peak Tracking, Rug Checks, TP/SL ---
                current_sell_value_lamports = curve_state.estimate_sol_out_for_tokens(user_tokens_held_lamports)
                current_sell_value_sol = current_sell_value_lamports / LAMPORTS_PER_SOL
                current_curve_liquidity_lamports = curve_state.virtual_sol_reserves

                peak_value = self.token_peak_values_sol.get(mint_str, initial_buy_value_sol)
                if current_sell_value_sol > peak_value: self.token_peak_values_sol[
                    mint_str] = current_sell_value_sol; peak_value = current_sell_value_sol
                peak_liquidity = self.token_peak_liquidity_lamports.get(mint_str, 0)
                if current_curve_liquidity_lamports > peak_liquidity: self.token_peak_liquidity_lamports[
                    mint_str] = current_curve_liquidity_lamports; peak_liquidity = current_curve_liquidity_lamports

                sell_reason: Optional[str] = None
                # Rug checks first
                if self.rug_check_price_drop_enabled and peak_value > 0 and current_sell_value_sol < peak_value * (
                        1 - self.rug_price_drop_pct_from_peak):
                    sell_reason = f"Rug: Price Drop >{self.rug_price_drop_pct_from_peak * 100:.0f}%"
                elif self.rug_check_liquidity_drop_enabled and peak_liquidity > 0 and current_curve_liquidity_lamports < peak_liquidity * (
                        1 - self.rug_liquidity_drop_pct_from_peak):
                    sell_reason = f"Rug: Liquidity Drop >{self.rug_liquidity_drop_pct_from_peak * 100:.0f}%"
                # Then TP/SL if not marry mode and no rug triggered
                elif not self.marry_mode:
                    if current_sell_value_sol >= initial_buy_value_sol * self.take_profit_multiplier:
                        sell_reason = f"Take Profit >={self.take_profit_multiplier:.1f}x"
                    elif current_sell_value_sol <= initial_buy_value_sol * self.stop_loss_value_multiplier:
                        sell_reason = f"Stop Loss <={self.stop_loss_value_multiplier * 100:.0f}%"

                if sell_reason:
                    logger.info(f"Sell condition met on curve for {token_info.symbol}: {sell_reason}")
                    await self._sell_on_curve(token_info, user_tokens_held_lamports, sell_reason)
                    return  # Exit monitoring loop

                elif self.marry_mode and int(time.time()) % 60 == 0:
                    logger.info(
                        f"MarryMode {token_info.symbol} (Curve): Value={current_sell_value_sol:.6f} SOL. Holding.")

        except asyncio.CancelledError:
            logger.info(f"Curve monitoring for {token_info.symbol} cancelled.")
            await self._update_token_state(mint_str, TokenState.MONITORING_ERROR, "Curve monitor cancelled")
        except Exception as e:
            logger.error(f"Error monitoring {token_info.symbol} on curve: {e}", exc_info=True)
            await self._update_token_state(mint_str, TokenState.MONITORING_ERROR, f"Curve monitor error: {e}")
        finally:
            logger.debug(f"Exiting _monitor_bonding_curve_token for {mint_str}")
            # Cleanup of state dicts (peak values etc) happens here OR in the calling function/sell function
            # Let's keep cleanup tied to the final state transition (sell/error)
            pass

    async def _monitor_raydium_token(self, token_info: TokenInfo):
        """Monitors a token on Raydium for sell conditions."""
        mint_str = token_info.mint_str
        buy_result = self.buy_results.get(mint_str)
        if not buy_result or buy_result.acquired_tokens is None:
            logger.error(f"RaydiumMonitor cannot start for {token_info.symbol}: Missing buy result/tokens.")
            await self._update_token_state(mint_str, TokenState.MONITORING_ERROR,
                                           "Missing buy result for Raydium monitor")
            return

        initial_buy_value_sol = (buy_result.spent_lamports or 0) / LAMPORTS_PER_SOL
        user_tokens_held_lamports = buy_result.acquired_tokens

        logger.info(
            f"Monitoring {token_info.symbol} ({mint_str}) on Raydium. Holding: {user_tokens_held_lamports / (10 ** token_info.decimals):.4f}. Initial Buy Value: {initial_buy_value_sol:.6f} SOL.")

        amm_info: Optional[RaydiumPoolInfo] = None
        fetch_attempts = 0

        try:
            while not self._shutdown_event.is_set() and self.active_token_states.get(mint_str) == TokenState.ON_RAYDIUM:

                # 1. Find Raydium Pool Info (retry logic)
                if not amm_info:
                    if fetch_attempts >= self.raydium_pool_fetch_retries:
                        logger.error(
                            f"RaydiumMonitor: Failed to find pool for {token_info.symbol} after {fetch_attempts} attempts. Stopping monitor.")
                        await self._update_token_state(mint_str, TokenState.MONITORING_ERROR, "Raydium pool not found")
                        return  # Exit monitoring

                    logger.info(
                        f"RaydiumMonitor: Searching for pool for {token_info.symbol} (Attempt {fetch_attempts + 1})...")
                    pool_info_list = await self.raydium_fetcher.get_pool_info_for_token(token_info.mint_str)
                    fetch_attempts += 1

                    if pool_info_list:
                        amm_info = pool_info_list[0]  # Assume first is best
                        logger.info(f"RaydiumMonitor: Found pool for {token_info.symbol}. AMM ID: {amm_info.id}")
                    else:
                        logger.info(
                            f"RaydiumMonitor: Pool not found for {token_info.symbol}. Waiting {self.raydium_pool_fetch_delay}s...")
                        await asyncio.sleep(self.raydium_pool_fetch_delay)
                        continue  # Try finding pool again in next cycle

                # 2. Get Current Price / Value on Raydium
                try:
                    base_vault_pk = Pubkey.from_string(amm_info.base_vault)
                    quote_vault_pk = Pubkey.from_string(amm_info.quote_vault)
                    base_vault_balance_resp = await self.client.get_token_account_balance(base_vault_pk,
                                                                                          commitment=Processed)
                    quote_vault_balance_resp = await self.client.get_token_account_balance(quote_vault_pk,
                                                                                           commitment=Processed)

                    if not (
                            base_vault_balance_resp and base_vault_balance_resp.value and quote_vault_balance_resp and quote_vault_balance_resp.value):
                        logger.warning(
                            f"RaydiumMonitor: Failed fetch vault balances for pool {amm_info.id}. Skip cycle.")
                        await asyncio.sleep(self.monitoring_interval_seconds * 2)
                        continue

                    base_reserves = int(base_vault_balance_resp.value.amount)
                    quote_reserves = int(quote_vault_balance_resp.value.amount)

                    # Calculate value of held tokens if sold to WSOL
                    current_sell_value_sol_lamports = 0
                    fee_num = 997;
                    fee_den = 1000  # Approx 0.25% fee
                    if amm_info.base_mint == token_info.mint_str:  # Selling base(token) for quote(WSOL)
                        num = quote_reserves * user_tokens_held_lamports * fee_num
                        den = (base_reserves * fee_den) + (user_tokens_held_lamports * fee_num)
                        if den != 0: current_sell_value_sol_lamports = num // den
                    elif amm_info.quote_mint == token_info.mint_str:  # Selling quote(token) for base(WSOL)
                        num = base_reserves * user_tokens_held_lamports * fee_num
                        den = (quote_reserves * fee_den) + (user_tokens_held_lamports * fee_num)
                        if den != 0: current_sell_value_sol_lamports = num // den

                    current_sell_value_sol = current_sell_value_sol_lamports / LAMPORTS_PER_SOL

                except Exception as e_price:
                    logger.warning(f"RaydiumMonitor: Error calculating price for {token_info.symbol}: {e_price}")
                    current_sell_value_sol = 0

                    # 3. Update Peak Value
                peak_value = self.token_peak_values_sol.get(mint_str, initial_buy_value_sol)
                if current_sell_value_sol > peak_value: self.token_peak_values_sol[
                    mint_str] = current_sell_value_sol; peak_value = current_sell_value_sol

                # 4. Check Sell Conditions (TP/SL)
                sell_reason: Optional[str] = None
                if current_sell_value_sol >= initial_buy_value_sol * self.take_profit_multiplier:
                    sell_reason = f"Take Profit >={self.take_profit_multiplier:.1f}x (Raydium)"
                elif current_sell_value_sol <= initial_buy_value_sol * self.stop_loss_value_multiplier:
                    sell_reason = f"Stop Loss <={self.stop_loss_value_multiplier * 100:.0f}% (Raydium)"

                # 5. Execute Sell if reason found
                if sell_reason:
                    # Fetch current token balance again before selling for accuracy
                    user_ata_pubkey = InstructionBuilder.get_associated_token_address(self.wallet.pubkey,
                                                                                      token_info.mint)
                    current_token_balance_resp = await self.client.get_token_account_balance(user_ata_pubkey,
                                                                                             commitment=Confirmed)
                    actual_tokens_to_sell = int(
                        current_token_balance_resp.value.amount) if current_token_balance_resp and current_token_balance_resp.value else 0

                    if actual_tokens_to_sell > 0:
                        logger.info(
                            f"Condition met to sell {actual_tokens_to_sell} {token_info.symbol} on Raydium: {sell_reason}")
                        await self._sell_on_raydium(token_info, amm_info, actual_tokens_to_sell, sell_reason)
                    else:
                        logger.warning(
                            f"Sell triggered for {token_info.symbol} on Raydium, but balance is zero or could not be fetched.")
                        await self._update_token_state(mint_str, TokenState.SOLD_ON_RAYDIUM,
                                                       "Balance zero at Raydium sell trigger")

                    return  # Exit monitoring loop after sell attempt

                await asyncio.sleep(self.monitoring_interval_seconds)

        except asyncio.CancelledError:
            logger.info(f"Raydium monitoring for {token_info.symbol} cancelled.")
            await self._update_token_state(mint_str, TokenState.MONITORING_ERROR, "Raydium monitor cancelled")
        except Exception as e:
            logger.error(f"Error monitoring {token_info.symbol} on Raydium: {e}", exc_info=True)
            await self._update_token_state(mint_str, TokenState.MONITORING_ERROR, f"Raydium monitor error: {e}")
        finally:
            logger.info(f"Stopped monitoring {token_info.symbol} on Raydium.")
            # Clean up state associated with this monitored token
            self.buy_results.pop(mint_str, None)
            self.token_peak_values_sol.pop(mint_str, None)
            self.token_peak_liquidity_lamports.pop(mint_str, None)
            if mint_str in self.active_tasks and self.active_tasks[mint_str].done():
                del self.active_tasks[mint_str]

    async def _sell_on_curve(self, token_info: TokenInfo, amount_lamports: int, reason: str):
        """Initiates the sell process on the bonding curve and handles cleanup."""
        mint_str = token_info.mint_str
        mint_pk = token_info.mint
        logger.info(
            f"Initiating sell ({reason}) for {amount_lamports / (10 ** token_info.decimals):.4f} {token_info.symbol} on curve.")
        await self._update_token_state(mint_str, TokenState.SELLING_ON_CURVE, reason)

        sell_result = await self.seller.execute(token_info, amount_lamports)
        initial_buy_result = self.buy_results.get(mint_str)
        await self.audit_logger.log_trade_event(f"SELL_CURVE_{reason.upper().replace(' ', '_')}", token_info,
                                                sell_result, buy_trade_result=initial_buy_result)

        if sell_result.success and sell_result.signature:
            acquired_sol_str = f"{sell_result.acquired_sol / LAMPORTS_PER_SOL:.6f}" if sell_result.acquired_sol is not None else 'N/A'
            logger.info(
                f"Sell successful for {token_info.symbol}. Tx: {sell_result.signature}. Acquired SOL: ~{acquired_sol_str}")
            await self._update_token_state(mint_str, TokenState.SOLD_ON_CURVE, reason)
            await handle_cleanup_after_sell(  # Call cleanup handler
                client=self.client, wallet=self.wallet, mint=mint_pk,
                priority_fee_manager=self.priority_fee_manager,
                cleanup_mode_setting=self.cleanup_mode,
                use_priority_fee_setting=self.cleanup_use_priority_fee
            )
        else:
            logger.error(f"Sell failed for {token_info.symbol} on curve: {sell_result.error}")
            await self._update_token_state(mint_str, TokenState.SELL_FAILED_ON_CURVE, sell_result.error)

        # Task should end, remove from active_tasks
        if mint_str in self.active_tasks: del self.active_tasks[mint_str]

    async def _sell_on_raydium(self, token_info: TokenInfo, amm_info: RaydiumPoolInfo, amount_lamports: int,
                               reason: str):
        """Initiates the sell process on Raydium and handles cleanup."""
        mint_str = token_info.mint_str
        mint_pk = token_info.mint
        logger.info(
            f"Initiating Raydium sell ({reason}) for {amount_lamports / (10 ** token_info.decimals):.4f} {token_info.symbol}.")
        await self._update_token_state(mint_str, TokenState.SELLING_ON_RAYDIUM, reason)

        sell_result = await self.raydium_seller.execute(token_info, amm_info, amount_lamports)
        initial_buy_result = self.buy_results.get(mint_str)
        await self.audit_logger.log_trade_event(f"SELL_RAYDIUM_{reason.upper().replace(' ', '_')}", token_info,
                                                sell_result, buy_trade_result=initial_buy_result)

        if sell_result.success and sell_result.signature:
            acquired_sol_str = f"{sell_result.acquired_sol / LAMPORTS_PER_SOL:.6f}" if sell_result.acquired_sol is not None else 'N/A'
            logger.info(
                f"Raydium sell successful for {token_info.symbol}. Tx: {sell_result.signature}. Acquired SOL: ~{acquired_sol_str}")
            await self._update_token_state(mint_str, TokenState.SOLD_ON_RAYDIUM, reason)
            await handle_cleanup_after_sell(  # Call cleanup handler
                client=self.client, wallet=self.wallet, mint=mint_pk,
                priority_fee_manager=self.priority_fee_manager,
                cleanup_mode_setting=self.cleanup_mode,
                use_priority_fee_setting=self.cleanup_use_priority_fee
            )
        else:
            logger.error(f"Raydium sell failed for {token_info.symbol}: {sell_result.error}")
            await self._update_token_state(mint_str, TokenState.SELL_FAILED_ON_RAYDIUM, sell_result.error)

        # Task should end, remove from active_tasks
        if mint_str in self.active_tasks: del self.active_tasks[mint_str]

    async def _update_token_state(self, mint_str: str, state: TokenState, reason: Optional[str] = None):
        """Updates the in-memory state for a token."""
        self.active_token_states[mint_str] = state
        log_msg = f"StateChange: {mint_str[:8]} -> {state.value}"
        # if reason: log_msg += f" (Reason: {reason})" # Keep reason internal if too verbose
        logger.info(log_msg)

    async def stop(self):
        """Gracefully stops the trader and all its tasks, performs post-session cleanup."""
        if self._shutdown_event.is_set(): return
        logger.info("Initiating trader shutdown...")
        self._shutdown_event.set()

        tasks_to_cancel = []
        if self.listener_task and not self.listener_task.done(): tasks_to_cancel.append(
            self.listener_task); self.listener_task.cancel()
        if self.processor_task and not self.processor_task.done(): tasks_to_cancel.append(
            self.processor_task); self.processor_task.cancel()
        active_handle_monitor_tasks = list(self.active_tasks.values())
        for task in active_handle_monitor_tasks:
            if not task.done(): tasks_to_cancel.append(task); task.cancel()

        if tasks_to_cancel:
            logger.info(f"Waiting for {len(tasks_to_cancel)} tasks to cancel...")
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
            logger.info("Active tasks cancellation complete.")

        self.active_tasks.clear()

        if self.listener and hasattr(self.listener, 'stop'):
            logger.info("Stopping listener...")
            await self.listener.stop()

        logger.info("Performing post-session cleanup check...")
        await handle_cleanup_post_session(
            client=self.client, wallet=self.wallet, mints=list(self.session_mints),
            priority_fee_manager=self.priority_fee_manager,
            cleanup_mode_setting=self.cleanup_mode,
            use_priority_fee_setting=self.cleanup_use_priority_fee
        )

        logger.info("Trader shutdown sequence complete.")