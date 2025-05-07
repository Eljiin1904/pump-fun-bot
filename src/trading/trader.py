import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from solders.pubkey import Pubkey
from solana.rpc.commitment import Confirmed, Processed

# Core Project Imports
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager
from src.core.curve import BondingCurveManager, BondingCurveState, LAMPORTS_PER_SOL
from src.core.instruction_builder import InstructionBuilder
from src.core.transactions import build_and_send_transaction

# Monitoring Imports
from src.monitoring.listener_factory import BaseListener, ListenerFactory

# Trading Imports
from src.trading.base import TokenInfo, TokenState, TradeResult
from src.trading.buyer import TokenBuyer
from src.trading.seller import TokenSeller
from src.tools.raydium_amm import RaydiumAMMInfoFetcher, RaydiumPoolInfo, RaydiumSwapWrapper
from src.trading.raydium_seller import RaydiumSeller

# Cleanup Handlers
from src.cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)

# Utility Imports
from src.utils.audit_logger import AuditLogger
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Default Values
DEFAULT_MAX_TOKEN_AGE_SECONDS = 45.0
DEFAULT_CLEANUP_MODE = 'after_sell'
DEFAULT_CLEANUP_WITH_PRIORITY_FEE = False


class PumpTrader:
    """
    Orchestrates v3 trading: bonding curve → Raydium transition, monitoring, and selling.
    Supports run_once, marry, and yolo modes and integrates cleanup logic.
    """

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
        logger.info(f"Initializing PumpTrader for wallet: {self.wallet.pubkey}")

        # Core components
        self.curve_manager = BondingCurveManager(self.client)
        self.audit_logger = AuditLogger()

        # Config parameters
        self.buy_amount_sol = float(config.get('BUY_AMOUNT_SOL', 0.01))
        self.wait_time_after_creation_seconds = float(
            config.get('WAIT_AFTER_CREATION', 2.0)
        )
        self.rug_check_creator_enabled = bool(
            config.get('RUG_CHECK_CREATOR_ENABLED', True)
        )
        self.rug_max_creator_hold_pct = float(
            config.get('RUG_MAX_CREATOR_HOLD_PCT', 0.5)
        )

        # Cleanup config
        self.cleanup_mode: str = config.get('CLEANUP_MODE', DEFAULT_CLEANUP_MODE)
        self.cleanup_use_priority_fee: bool = bool(
            config.get('CLEANUP_WITH_PRIORITY_FEE', DEFAULT_CLEANUP_WITH_PRIORITY_FEE)
        )

        # Trading components
        # TODO: Replace `None` with actual initialization parameters
        # self.buyer = TokenBuyer(self.client, self.wallet, ...)
        # self.seller = TokenSeller(self.client, self.wallet, ...)
        self.raydium_fetcher = RaydiumAMMInfoFetcher()
        self.raydium_swap_wrapper = RaydiumSwapWrapper(self.client)
        # self.raydium_seller = RaydiumSeller(self.client, self.wallet, ...)

        # State variables
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        self.processed_mints_session: Set[Pubkey] = set()
        self.buy_results: Dict[str, TradeResult] = {}
        self.active_token_states: Dict[str, TokenState] = {}
        self.token_peak_values_sol: Dict[str, float] = {}
        self.token_peak_liquidity_lamports: Dict[str, int] = {}
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.session_mints: Set[Pubkey] = set()
        self._shutdown_event = asyncio.Event()

        # Mode flags
        self.yolo_mode = False
        self.marry_mode = False
        self.run_once_mode = True

        self.listener: Optional[BaseListener] = None
        self.listener_task: Optional[asyncio.Task] = None
        self.processor_task: Optional[asyncio.Task] = None

        logger.info("PumpTrader initialized successfully.")

    # ----------------------------------------------------------------------
    # Public Interface
    # ----------------------------------------------------------------------

    async def start(self) -> None:
        """
        Start listening for new token events and processing them.
        """
        # TODO: Implement or copy logic from v2
        raise NotImplementedError

    async def stop(self) -> None:
        """
        Gracefully stop all tasks and perform post-session cleanup.
        """
        if self._shutdown_event.is_set():
            return
        logger.info("Initiating trader shutdown...")
        self._shutdown_event.set()

        # Cancel running tasks
        tasks = []
        if self.listener_task and not self.listener_task.done():
            tasks.append(self.listener_task)
            self.listener_task.cancel()
        if self.processor_task and not self.processor_task.done():
            tasks.append(self.processor_task)
            self.processor_task.cancel()
        tasks.extend(
            task for task in self.active_tasks.values() if not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Stop listener
        if self.listener and hasattr(self.listener, 'stop'):
            await self.listener.stop()

        # Post-session cleanup
        logger.info("Performing post-session cleanup...")
        await handle_cleanup_post_session(
            client=self.client,
            wallet=self.wallet,
            mints=list(self.session_mints),
            priority_fee_manager=self.priority_fee_manager,
            cleanup_mode_setting=self.cleanup_mode,
            use_priority_fee_setting=self.cleanup_use_priority_fee,
        )
        logger.info("Trader shutdown complete.")

    # ----------------------------------------------------------------------
    # Internal Handlers
    # ----------------------------------------------------------------------

    async def _handle_new_token(
        self, token_info: TokenInfo
    ) -> None:
        mint_str = token_info.mint_str
        mint_pk = token_info.mint
        self.session_mints.add(mint_pk)

        try:
            await self._update_token_state(mint_str, TokenState.PRE_BUY_CHECK)
            logger.info(f"New token detected: {token_info.symbol} ({mint_str})")

            await asyncio.sleep(self.wait_time_after_creation_seconds)

            if self.rug_check_creator_enabled and not await self._check_creator_holding(token_info):
                await self._update_token_state(
                    mint_str, TokenState.RUGGED, "Creator holding too high"
                )
                return

            await self._update_token_state(mint_str, TokenState.BUYING)
            buy_result = await self.buyer.execute(token_info)
            await self.audit_logger.log_trade_event(
                "BUY_ATTEMPT", token_info, buy_result
            )

            if not buy_result.success or not buy_result.signature:
                logger.error(f"Buy failed: {buy_result.error}")
                await handle_cleanup_after_failure(
                    client=self.client,
                    wallet=self.wallet,
                    mint=mint_pk,
                    priority_fee_manager=self.priority_fee_manager,
                    cleanup_mode_setting=self.cleanup_mode,
                    use_priority_fee_setting=self.cleanup_use_priority_fee,
                )
                await self._update_token_state(
                    mint_str, TokenState.BUY_FAILED, buy_result.error
                )
                return

            # Buy successful
            self.buy_results[mint_str] = buy_result
            self.token_peak_values_sol[mint_str] = self.buy_amount_sol
            self.token_peak_liquidity_lamports[mint_str] = (
                buy_result.initial_sol_liquidity or 0
            )
            await self._update_token_state(
                mint_str, TokenState.ON_BONDING_CURVE
            )
            task = asyncio.create_task(
                self._monitor_bonding_curve_token(token_info),
                name=f"MonitorCurve_{mint_str[:8]}",
            )
            self.active_tasks[mint_str] = task

        except asyncio.CancelledError:
            logger.debug(f"Handle_new_token cancelled for {mint_str}")
        except Exception as e:
            logger.error(f"Error in _handle_new_token: {e}", exc_info=True)
        finally:
            # Cleanup task tracking
            state = self.active_token_states.get(mint_str)
            ongoing = {
                TokenState.ON_BONDING_CURVE,
                TokenState.ON_RAYDIUM,
                TokenState.SELLING_ON_CURVE,
                TokenState.SELLING_ON_RAYDIUM,
            }
            if state not in ongoing and mint_str in self.active_tasks:
                task = self.active_tasks.pop(mint_str)
                if task.done():
                    logger.debug(f"Removed completed task for {mint_str}")

    async def _sell_on_curve(
        self, token_info: TokenInfo, amount_lamports: int, reason: str
    ) -> None:
        mint_str = token_info.mint_str
        mint_pk = token_info.mint
        await self._update_token_state(
            mint_str, TokenState.SELLING_ON_CURVE, reason
        )
        sell_result = await self.seller.execute(token_info, amount_lamports)
        initial_buy = self.buy_results.get(mint_str)
        await self.audit_logger.log_trade_event(
            f"SELL_CURVE_{reason.upper().replace(' ', '_')}",
            token_info,
            sell_result,
            buy_trade_result=initial_buy,
        )

        if sell_result.success and sell_result.signature:
            await self._update_token_state(
                mint_str, TokenState.SOLD_ON_CURVE, reason
            )
            await handle_cleanup_after_sell(
                client=self.client,
                wallet=self.wallet,
                mint=mint_pk,
                priority_fee_manager=self.priority_fee_manager,
                cleanup_mode_setting=self.cleanup_mode,
                use_priority_fee_setting=self.cleanup_use_priority_fee,
            )
        else:
            await self._update_token_state(
                mint_str, TokenState.SELL_FAILED_ON_CURVE, sell_result.error
            )
        self.active_tasks.pop(mint_str, None)

    async def _sell_on_raydium(
        self,
        token_info: TokenInfo,
        amm_info: RaydiumPoolInfo,
        amount_lamports: int,
        reason: str,
    ) -> None:
        mint_str = token_info.mint_str
        mint_pk = token_info.mint
        await self._update_token_state(
            mint_str, TokenState.SELLING_ON_RAYDIUM, reason
        )
        sell_result = await self.raydium_seller.execute(
            token_info, amm_info, amount_lamports
        )
        initial_buy = self.buy_results.get(mint_str)
        await self.audit_logger.log_trade_event(
            f"SELL_RAYDIUM_{reason.upper().replace(' ', '_')}",
            token_info,
            sell_result,
            buy_trade_result=initial_buy,
        )

        if sell_result.success and sell_result.signature:
            await self._update_token_state(
                mint_str, TokenState.SOLD_ON_RAYDIUM, reason
            )
            await handle_cleanup_after_sell(
                client=self.client,
                wallet=self.wallet,
                mint=mint_pk,
                priority_fee_manager=self.priority_fee_manager,
                cleanup_mode_setting=self.cleanup_mode,
                use_priority_fee_setting=self.cleanup_use_priority_fee,
            )
        else:
            await self._update_token_state(
                mint_str, TokenState.SELL_FAILED_ON_RAYDIUM, sell_result.error
            )
        self.active_tasks.pop(mint_str, None)

    async def _update_token_state(
        self, mint_str: str, state: TokenState, reason: Optional[str] = None
    ) -> None:
        """Updates the in-memory state for a token and logs the change."""
        self.active_token_states[mint_str] = state
        msg = f"StateChange: {mint_str[:8]} -> {state.value}"
        if reason:
            msg += f" (Reason: {reason})"
        logger.info(msg)

    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        """Verifies the creator's token holding percentage against a threshold."""
        logger.info(
            f"Checking creator ({token_info.creator}) holding for {token_info.symbol}..."
        )
        try:
            supply_resp = await self.client.get_token_supply(token_info.mint)
            total = int(supply_resp.value.amount) if supply_resp.value else 0
            if total == 0:
                return True

            ata = InstructionBuilder.get_associated_token_address(
                token_info.creator, token_info.mint
            )
            balance_resp = await self.client.get_token_account_balance(
                ata, commitment=Processed
            )
            creator_amt = (
                int(balance_resp.value.amount) if balance_resp.value else 0
            )
            pct = creator_amt / total
            if pct > self.rug_max_creator_hold_pct:
                logger.warning(
                    f"RUG_CHECK FAIL: {pct*100:.2f}% > {self.rug_max_creator_hold_pct*100:.2f}%"
                )
                return False
            logger.info("Creator check PASSED.")
            return True
        except Exception as e:
            logger.error(f"Error checking creator holding: {e}")
            return True
