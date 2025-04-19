# src/trading/trader.py
import os
import sys
import asyncio
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from project .env
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)
# Ensure project root in path
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from src.core.client import SolanaClient
    from src.core.wallet import Wallet
    from src.core.priority_fee.manager import PriorityFeeManager
    from src.core.curve import BondingCurveManager
    from src.monitoring.listener_factory import ListenerFactory
    from src.trading.base import TokenInfo, TradeResult, TokenState
    from src.trading.buyer import TokenBuyer
    from src.trading.seller import TokenSeller
    from src.trading.raydium_seller import RaydiumSeller
    from src.data.raydium_data import get_raydium_pool_data
    from src.utils.logger import get_logger
    from src.utils.audit_logger import TradeAuditLogger
except ImportError as e:
    print(f"[Critical Import Error] {e}")
    sys.exit(1)

logger = get_logger(__name__)

class PumpTrader:
    """Orchestrates the two-stage trading process (bonding curve -> Raydium)."""
    def __init__(self,
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
        # Core components
        self.wallet = Wallet(private_key)
        self.solana_client = client
        self.priority_fee_manager = PriorityFeeManager(client)
        self.curve_manager = BondingCurveManager(self.solana_client)
        # Configuration parameters
        self.wss_endpoint = wss_endpoint
        self.listener_type = listener_type
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage        # fraction (e.g., 0.15 for 15%)
        self.sell_slippage = sell_slippage      # fraction (e.g., 0.25 for 25%)
        self.sell_profit_threshold = sell_profit_threshold    # fraction (e.g., 1.0 = 100%)
        self.sell_stoploss_threshold = sell_stoploss_threshold  # fraction (e.g., 0.3 = 30%)
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
        # Convert SOL threshold to lamports
        self.SOL_THRESHOLD_FOR_RAYDIUM = int(sol_threshold_for_raydium * 1_000_000_000)
        # Initialize trade audit logger
        self.audit_logger = TradeAuditLogger()
        # Mode flags
        self.marry_mode_flag = False
        self.yolo_mode_flag = False
        # Initialize trading components
        self.buyer = TokenBuyer(client=self.solana_client)
        self.seller = TokenSeller(client=self.solana_client)
        # Data structures for tracking tokens
        self.token_queue = asyncio.Queue()
        self.token_states: dict[str, str] = {}
        self.token_infos: dict[str, TokenInfo] = {}
        self.token_buy_results: dict[str, TradeResult] = {}
        self.traded_mints_session: set = set()
        # Trailing profit tracking (YOLO mode)
        self.profit_targets: dict[str, bool] = {}
        self.profit_target_times: dict[str, float] = {}
        self.peak_reserves: dict[str, int] = {}
        self.drop_trigger_after_profit: float = 0.05  # 5% drop trigger
        self.drop_window_sec: float = 30.0            # 30-second window for drop trigger

    async def start(self, match_string: Optional[str] = None, bro_address: Optional[str] = None,
                    marry_mode: bool = False, yolo_mode: bool = False):
        self.marry_mode_flag = marry_mode
        self.yolo_mode_flag = yolo_mode
        logger.info(f"Starting trader.")
        logger.info(f"Match: {match_string or 'None'} | Creator: {bro_address or 'None'} | Marry: {marry_mode} | YOLO: {yolo_mode} | Max Age: {self.max_token_age}s")
        # (Listener setup and tasks launch would go here)

    async def _handle_new_token(self, token_info: TokenInfo):
        """Processes a newly detected token: attempt buy and set up monitoring."""
        mint_str = str(token_info.mint)
        # Initial state
        self.token_states[mint_str] = TokenState.DETECTED
        self.token_infos[mint_str] = token_info
        buyer_result: Optional[TradeResult] = None
        # Optional delay after creation (for safety checks)
        if self.wait_time_after_creation > 0:
            await asyncio.sleep(self.wait_time_after_creation)
        # Execute purchase
        self.token_states[mint_str] = TokenState.BUYING
        try:
            buyer_result = await self.buyer.execute(token_info=token_info)
        except Exception as e:
            logger.error(f"Exception during buy for {token_info.symbol}: {e}", exc_info=True)
        if buyer_result and buyer_result.success:
            logger.info(f"Successfully bought {token_info.symbol}. Tx: {buyer_result.tx_signature}")
            self._log_trade("buy", token_info, buyer_result.price, buyer_result.amount, buyer_result.tx_signature)
            self.traded_mints_session.add(token_info.mint)
            self.token_buy_results[mint_str] = buyer_result
            # Transition to bonding curve monitoring
            self.token_states[mint_str] = TokenState.ON_BONDING_CURVE
        else:
            logger.warning(f"Buy failed for {token_info.symbol}.")
            self.token_states[mint_str] = TokenState.BUY_FAILED
            return
        # Start monitoring immediately in marry mode
        if self.marry_mode_flag:
            await self._monitor_bonding_curve_token(token_info)

    async def _monitor_active_trades(self):
        """Main loop monitoring all active tokens and handling state transitions."""
        while True:
            if self.marry_mode_flag:
                # Only one token at a time in marry mode
                if not self.token_states:
                    await asyncio.sleep(1)
                    continue
                mint_str, state = next(iter(self.token_states.items()))
                token_info = self.token_infos.get(mint_str)
                if state == TokenState.ON_BONDING_CURVE:
                    await self._monitor_bonding_curve_token(token_info)
                elif state == TokenState.ON_RAYDIUM:
                    await self._monitor_raydium_token(token_info)
                # Break out after handling the single token
                if state in (TokenState.SOLD_CURVE, TokenState.SOLD_RAYDIUM, TokenState.RUGGED):
                    break
            else:
                # Concurrent (YOLO) mode: handle all tokens concurrently
                tasks = []
                for mint_str, state in list(self.token_states.items()):
                    token_info = self.token_infos.get(mint_str)
                    if state == TokenState.ON_BONDING_CURVE:
                        tasks.append(self._monitor_bonding_curve_token(token_info))
                    elif state == TokenState.ON_RAYDIUM:
                        tasks.append(self._monitor_raydium_token(token_info))
                if tasks:
                    await asyncio.gather(*tasks)
                await asyncio.sleep(1)

    async def _monitor_bonding_curve_token(self, token_info: TokenInfo):
        """Monitor a token while it's trading on the bonding curve (Stage 1)."""
        mint_str = str(token_info.mint)
        initial_trade = self.token_buy_results.get(mint_str)
        initial_price = initial_trade.price if initial_trade else None
        initial_liq = initial_trade.initial_sol_liquidity if initial_trade else None
        # Wait a short time after buy (if configured) before applying sell logic
        if self.wait_time_after_buy > 0:
            logger.info(f"Waiting {self.wait_time_after_buy:.1f}s after buy for {token_info.symbol}")
            await asyncio.sleep(self.wait_time_after_buy)
        while mint_str in self.token_states and self.token_states[mint_str] == TokenState.ON_BONDING_CURVE:
            # Fetch current SOL reserves from bonding curve state
            current_state = await self.curve_manager.get_curve_state(token_info.bonding_curve)
            current_reserves = getattr(current_state, 'virtual_sol_reserves', None)
            if current_reserves is None:
                logger.warning(f"Could not fetch curve state for {token_info.symbol}.")
                self.token_states[mint_str] = TokenState.BUY_FAILED
                break
            current_lamports = int(current_reserves)
            initial_lamports = int(initial_liq) if initial_liq else 0
            # Check for rug pull via liquidity drop
            if self.rug_check_liquidity_drop and initial_lamports > 0 and current_lamports < initial_lamports:
                drop_pct = (initial_lamports - current_lamports) / initial_lamports * 100
                if drop_pct >= self.rug_liquidity_drop_pct * 100:
                    logger.critical(f"{token_info.symbol} liquidity dropped {drop_pct:.1f}%. Marking RUGGED.")
                    self.token_states[mint_str] = TokenState.RUGGED
                    # Clear trailing data tracking
                    self.profit_targets.pop(mint_str, None)
                    self.profit_target_times.pop(mint_str, None)
                    self.peak_reserves.pop(mint_str, None)
                    break
            sell_reason = None
            if self.yolo_mode_flag:
                # Trailing profit logic for YOLO mode
                if initial_lamports > 0:
                    if not self.profit_targets.get(mint_str) and current_lamports >= initial_lamports * (1 + self.sell_profit_threshold):
                        self.profit_targets[mint_str] = True
                        self.profit_target_times[mint_str] = asyncio.get_event_loop().time()
                        self.peak_reserves[mint_str] = current_lamports
                        logger.info(f"{token_info.symbol} profit target reached on curve (YOLO). Starting trailing.")
                    if self.profit_targets.get(mint_str):
                        # Update peak reserves
                        if current_lamports > (self.peak_reserves.get(mint_str) or 0):
                            self.peak_reserves[mint_str] = current_lamports
                        elapsed = asyncio.get_event_loop().time() - self.profit_target_times.get(mint_str, 0)
                        if elapsed <= self.drop_window_sec and current_lamports <= self.peak_reserves[mint_str] * (1 - self.drop_trigger_after_profit):
                            sell_reason = f"Trailing drop {self.drop_trigger_after_profit*100:.1f}% after profit"
            else:
                # Static profit target for non-YOLO mode
                if initial_lamports > 0 and current_lamports >= initial_lamports * (1 + self.sell_profit_threshold):
                    sell_reason = "Profit target reached"
            # Stop-loss check (always active)
            if initial_lamports > 0 and current_lamports <= initial_lamports * (1 - self.sell_stoploss_threshold):
                sell_reason = sell_reason or "Stop-loss triggered"
            # Transition to Raydium stage if liquidity threshold reached
            if current_lamports >= self.SOL_THRESHOLD_FOR_RAYDIUM:
                logger.info(f"{token_info.symbol} reached Raydium threshold (~{current_lamports/1e9:.1f} SOL). Transitioning to Raydium.")
                self.token_states[mint_str] = TokenState.ON_RAYDIUM
                # Clear trailing data when moving to Stage 2
                self.profit_targets.pop(mint_str, None)
                self.profit_target_times.pop(mint_str, None)
                self.peak_reserves.pop(mint_str, None)
                break
            if sell_reason:
                logger.warning(f"*** Sell trigger on bonding curve for {token_info.symbol}: {sell_reason}")
                self.token_states[mint_str] = TokenState.SELLING_CURVE
                seller_result = await self.seller.execute(token_info=token_info)
                if seller_result.success:
                    logger.info(f"Sold {token_info.symbol} on bonding curve. Tx: {seller_result.tx_signature}")
                    self.token_states[mint_str] = TokenState.SOLD_CURVE
                    self._log_trade("sell_curve", token_info, seller_result.price, seller_result.amount, seller_result.tx_signature)
                    if self.cleanup_mode == "after_sell":
                        logger.info(f"Performing cleanup for {token_info.symbol} after curve sell")
                        # (Cleanup actions would be executed here)
                    # Remove trailing tracking
                    self.profit_targets.pop(mint_str, None)
                    self.profit_target_times.pop(mint_str, None)
                    self.peak_reserves.pop(mint_str, None)
                    break
                else:
                    logger.error(f"Sell on curve failed for {token_info.symbol}: {seller_result.error_message}")
                    self.token_states[mint_str] = TokenState.ON_BONDING_CURVE
            await asyncio.sleep(1)

    async def _monitor_raydium_token(self, token_info: TokenInfo):
        """Monitor a token after it transitions to Raydium (Stage 2)."""
        mint_str = str(token_info.mint)
        initial_trade_result = self.token_buy_results.get(mint_str)
        while mint_str in self.token_states and self.token_states[mint_str] == TokenState.ON_RAYDIUM:
            raydium_data = await get_raydium_pool_data(mint_str)
            if not raydium_data or "price" not in raydium_data:
                logger.warning(f"Raydium data not yet available for {token_info.symbol}, retrying...")
                await asyncio.sleep(2)
                continue
            price = raydium_data["price"]
            sell_reason = None
            if price is not None and initial_trade_result and initial_trade_result.price and price >= initial_trade_result.price * (1 + self.sell_profit_threshold):
                sell_reason = "Raydium profit target reached"
            if price is not None and initial_trade_result and initial_trade_result.price and price <= initial_trade_result.price * (1 - self.sell_stoploss_threshold):
                sell_reason = sell_reason or "Raydium stop-loss triggered"
            if sell_reason:
                logger.warning(f"*** Sell trigger for {token_info.symbol} on Raydium: {sell_reason}")
                self.token_states[mint_str] = TokenState.SELLING_RAYDIUM
                # Fetch current token balance in our wallet (Associated Token Account)
                ata_address = Wallet.get_associated_token_address(self.wallet.pubkey, token_info.mint)
                balance_lamports = await self.solana_client.get_token_balance_lamports(ata_address, commitment="confirmed")
                amount_to_sell = balance_lamports or 0
                if amount_to_sell <= 0:
                    logger.error(f"No {token_info.symbol} balance to sell on Raydium.")
                    self.token_states[mint_str] = TokenState.SOLD_RAYDIUM
                    break
                raydium_seller = RaydiumSeller(self.solana_client, self.wallet,
                                               sell_slippage_bps=int(self.sell_slippage * 10000),
                                               max_retries=self.max_retries,
                                               confirm_timeout=self.confirm_timeout)
                sell_result = await raydium_seller.execute(token_info, amount_to_sell)
                if sell_result.success:
                    logger.info(f"Sold {token_info.symbol} on Raydium. Tx: {sell_result.tx_signature}")
                    self.token_states[mint_str] = TokenState.SOLD_RAYDIUM
                    price_used = sell_result.price or price
                    amount_tokens = sell_result.amount or (amount_to_sell / 1e9)
                    self._log_trade("sell_raydium", token_info, price_used, amount_tokens, sell_result.tx_signature)
                    if self.cleanup_mode == "after_sell":
                        logger.info(f"Performing cleanup for {token_info.symbol} after Raydium sell")
                        # (Any cleanup actions would be called here)
                    break
                else:
                    logger.error(f"Raydium sell failed for {token_info.symbol}: {sell_result.error_message}")
                    self.token_states[mint_str] = TokenState.ON_RAYDIUM
            await asyncio.sleep(2)

    def _log_trade(self, action: str, token_info: TokenInfo,
                   price: Optional[float], amount: Optional[float], tx_hash: Optional[str]) -> None:
        slippage_pct = 0.0
        if action == "buy":
            slippage_pct = self.buy_slippage * 100
        elif action.startswith("sell"):
            slippage_pct = self.sell_slippage * 100
        self.audit_logger.log_trade(action, token_info, price, amount, tx_hash, slippage_pct)
