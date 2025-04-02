# src/trading/trader.py

# ... (Keep all existing imports) ...
import asyncio
import json
import os
import time
from datetime import datetime
from typing import Optional, Set, Dict

from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAmount, UiTokenAmount
from solders.pubkey import Pubkey

from ..cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
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

logger = get_logger(__name__)

class PumpTrader:
    """Main class orchestrating the pump.fun trading process."""

    # --- V V V ENSURE THIS __init__ SIGNATURE IS CORRECT V V V ---
    def __init__(
        self,
        client: SolanaClient, # Accepts client instance
        rpc_endpoint: str,    # Keep even if client passed, maybe useful info
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
        # --- Names must match PriorityFeeManager ---
        enable_dynamic_fee: bool,
        enable_fixed_fee: bool,
        fixed_fee: int,           # <--- Make sure this exists
        extra_fee: float,         # <--- Make sure this exists
        cap: int,                 # <--- Make sure this exists
        # --- Rug check params ---
        rug_check_creator: bool,
        rug_max_creator_hold_pct: float,
        rug_check_price_drop: bool,
        rug_price_drop_pct: float,
        rug_check_liquidity_drop: bool,
        rug_liquidity_drop_pct: float,
        # --- Cleanup params ---
        cleanup_mode: str,
        cleanup_force_close: bool,
        cleanup_priority_fee: bool,
    ):
        """Initialize the PumpTrader."""
        self.wallet = Wallet(private_key)
        # Use the client instance passed from cli.py
        self.solana_client = client
        self.curve_manager = BondingCurveManager(self.solana_client)

        # Pass correct args to PriorityFeeManager
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_fee,
            enable_fixed_fee=enable_fixed_fee,
            fixed_fee=fixed_fee,       # Pass it through
            extra_fee=extra_fee,       # Pass it through
            cap=cap                    # Pass it through
        )

        self.wss_endpoint = wss_endpoint
        self.listener_type = listener_type
        self.token_listener: Optional[BaseTokenListener] = None

        # Store other trading parameters
        self.buy_amount = buy_amount; self.buy_slippage = buy_slippage; self.sell_slippage = sell_slippage
        self.sell_profit_threshold = sell_profit_threshold; self.sell_stoploss_threshold = sell_stoploss_threshold
        self.max_token_age = max_token_age; self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy; self.wait_time_before_new_token = wait_time_before_new_token
        self.max_retries = max_retries; self.max_buy_retries = max_buy_retries; self.confirm_timeout = confirm_timeout
        self.rug_check_creator = rug_check_creator; self.rug_max_creator_hold_pct = rug_max_creator_hold_pct
        self.rug_check_price_drop = rug_check_price_drop; self.rug_price_drop_pct = rug_price_drop_pct
        self.rug_check_liquidity_drop = rug_check_liquidity_drop; self.rug_liquidity_drop_pct = rug_liquidity_drop_pct
        self.cleanup_mode = cleanup_mode; self.cleanup_force_close = cleanup_force_close; self.cleanup_priority_fee = cleanup_priority_fee

        # Initialize buyer and seller
        self.buyer = TokenBuyer(
            self.solana_client, self.wallet, self.curve_manager, self.priority_fee_manager,
            self.buy_amount, self.buy_slippage, self.max_buy_retries, self.confirm_timeout
        )
        self.seller = TokenSeller(
            client=self.solana_client, wallet=self.wallet, curve_manager=self.curve_manager,
            priority_fee_manager=self.priority_fee_manager,
            slippage=self.sell_slippage,
            max_retries=self.max_buy_retries
        )

        # State variables
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        self.processed_tokens: Set[str] = set()
        self.token_timestamps: Dict[str, float] = {}
        self.traded_mints_session: Set[Pubkey] = set()

        logger.info(f"PumpTrader initialized. Wallet: {self.wallet.pubkey}")
        # Add more detailed init logging if desired


    # --- start method (Fix ListenerFactory instantiation/call) ---
    async def start(
        self,
        match_string: Optional[str] = None,
        bro_address: Optional[str] = None,
        marry_mode: bool = False,
        yolo_mode: bool = False,
    ):
        """Start the trading bot."""
        logger.info(f"Starting pump.fun trader")
        logger.info(f"Match: {match_string or 'None'}"); logger.info(f"Creator: {bro_address or 'None'}"); logger.info(f"Marry: {marry_mode}"); logger.info(f"YOLO: {yolo_mode}"); logger.info(f"Max Age: {self.max_token_age}s")

        # V V V FIX: Instantiate factory V V V
        factory = ListenerFactory()
        try:
            # Pass client instance
            self.token_listener = factory.create_listener(
                self.listener_type, self.wss_endpoint, self.solana_client
            )
            logger.info(f"Using {self.listener_type} listener")
        except ValueError as e: logger.critical(f"Listener creation fail: {e}"); return
        except Exception as e: logger.critical(f"Error creating listener: {e}", exc_info=True); return

        processor_task = asyncio.create_task(self._process_token_queue(marry_mode, yolo_mode))

        try:
            if self.token_listener:
                # V V V FIX: Call listen_for_tokens on the instance V V V
                await self.token_listener.listen_for_tokens(
                    self._queue_token, match_string, bro_address
                )
                await processor_task # Wait if listener finishes
            else: logger.error("Token listener failed initialization.")
        except asyncio.CancelledError: logger.info("Main trader task cancelled.")
        except Exception as e: logger.critical(f"Trading stopped: {e!s}", exc_info=True); processor_task.cancel()
        finally:
             logger.info("Shutting down trader...")
             # V V V Cleanup logic using optional chaining/hasattr V V V
             listener_stopped = False
             if self.token_listener and hasattr(self.token_listener, 'stop'):
                 try:
                     await self.token_listener.stop()
                     listener_stopped = True
                 except Exception as stop_e:
                     logger.error(f"Error stopping listener: {stop_e}")

             if processor_task and not processor_task.done():
                 processor_task.cancel()
                 try: await processor_task
                 except asyncio.CancelledError: pass # Expected

             if self.cleanup_mode == "post_session" and self.traded_mints_session:
                 logger.info("Performing post-session cleanup...")
                 try:
                     # V V V Ensure function signature matches V V V
                     await handle_cleanup_post_session(self.solana_client, self.wallet, list(self.traded_mints_session), self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
                 except Exception as clean_e:
                     logger.error(f"Error during post-session cleanup: {clean_e}", exc_info=True)

             # Client is closed by context manager in cli.py
             logger.info("Trader shutdown complete.")


    # --- _queue_token (Remove semicolon) ---
    async def _queue_token(self, token_info: TokenInfo) -> None:
        """Queue a token for processing if not already processed."""
        token_key = str(token_info.mint)
        if token_key in self.processed_tokens:
            return # Silently ignore if already processed/queued
        self.token_timestamps[token_key] = time.time() # Record discovery time
        await self.token_queue.put(token_info)
        logger.info(f"Queued new token: {token_info.symbol} ({token_info.mint})")

    # --- _process_token_queue (Remove semicolon, improve age check) ---
    async def _process_token_queue(self, marry_mode: bool, yolo_mode: bool) -> None:
        """Continuously process tokens from the queue, checking freshness."""
        while True:
             token_info: Optional[TokenInfo] = None # Ensure defined in outer scope
             try:
                 token_info = await self.token_queue.get()
                 token_key = str(token_info.mint)
                 if token_key in self.processed_tokens:
                     logger.debug(f"Token {token_key[:6]} already handled. Skipping.")
                     self.token_queue.task_done()
                     continue

                 discovery_time = self.token_timestamps.get(token_key)
                 token_age = -1.0 # Indicate age couldn't be determined initially
                 if discovery_time:
                      token_age = time.time() - discovery_time
                      if token_age > self.max_token_age:
                           logger.info(f"Skipping {token_info.symbol} - too old ({token_age:.1f}s > {self.max_token_age}s)")
                           self.processed_tokens.add(token_key)
                           self.token_queue.task_done()
                           continue
                 else: logger.warning(f"No timestamp for {token_key}. Processing anyway.")

                 self.processed_tokens.add(token_key) # Mark as processed
                 logger.info(f"Processing token: {token_info.symbol} (age: {token_age:.1f}s)")
                 await self._handle_token(token_info, marry_mode) # Removed unused yolo_mode arg
                 self.token_queue.task_done()

                 if not yolo_mode: logger.info("YOLO disabled. Exiting processor."); break

             except asyncio.CancelledError: logger.info("Token processor cancelled."); break
             except Exception as e:
                  token_id = f"token {token_info.symbol} ({token_info.mint})" if token_info else "unknown token"
                  logger.error(f"Error in token processing loop for {token_id}: {e}", exc_info=True)
                  # Ensure task_done is called if item was retrieved before error
                  if token_info: self.token_queue.task_done()


    # --- _handle_token (Remove yolo_mode arg, fix buyer/seller call) ---
    async def _handle_token(self, token_info: TokenInfo, marry_mode: bool) -> None:
        """Handles the lifecycle of trading a single token."""
        buy_success = False; buy_tx_sig = None; buyer_result: Optional[TradeResult] = None
        try:
            await self._save_token_info(token_info)
            logger.info(f"Waiting {self.wait_time_after_creation:.1f}s..."); await asyncio.sleep(self.wait_time_after_creation)

            if self.rug_check_creator:
                if not await self._check_creator_holding(token_info): logger.warning(f"Skipping {token_info.symbol}: Failed creator check."); return

            logger.info(f"Buying {self.buy_amount:.6f} SOL of {token_info.symbol}...");
            buyer_result = await self.buyer.execute(token_info=token_info) # Call execute explicitly
            buy_success = buyer_result.success; buy_tx_sig = buyer_result.tx_signature

            if buy_success: logger.info(f"Bought {token_info.symbol}. Tx: {buy_tx_sig}"); self._log_trade("buy", token_info, buyer_result.price, buyer_result.amount, buy_tx_sig); self.traded_mints_session.add(token_info.mint)
            else:
                logger.error(f"Failed buy {token_info.symbol}: {buyer_result.error_message}")
                # V V V Ensure cleanup function signature matches V V V
                if self.cleanup_mode == "on_fail": await handle_cleanup_after_failure(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
                return

            perform_rug_sell = False
            if not marry_mode and buy_success and (self.rug_check_price_drop or self.rug_check_liquidity_drop):
                logger.info(f"Performing post-buy rug checks for {token_info.symbol}...")
                immediate_sell_reason = await self._check_post_buy_conditions(token_info, buyer_result)
                if immediate_sell_reason: logger.warning(f"Immediate sell trigger: {immediate_sell_reason}"); perform_rug_sell = True

            if marry_mode: logger.info("Marry mode. Skipping sell."); return

            if not perform_rug_sell: logger.info(f"Waiting {self.wait_time_after_buy:.1f}s before sell check..."); await asyncio.sleep(self.wait_time_after_buy)

            logger.info(f"Checking sell conditions / Selling {token_info.symbol}...")
            # V V V Call seller.execute V V V
            # Optional: Pass perform_rug_sell if seller logic handles it differently
            seller_result: TradeResult = await self.seller.execute(token_info=token_info, buyer_result=buyer_result)

            if seller_result.success:
                logger.info(f"Sold {token_info.symbol}. Tx: {seller_result.tx_signature}"); self._log_trade("sell", token_info, seller_result.price, seller_result.amount, seller_result.tx_signature)
                # V V V Ensure cleanup function signature matches V V V
                if self.cleanup_mode == "after_sell": await handle_cleanup_after_sell(self.solana_client, self.wallet, token_info.mint, self.priority_fee_manager, self.cleanup_force_close, self.cleanup_priority_fee)
            else: logger.error(f"Failed sell {token_info.symbol}: {seller_result.error_message}")

        except Exception as e: logger.error(f"Unhandled error processing {token_info.symbol}: {e}", exc_info=True)


    # --- _save_token_info (Add encoding) ---
    async def _save_token_info(self, token_info: TokenInfo) -> None:
        os.makedirs("trades", exist_ok=True); file_name = os.path.join("trades", f"{str(token_info.mint)}.txt")
        data_to_save = {"name": token_info.name, "symbol": token_info.symbol, "uri": token_info.uri, "mint": str(token_info.mint), "bondingCurve": str(token_info.bonding_curve), "associatedBondingCurve": str(token_info.associated_bonding_curve), "user": str(token_info.user), "created_timestamp": token_info.created_timestamp, "website": token_info.website, "twitter": token_info.twitter, "telegram": token_info.telegram, "description": token_info.description}
        try:
            with open(file_name, "w", encoding="utf-8") as file: json.dump(data_to_save, file, indent=2) # Added encoding
            logger.info(f"Token information saved to {file_name}")
        except IOError as e: logger.error(f"Failed to save token info to {file_name}: {e}")


    # --- _log_trade (Add encoding) ---
    def _log_trade(self, action: str, token_info: TokenInfo, price: Optional[float], amount: Optional[float], tx_hash: Optional[str]) -> None:
        os.makedirs("trades", exist_ok=True); log_entry = {"timestamp": datetime.utcnow().isoformat(), "action": action, "token_address": str(token_info.mint), "symbol": token_info.symbol, "price": price, "amount": amount, "tx_hash": str(tx_hash)};
        try:
            with open("trades/trades.log", "a", encoding="utf-8") as log_file: log_file.write(json.dumps(log_entry) + "\n") # Added encoding
        except IOError as e: logger.error(f"Failed to write trade log: {e}")


    # --- Rug Check Methods (Keep robust type checks) ---
    async def _check_creator_holding(self, token_info: TokenInfo) -> bool:
        """Checks if the token creator holds too much supply. Returns True if OK to buy."""
        logger.info(f"Performing pre-buy creator holding check for {token_info.symbol}...")
        try:
            mint_pk = token_info.mint
            total_supply_resp = await self.solana_client.get_token_supply(mint_pk, commitment=Confirmed)
            if total_supply_resp is None or not isinstance(total_supply_resp.value, UiTokenAmount): logger.warning("Cannot get supply obj. Skip check."); return True
            try: total_supply = int(total_supply_resp.value.amount)
            except (ValueError, TypeError, AttributeError): logger.warning("Cannot parse supply. Skip check."); return True
            if total_supply <= 0: logger.warning("Total supply 0. Skip check."); return True
            creator_ata = Wallet.get_associated_token_address(mint=mint_pk, owner=token_info.user)
            creator_balance_resp = await self.solana_client.get_token_account_balance(creator_ata, commitment=Confirmed)
            creator_balance = 0
            if creator_balance_resp and isinstance(creator_balance_resp.value, UiTokenAmount):
                try: creator_balance = int(creator_balance_resp.value.amount)
                except (ValueError, TypeError, AttributeError): logger.warning(f"Cannot parse creator balance {creator_ata}. Assume 0.")
            if creator_balance <= 0: creator_percentage = 0.0; logger.info("Creator balance zero/not found.")
            else: creator_percentage = creator_balance / total_supply; logger.info(f"Creator {token_info.user} holds {creator_balance}/{total_supply} ({creator_percentage:.2%})")
            if creator_percentage > self.rug_max_creator_hold_pct: logger.warning(f"FAILED check: {creator_percentage:.2%} > {self.rug_max_creator_hold_pct:.2%}"); return False
            else: logger.info("Creator holding check PASSED."); return True
        except Exception as e: logger.error(f"Error during creator holding check: {e}", exc_info=True); return True

    async def _check_post_buy_conditions(self, token_info: TokenInfo, buyer_result: TradeResult) -> Optional[str]:
        """Checks for rapid price/liquidity drops immediately after buying. Returns reason string if rug detected."""
        try:
            current_state: Optional[BondingCurveState] = await self.curve_manager.get_curve_state(token_info.bonding_curve, commitment=Confirmed)
            if not current_state: logger.warning("Cannot get curve state post-buy."); return None
            original_buy_price = buyer_result.price
            initial_sol_liquidity_lamports = buyer_result.initial_sol_liquidity # In Lamports
            # Price Drop Check
            if self.rug_check_price_drop and original_buy_price is not None and original_buy_price > 1e-18:
                current_price = current_state.calculate_price()
                if current_price < original_buy_price:
                    price_drop_pct = (original_buy_price - current_price) / original_buy_price
                    logger.debug(f"Post-buy price: Buy={original_buy_price:.9f}, Now={current_price:.9f}, Drop={price_drop_pct:.2%}, Thr={self.rug_price_drop_pct:.2%}")
                    if price_drop_pct > self.rug_price_drop_pct: return f"Price drop {price_drop_pct:.1%}"
                else: logger.debug(f"Post-buy price hasn't dropped.")
            # Liquidity Drop Check (Lamports)
            if self.rug_check_liquidity_drop and initial_sol_liquidity_lamports is not None and initial_sol_liquidity_lamports > 0:
                current_sol_reserves_lamports = getattr(current_state, 'virtual_sol_reserves', None)
                if current_sol_reserves_lamports is not None:
                    current_sol_reserves_lamports = int(current_sol_reserves_lamports)
                    initial_sol_liquidity_lamports = int(initial_sol_liquidity_lamports)
                    if current_sol_reserves_lamports < initial_sol_liquidity_lamports:
                        liquidity_drop_pct = (initial_sol_liquidity_lamports - current_sol_reserves_lamports) / initial_sol_liquidity_lamports
                        logger.debug(f"Post-buy liq (lamports): Initial={initial_sol_liquidity_lamports}, Now={current_sol_reserves_lamports}, Drop={liquidity_drop_pct:.2%}, Thr={self.rug_liquidity_drop_pct:.2%}")
                        if liquidity_drop_pct > self.rug_liquidity_drop_pct: return f"Liquidity drop {liquidity_drop_pct:.1%}"
                    else: logger.debug(f"Post-buy liq hasn't dropped.")
                else: logger.warning("Could not get 'virtual_sol_reserves' for liq check.")
            return None
        except Exception as e: logger.error(f"Error during post-buy check: {e}", exc_info=True); return None