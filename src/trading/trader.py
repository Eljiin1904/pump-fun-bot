# src/trading/trader.py

import asyncio
from typing import Optional, Dict, Any

from solders.pubkey import Pubkey

from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager
from src.core.curve import BondingCurveManager, LAMPORTS_PER_SOL
from src.trading.base import TokenInfo, TradeResult
from src.trading.buyer import TokenBuyer
from src.trading.seller import TokenSeller
from src.tools.raydium_amm import RaydiumAMMInfoFetcher
from src.trading.raydium_seller import RaydiumSeller
from src.monitoring.listener_factory import ListenerFactory
from src.utils.logger import get_logger
from src.utils.audit_logger import AuditLogger
from src.cleanup.modes import (
    handle_cleanup_after_sell,
    handle_cleanup_after_failure,
)

logger = get_logger(__name__)


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
        logger.info(f"Initializing PumpTrader for wallet {self.wallet.pubkey}")

        # Core services
        self.curve_manager = BondingCurveManager(self.client)
        self.audit_logger = AuditLogger()

        # TokenBuyer
        self.buyer = TokenBuyer(
            client=self.client,
            wallet=self.wallet,
            curve_manager=self.curve_manager,
            fee_manager=self.priority_fee_manager,
            buy_amount_sol=float(config["BUY_AMOUNT_SOL"]),
            slippage_bps=int(config["BUY_SLIPPAGE_BPS"]),
            max_retries=int(config["MAX_BUY_RETRIES"]),
            confirm_timeout_seconds=int(config["CONFIRM_TIMEOUT_SECONDS"]),
            priority_fee_cu_limit=int(config["BUY_COMPUTE_UNIT_LIMIT"]),
            priority_fee_cu_price=config.get("BUY_COMPUTE_UNIT_PRICE"),
        )

        # TokenSeller
        self.seller = TokenSeller(
            client=self.client,
            wallet=self.wallet,
            curve_manager=self.curve_manager,
            fee_manager=self.priority_fee_manager,
            slippage_bps=int(config["SELL_SLIPPAGE_BPS"]),
            max_retries=int(config["MAX_SELL_RETRIES"]),
            confirm_timeout_seconds=int(config["CONFIRM_TIMEOUT_SECONDS"]),
            priority_fee_cu_limit=int(config["SELL_COMPUTE_UNIT_LIMIT"]),
            priority_fee_cu_price=config.get("SELL_COMPUTE_UNIT_PRICE"),
        )

        # RaydiumSeller
        self.raydium_fetcher = RaydiumAMMInfoFetcher()
        self.raydium_seller = RaydiumSeller(
            client=self.client,
            wallet=self.wallet,
            swap_wrapper=self.raydium_fetcher,
            fee_manager=self.priority_fee_manager,
            sell_slippage_bps=int(config["RAYDIUM_SELL_SLIPPAGE_BPS"]),
            max_retries=int(config["MAX_RAYDIUM_SELL_RETRIES"]),
            confirm_timeout_seconds=int(config["CONFIRM_TIMEOUT_SECONDS"]),
            priority_fee_cu_limit=int(config["RAYDIUM_SELL_COMPUTE_UNIT_LIMIT"]),
            priority_fee_cu_price=config.get("RAYDIUM_SELL_COMPUTE_UNIT_PRICE"),
        )

        # Internal state
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.buy_results: Dict[str, TradeResult] = {}
        self._shutdown_event = asyncio.Event()
        self.yolo_mode = False
        self.marry_mode = False
        self.listener = None
        self.listener_task: Optional[asyncio.Task] = None
        self.processor_task: Optional[asyncio.Task] = None

        logger.info("PumpTrader initialized successfully.")

    async def start(
        self,
        listener_type: str,
        wss_endpoint: str,
        match_string: Optional[str],
        creator_address: Optional[str],
        yolo_mode: bool,
        marry_mode: bool
    ):
        # Apply modes
        self.yolo_mode = yolo_mode
        self.marry_mode = marry_mode
        logger.info(f"Starting PumpTrader (YOLO={yolo_mode}, marry={marry_mode})")

        # Resolve program_id
        pid_cfg = self.config.get("PUMP_FUN_PROGRAM_ID")
        program_id = pid_cfg if isinstance(pid_cfg, Pubkey) else Pubkey.from_string(str(pid_cfg))

        # Normalize discriminator to a hex‐string
        raw_disc = self.config.get("CREATE_EVENT_DISCRIMINATOR")
        discriminator = raw_disc.hex() if isinstance(raw_disc, (bytes, bytearray)) else str(raw_disc)

        # Create & start the LogsListener
        self.listener = ListenerFactory.create_listener(
            listener_type=listener_type,
            client=self.client,
            token_queue=self.token_queue,
            wss_endpoint=wss_endpoint,
            program_id=program_id,
            discriminator=discriminator,
        )
        self.listener_task = asyncio.create_task(
            self.listener.listen_for_tokens(
                token_callback=self._enqueue_token,
                match_string=match_string,
                creator_address=creator_address,
            ),
            name="TokenListener"
        )

        # Immediately start the queue‐processor
        self.processor_task = asyncio.create_task(
            self._process_token_queue(),
            name="TokenProcessor"
        )

        # And wait for a shutdown signal
        await self._shutdown_event.wait()
        await self.stop()

    async def _enqueue_token(self, token_info: TokenInfo):
        key = token_info.mint_str
        if key in self.active_tasks:
            return
        await self.token_queue.put(token_info)

    async def _process_token_queue(self):
        logger.info("Token processor started.")
        try:
            while not self._shutdown_event.is_set():
                try:
                    token_info = await asyncio.wait_for(self.token_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                key = token_info.mint_str
                logger.debug(f"Dequeued {key}, spawning handler…")
                task = asyncio.create_task(self._handle_new_token(token_info), name=f"Handle_{key[:8]}")
                self.active_tasks[key] = task

                if not self.yolo_mode:
                    await task
                    self._shutdown_event.set()
                    break

        except asyncio.CancelledError:
            logger.info("Token processor cancelled.")
        finally:
            logger.info("Token processor stopped.")

    async def _handle_new_token(self, token_info: TokenInfo):
        key = token_info.mint_str
        try:
            result = await self.buyer.execute(token_info)
            if not result.success:
                raise RuntimeError(f"Buy failed: {result.error}")
            self.buy_results[key] = result
            await self._monitor_curve(token_info, result.acquired_tokens)
        except Exception as e:
            logger.error(f"Error handling {key}: {e}")
            await handle_cleanup_after_failure(
                self.client,
                self.wallet,
                token_info.mint,
                self.priority_fee_manager,
                self.config.get("CLEANUP_MODE"),
                self.config.get("CLEANUP_WITH_PRIORITY_FEE")
            )
        finally:
            self.active_tasks.pop(key, None)

    async def _monitor_curve(self, token_info: TokenInfo, amount: int):
        try:
            while True:
                await asyncio.sleep(float(self.config["MONITORING_INTERVAL_SECONDS"]))
                state = await self.curve_manager.get_curve_state(token_info.bonding_curve_address)
                if not state:
                    continue
                if state.is_complete:
                    await self._sell_on_curve(token_info, amount)
                    return
        except asyncio.CancelledError:
            logger.info(f"Curve monitor for {token_info.mint_str} cancelled.")

    async def _sell_on_curve(self, token_info: TokenInfo, amount: int):
        result = await self.seller.execute(token_info, amount)
        if result.success:
            await handle_cleanup_after_sell(
                self.client,
                self.wallet,
                token_info.mint,
                self.priority_fee_manager,
                self.config.get("CLEANUP_MODE"),
                self.config.get("CLEANUP_WITH_PRIORITY_FEE")
            )
            logger.info(f"Sold {token_info.mint_str} on curve.")

    async def stop(self):
        if self.listener:
            await self.listener.stop()

        # Cancel outstanding tasks
        tasks = [t for t in (self.listener_task, self.processor_task) if t and not t.done()]
        for t in tasks:
            t.cancel()
        for t in self.active_tasks.values():
            t.cancel()

        await asyncio.gather(*(tasks + list(self.active_tasks.values())), return_exceptions=True)
        self._shutdown_event.set()
        logger.info("PumpTrader stopped.")
