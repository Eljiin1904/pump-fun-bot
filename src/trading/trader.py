# src/trading/trader.py

import asyncio
import time
import json
from datetime import datetime, timezone
from typing import Optional, Set, Dict, List, Any

from solders.pubkey import Pubkey

# Core Components
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager
from src.core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL
from src.core.instruction_builder import InstructionBuilder
from src.core.transactions import build_and_send_transaction, get_transaction_fee

# Monitoring
from src.monitoring.listener_factory import ListenerFactory
from src.monitoring.base_listener import BaseTokenListener

# Trading Logic
from src.trading.base import TokenInfo, TradeResult, TokenState
from src.trading.buyer import TokenBuyer
from src.trading.seller import TokenSeller

# Utilities
from src.utils.logger import get_logger
from src.utils.audit_logger import AuditLogger

# Solana specifics
from solana.rpc.commitment import Confirmed

logger = get_logger(__name__)

DEFAULT_MAX_TOKEN_AGE_SECONDS = 45.0
DEFAULT_WAIT_TIME_AFTER_CREATION_SECONDS = 3.0
DEFAULT_MONITOR_INTERVAL_SECONDS = 5.0


class PumpTrader:
    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        config: Dict[str, Any],
    ):
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.config = config

        logger.info(f"Initializing PumpTrader (v2) for wallet: {self.wallet.pubkey}")

        self.curve_manager = BondingCurveManager(self.client)
        self.audit_logger = AuditLogger()

        # Config
        self.buy_amount_sol = float(config.get("BUY_AMOUNT_SOL", 0.01))
        self.max_token_age_seconds = float(
            config.get("MAX_TOKEN_AGE_SECONDS", DEFAULT_MAX_TOKEN_AGE_SECONDS)
        )
        self.wait_time_after_creation_seconds = float(
            config.get("WAIT_TIME_AFTER_CREATION_SECONDS", DEFAULT_WAIT_TIME_AFTER_CREATION_SECONDS)
        )
        self.take_profit_multiplier = float(
            config.get("TAKE_PROFIT_MULTIPLIER", 15.0)
        )
        self.stop_loss_percentage = float(
            config.get("STOP_LOSS_PERCENTAGE", 0.70)
        )
        self.rug_check_creator_enabled = bool(
            config.get("RUG_CHECK_CREATOR_ENABLED", True)
        )
        self.rug_max_creator_hold_pct = float(
            config.get("RUG_MAX_CREATOR_HOLD_PCT", 20.0)
        ) / 100.0
        self.rug_check_price_drop_enabled = bool(
            config.get("RUG_CHECK_PRICE_DROP_ENABLED", True)
        )
        self.rug_price_drop_pct = float(
            config.get("RUG_PRICE_DROP_PCT", 40.0)
        ) / 100.0
        self.rug_check_liquidity_drop_enabled = bool(
            config.get("RUG_CHECK_LIQUIDITY_DROP_ENABLED", True)
        )
        self.rug_liquidity_drop_pct = float(
            config.get("RUG_LIQUIDITY_DROP_PCT", 50.0)
        ) / 100.0
        self.cleanup_atas_on_sell = bool(
            config.get("CLEANUP_ATAS_ON_SELL", True)
        )

        # Buyer & Seller
        self.buyer = TokenBuyer(
            client=self.client,
            wallet=self.wallet,
            curve_manager=self.curve_manager,
            fee_manager=self.priority_fee_manager,
            buy_amount_sol=self.buy_amount_sol,
            slippage_bps=int(config.get("BUY_SLIPPAGE_BPS", 1500)),
            max_retries=int(config.get("MAX_BUY_RETRIES", 3)),
            confirm_timeout_seconds=int(config.get("CONFIRM_TIMEOUT_SECONDS", 60)),
            priority_fee_cu_limit=int(config.get("BUY_COMPUTE_UNIT_LIMIT", 300_000)),
            priority_fee_cu_price=config.get("BUY_COMPUTE_UNIT_PRICE"),
        )
        self.seller = TokenSeller(
            client=self.client,
            wallet=self.wallet,
            curve_manager=self.curve_manager,
            fee_manager=self.priority_fee_manager,
            slippage_bps=int(config.get("SELL_SLIPPAGE_BPS", 2500)),
            max_retries=int(config.get("MAX_SELL_RETRIES", 2)),
            confirm_timeout_seconds=int(config.get("CONFIRM_TIMEOUT_SECONDS", 60)),
            priority_fee_cu_limit=int(config.get("SELL_COMPUTE_UNIT_LIMIT", 200_000)),
            priority_fee_cu_price=config.get("SELL_COMPUTE_UNIT_PRICE"),
        )

        # State
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        self.processed_mints_session: Set[Pubkey] = set()
        self.active_trades: Dict[str, TokenInfo] = {}
        self.token_buy_results: Dict[str, TradeResult] = {}
        self.token_peak_values_sol: Dict[str, float] = {}
        self.token_peak_liquidity_sol: Dict[str, int] = {}

        # Modes & tasks
        self.yolo_mode = False
        self.marry_mode = False
        self.run_once_mode = True

        self.listener: Optional[BaseTokenListener] = None
        self.listener_task: Optional[asyncio.Task] = None
        self.processor_task: Optional[asyncio.Task] = None
        self.monitor_tasks: Dict[str, asyncio.Task] = {}

        self._shutdown_event = asyncio.Event()
        self._processing_token_lock = asyncio.Lock()

        logger.info("PumpTrader (v2) initialized successfully.")

    async def start(
        self,
        listener_type: str,
        wss_endpoint: str,
        match_string: Optional[str],
        creator_address: Optional[str],
        yolo_mode: bool,
        marry_mode: bool,
    ):
        self.yolo_mode = yolo_mode
        self.marry_mode = marry_mode
        if yolo_mode or marry_mode:
            self.run_once_mode = False

        logger.info(
            f"Starting PumpTrader (v2). YOLO={self.yolo_mode}, "
            f"Marry={self.marry_mode}, RunOnce={self.run_once_mode}, "
            f"Listener={listener_type}"
        )

        try:
            self.listener = ListenerFactory.create_listener(
                listener_type=listener_type,
                client=self.client,
                wss_endpoint=wss_endpoint,
                token_queue=self.token_queue,
                create_event_discriminator=self.config.get(
                    "CREATE_EVENT_DISCRIMINATOR", "1b72a94ddeeb6376"
                ),
            )

            self.listener_task = asyncio.create_task(
                self.listener.listen_for_tokens(), name="TokenListener"
            )
            logger.info("Token Listener task started.")

            self.processor_task = asyncio.create_task(
                self._process_token_queue(), name="TokenProcessor"
            )
            logger.info("Token Processor task started.")

            await self._shutdown_event.wait()

        except asyncio.CancelledError:
            logger.info("Trader start task was cancelled.")
        except Exception as e:
            logger.error(f"CRITICAL ERROR during trader startup/runtime: {e}", exc_info=True)
            self._shutdown_event.set()
        finally:
            logger.info("Trader start looping ending. Shutting down...")
            await self.stop()
            logger.info("Trader fully stopped.")

    async def _enqueue_token(self, token_info: TokenInfo):
        if token_info.mint in self.processed_mints_session:
            return
        age = time.time() - (token_info.creation_timestamp or 0)
        if age > self.max_token_age_seconds:
            return
        await self.token_queue.put(token_info)
        logger.info(f"Queued {token_info.symbol} ({token_info.mint_str[:6]}.. Age={age:.1f}s)")
        self.processed_mints_session.add(token_info.mint)

    async def _process_token_queue(self):
        logger.info("Token processor loop started.")
        try:
            while not self._shutdown_event.is_set():
                try:
                    token_info = await asyncio.wait_for(self.token_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                logger.info(f"Dequeued {token_info.symbol} for processing.")
                if self.yolo_mode:
                    task = asyncio.create_task(self._handle_new_token(token_info))
                    self.monitor_tasks[token_info.mint_str] = task
                else:
                    async with self._processing_token_lock:
                        await self._handle_new_token(token_info)
                        if self.run_once_mode:
                            self._shutdown_event.set()
                            break

        except asyncio.CancelledError:
            logger.info("Token processor task cancelled.")
        finally:
            logger.info("Token processor loop stopped.")

    async def _handle_new_token(self, token_info: TokenInfo):
        mint_str = token_info.mint_str
        buy_result: TradeResult = None  # type: ignore
        try:
            await self._update_token_state(token_info, TokenState.PRE_BUY_CHECK, "Starting pre-buy checks")
            await asyncio.sleep(self.wait_time_after_creation_seconds)

            if self.rug_check_creator_enabled:
                ok = await self._check_creator_holding(token_info)
                if not ok:
                    await self._update_token_state(token_info, TokenState.RUGGED, "Creator holding too high")
                    return

            await self._update_token_state(token_info, TokenState.BUYING, f"Buying {self.buy_amount_sol} SOL")
            buy_result = await self.buyer.execute(token_info)
            await self.audit_logger.log_trade_event("BUY", token_info, buy_result)

            if not buy_result.success:
                await self._update_token_state(token_info, TokenState.BUY_FAILED, buy_result.error)
                return

            await self._update_token_state(token_info, TokenState.ON_BONDING_CURVE, "Buy successful")
            self.active_trades[mint_str] = token_info
            self.token_buy_results[mint_str] = buy_result

            # Initialize peaks
            price_per_token = (
                (buy_result.spent_lamports or 0) / (buy_result.acquired_tokens or 1)
            ) / LAMPORTS_PER_SOL
            self.token_peak_values_sol[mint_str] = price_per_token
            self.token_peak_liquidity_sol[mint_str] = buy_result.initial_sol_liquidity or 0

            monitor_task = asyncio.create_task(
                self._monitor_bonding_curve_token(token_info, buy_result),
                name=f"Monitor_{mint_str[:8]}",
            )
            self.monitor_tasks[mint_str] = monitor_task
            if not self.yolo_mode:
                await monitor_task

        except asyncio.CancelledError:
            logger.info(f"Handling of {mint_str} cancelled.")
        except Exception as e:
            logger.error(f"Error handling {mint_str}: {e}", exc_info=True)
            await self._update_token_state(
                token_info,
                TokenState.MONITORING_ERROR if buy_result and buy_result.success else TokenState.BUY_FAILED,
                str(e)[:100],
            )
        finally:
            if not self.yolo_mode:
                # cleanup in-memory
                self.active_trades.pop(mint_str, None)
                self.token_buy_results.pop(mint_str, None)
                self.monitor_tasks.pop(mint_str, None)
                self.token_peak_values_sol.pop(mint_str, None)
                self.token_peak_liquidity_sol.pop(mint_str, None)

    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        supply = await self.client.get_token_supply(token_info.mint)
        if supply is None or supply == 0:
            return False
        resp = await self.client.get_token_largest_accounts(token_info.mint)
        accounts = getattr(resp, "value", []) or []
        creator_balance = 0
        for acct in accounts:
            if str(acct.address) == str(token_info.creator):
                creator_balance = int(acct.amount)
                break
        pct = creator_balance / supply
        return pct <= self.rug_max_creator_hold_pct

    async def _monitor_bonding_curve_token(
        self, token_info: TokenInfo, buy_trade_result: TradeResult
    ):
        mint_str = token_info.mint_str
        buy_price = self.token_peak_values_sol[mint_str]
        while True:
            state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(
                token_info.associated_bonding_curve_address
            )
            if state is None:
                break

            current_price = state.calculate_price(token_info.decimals)
            # update peaks
            if current_price > self.token_peak_values_sol[mint_str]:
                self.token_peak_values_sol[mint_str] = current_price
            if state.virtual_sol_reserves > self.token_peak_liquidity_sol[mint_str]:
                self.token_peak_liquidity_sol[mint_str] = state.virtual_sol_reserves

            # Take profit
            if current_price >= buy_price * self.take_profit_multiplier:
                await self._sell_on_curve(token_info, state.real_token_reserves, "Take Profit", buy_trade_result)
                return

            # Stop loss
            if current_price <= buy_price * self.stop_loss_percentage:
                await self._sell_on_curve(token_info, state.real_token_reserves, "Stop Loss", buy_trade_result)
                return

            # Rug price drop
            if self.rug_check_price_drop_enabled and current_price <= (
                self.token_peak_values_sol[mint_str] * (1 - self.rug_price_drop_pct)
            ):
                await self._sell_on_curve(token_info, state.real_token_reserves, "Rug Price Drop", buy_trade_result)
                return

            # Rug liquidity drop
            if self.rug_check_liquidity_drop_enabled and state.virtual_sol_reserves <= (
                self.token_peak_liquidity_sol[mint_str] * (1 - self.rug_liquidity_drop_pct)
            ):
                await self._sell_on_curve(token_info, state.real_token_reserves, "Rug Liquidity Drop", buy_trade_result)
                return

            # Curve complete
            if state.complete:
                await self._sell_on_curve(token_info, state.real_token_reserves, "Curve Complete", buy_trade_result)
                return

            await asyncio.sleep(self.config.get("MONITOR_INTERVAL_SECONDS", DEFAULT_MONITOR_INTERVAL_SECONDS))

    async def _sell_on_curve(
        self,
        token_info: TokenInfo,
        sell_amount: int,
        reason: str,
        buy_trade_result: TradeResult,
    ):
        await self._update_token_state(token_info, TokenState.SELLING_ON_CURVE, reason)
        sell_result = await self.seller.execute(token_info, amount=sell_amount)
        await self.audit_logger.log_trade_event("SELL", token_info, sell_result)
        if not sell_result.success:
            await self._update_token_state(token_info, TokenState.SELL_FAILED_ON_CURVE, sell_result.error)
            return
        await self._update_token_state(
            token_info,
            TokenState.SOLD_ON_CURVE,
            f"Sold {sell_result.sold_tokens} for {sell_result.acquired_sol} lamports",
        )
        if self.cleanup_atas_on_sell:
            await self._cleanup_token_resources(token_info)

    async def _cleanup_token_resources(self, token_info: TokenInfo):
        # close ATA if zero balance
        ata = token_info.associated_bonding_curve_address
        bal = await self.client.get_token_account_balance_lamports(ata)
        if bal == 0:
            logger.info(f"Cleaning up ATA {ata} for {token_info.symbol}")

    async def _update_token_state(
        self, token_info: TokenInfo, state: TokenState, reason: Optional[str] = None
    ):
        timestamp = datetime.now(timezone.utc).isoformat()
        logger.info(f"[{timestamp}] {token_info.symbol} => {state.value}: {reason or ''}")
        # you could also write to audit log here if desired:
        # await self.audit_logger.log_state_change(token_info, state, reason)

    async def stop(self):
        logger.info("Stopping PumpTrader and all tasks...")
        self._shutdown_event.set()
        if self.listener:
            await self.listener.stop()
        if self.listener_task:
            self.listener_task.cancel()
        if self.processor_task:
            self.processor_task.cancel()
        for t in self.monitor_tasks.values():
            t.cancel()
        await asyncio.sleep(0)  # let cancellations propagate
