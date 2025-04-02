# src/trading/buyer.py

import asyncio
import struct
from typing import Final, Optional

# V V V FIX: Add Commitment, Confirmed imports V V V
from solana.rpc.commitment import Confirmed
from solders.account import Account
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
# Import necessary types
from solders.signature import Signature
from spl.token.instructions import create_associated_token_account

from .base import TokenInfo, Trader, TradeResult
from ..core.client import SolanaClient
# V V V FIX: Add BondingCurveState import V V V
from ..core.curve import BondingCurveManager, BondingCurveState
from ..core.priority_fee.manager import PriorityFeeManager
from ..core.pubkeys import (
    LAMPORTS_PER_SOL,
    TOKEN_DECIMALS,
    PumpAddresses,
    SystemAddresses,
)
from ..core.wallet import Wallet
from ..utils.logger import get_logger

logger = get_logger(__name__)

EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 16927863322537952870)

class TokenBuyer(Trader):
    """Handles buying tokens on pump.fun."""

    # --- Update __init__ signature slightly ---
    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        buy_amount: float, # Renamed from amount for clarity
        buy_slippage: float, # Renamed from slippage
        max_tx_retries: int, # Renamed from max_buy_retries
        confirm_timeout: int = 60,
    ):
        """Initialize token buyer."""
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.buy_amount = buy_amount
        self.buy_slippage_bps = int(buy_slippage * 10000)
        self.max_tx_retries = max_tx_retries
        self.confirm_timeout = confirm_timeout
        logger.info(f"TokenBuyer initialized with SOL amount: {self.buy_amount}, Slippage: {buy_slippage*100:.2f}% ({self.buy_slippage_bps} bps)")

    # --- execute method (includes debug block and liquidity capture) ---
    async def execute(self, token_info: TokenInfo, *args, **kwargs) -> TradeResult:
        """Execute buy operation for a given token."""
        buy_tx_sig: Optional[str] = None
        initial_sol_liquidity: Optional[int] = None
        curve_state: Optional[BondingCurveState] = None
        final_balance_raw: Optional[int] = None

        try:
            # 1. Fetch Curve State & Calculate Buy Parameters
            logger.debug(f"Fetching curve state for {token_info.symbol} ({token_info.bonding_curve})")
            curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve)
            if not curve_state:
                return TradeResult(success=False, error_message="Failed to fetch bonding curve state.")

            # Capture initial liquidity
            if hasattr(curve_state, 'virtual_sol_reserves'):
                initial_sol_liquidity = curve_state.virtual_sol_reserves
                logger.debug(f"Captured initial SOL liquidity: {initial_sol_liquidity} lamports")
            else:
                 logger.warning("Could not find virtual_sol_reserves on BondingCurveState.")

            # Calculations
            sol_amount_lamports = int(self.buy_amount * LAMPORTS_PER_SOL)
            if sol_amount_lamports <= 0: return TradeResult(success=False, error_message="Invalid SOL buy amount.")
            token_price_sol = curve_state.calculate_price()
            if token_price_sol <= 0: return TradeResult(success=False, error_message="Invalid token price calculated.")

            expected_token_output = self.buy_amount / token_price_sol
            min_token_output_raw = int((expected_token_output * (1 - self.buy_slippage_bps / 10000.0)) * (10**TOKEN_DECIMALS))
            if min_token_output_raw <= 0: logger.warning(f"Calculated minimum token output zero/negative ({min_token_output_raw} raw).")
            max_sol_cost_lamports = sol_amount_lamports

            logger.info(f"Attempting buy: ~{expected_token_output:.6f} {token_info.symbol}, min_out={min_token_output_raw} raw, amount={self.buy_amount:.8f} SOL, max_cost={max_sol_cost_lamports/LAMPORTS_PER_SOL:.8f} SOL")

            # 2. Ensure ATA Exists
            associated_token_account = Wallet.get_associated_token_address(token_info.mint, self.wallet.pubkey)
            ata_exists = await self._ensure_associated_token_account(token_info.mint, associated_token_account)
            if not ata_exists: return TradeResult(success=False, error_message="Failed ensure ATA exists.")

            # 3. Send Buy Transaction
            buy_tx_sig = await self._send_buy_transaction(token_info, associated_token_account, max_sol_cost_lamports, min_token_output_raw)
            if not buy_tx_sig: return TradeResult(success=False, error_message="Failed send buy tx.", initial_sol_liquidity=initial_sol_liquidity)

            # 4. Confirm Buy Transaction
            success = await self.client.confirm_transaction(buy_tx_sig, commitment=Confirmed, max_timeout_seconds=self.confirm_timeout)

            # 5. Process Result & Debug Logging
            if success:
                logger.info(f"Buy transaction confirmed: {buy_tx_sig}")
                # --- DEBUG BLOCK ---
                try:
                    await asyncio.sleep(2.0)
                    logger.debug(f"Fetching detailed tx info for {buy_tx_sig}...")
                    sig_obj = Signature.from_string(buy_tx_sig)
                    actual_client = await self.client.get_client()
                    tx_info_resp = await actual_client.get_transaction(sig_obj, max_supported_transaction_version=0, commitment=Confirmed)

                    if tx_info_resp and tx_info_resp.value:
                        tx_value = tx_info_resp.value
                        if tx_value.transaction and tx_value.transaction.meta:
                            meta = tx_value.transaction.meta
                            logger.info(f"--- Tx Meta Analysis {buy_tx_sig} ---")
                            logger.info(f"  Status (err): {meta.err}")
                            if meta.err: logger.error(f"  !!! Tx Error: {meta.err} !!!"); logger.error(f"  Logs: {meta.log_messages}")
                            if meta.post_token_balances:
                                found_balance = False
                                for balance in meta.post_token_balances:
                                    if balance.owner == self.wallet.pubkey and balance.mint == token_info.mint:
                                        raw_amount = getattr(balance.ui_token_amount, 'amount', 'N/A')
                                        logger.info(f"  >>> User ATA Post-Balance: {raw_amount} raw")
                                        final_balance_raw = int(raw_amount) if raw_amount != 'N/A' else None
                                        found_balance = True; break
                                if not found_balance: logger.warning(f"  >>> User ATA balance not found post-tx.")
                            else: logger.info("  >>> No post_token_balances found.")
                            logger.info(f"--- End Meta Analysis ---")
                            if meta.err: return TradeResult(success=False, error_message=f"Buy swap failed in confirmed tx: {meta.err}", tx_signature=buy_tx_sig, initial_sol_liquidity=initial_sol_liquidity)
                            if final_balance_raw is not None and final_balance_raw <= 0:
                                logger.error(f"!!! Buy confirmed but ZERO token balance ({final_balance_raw} raw). Tx: {buy_tx_sig}")
                                return TradeResult(success=False, error_message="Buy resulted in zero token balance.", tx_signature=buy_tx_sig, initial_sol_liquidity=initial_sol_liquidity)
                        else: logger.warning("Could not find tx.meta details.")
                    else: logger.warning("Could not fetch tx info.")
                except Exception as e: logger.error(f"Error during post-buy tx detail fetch: {e}", exc_info=True)
                # --- END DEBUG BLOCK ---
                return TradeResult(success=True, tx_signature=buy_tx_sig, amount=expected_token_output, price=token_price_sol, final_token_balance_raw=final_balance_raw, initial_sol_liquidity=initial_sol_liquidity)
            else: # Confirmation failed
                logger.error(f"Buy transaction confirmation failed: {buy_tx_sig}")
                return TradeResult(success=False, error_message="Tx confirmation failed", tx_signature=buy_tx_sig, initial_sol_liquidity=initial_sol_liquidity)
        except Exception as e:
            logger.error(f"Unhandled exception during buy for {token_info.symbol}: {e}", exc_info=True)
            return TradeResult(success=False, error_message=str(e), tx_signature=buy_tx_sig, initial_sol_liquidity=initial_sol_liquidity)


    async def _ensure_associated_token_account(self, mint: Pubkey, associated_token_account: Pubkey) -> bool:
        """Ensure associated token account exists, else create it."""
        try:
            account_info: Optional[Account] = await self.client.get_account_info(associated_token_account, commitment=Confirmed)
            if account_info is None:
                logger.info(f"Creating ATA {associated_token_account} for mint {mint}...")
                create_ata_ix = create_associated_token_account(payer=self.wallet.pubkey, owner=self.wallet.pubkey, mint=mint)
                priority_fee = await self.priority_fee_manager.calculate_priority_fee([mint, self.wallet.pubkey, SystemAddresses.PROGRAM, SystemAddresses.TOKEN_PROGRAM, SystemAddresses.ASSOCIATED_TOKEN_PROGRAM])
                tx_sig = await self.client.build_and_send_transaction([create_ata_ix], self.wallet.keypair, skip_preflight=True, max_retries=self.max_tx_retries, priority_fee=priority_fee, compute_unit_limit=200_000)
                if not tx_sig: return False
                confirmed = await self.client.confirm_transaction(tx_sig, commitment=Confirmed, max_timeout_seconds=self.confirm_timeout)
                if confirmed: logger.info(f"ATA created: {associated_token_account}"); return True
                else: logger.error(f"Failed confirm ATA creation tx: {tx_sig}"); return False
            else: logger.info(f"ATA already exists: {associated_token_account}"); return True
        except Exception as e: logger.error(f"Failed check/creation of ATA {associated_token_account}: {e}", exc_info=True); return False


    async def _send_buy_transaction(self, token_info: TokenInfo, associated_token_account: Pubkey, max_sol_cost_lamports: int, min_token_output_raw: int) -> Optional[str]:
        """Send the actual pump.fun buy transaction."""
        try:
            accounts = [
                AccountMeta(PumpAddresses.GLOBAL, False, False), AccountMeta(PumpAddresses.FEE, False, True),
                AccountMeta(token_info.mint, False, False), AccountMeta(token_info.bonding_curve, False, True),
                AccountMeta(token_info.associated_bonding_curve, False, True), AccountMeta(associated_token_account, False, True),
                AccountMeta(self.wallet.pubkey, True, True), AccountMeta(SystemAddresses.PROGRAM, False, False),
                AccountMeta(SystemAddresses.TOKEN_PROGRAM, False, False), AccountMeta(SystemAddresses.RENT, False, False),
                AccountMeta(PumpAddresses.EVENT_AUTHORITY, False, False), AccountMeta(PumpAddresses.PROGRAM, False, False),
            ]
            data = (EXPECTED_DISCRIMINATOR + struct.pack("<Q", min_token_output_raw) + struct.pack("<Q", max_sol_cost_lamports))
            logger.debug(f"Buy ix data: min_tokens={min_token_output_raw}, max_sol={max_sol_cost_lamports}")
            buy_ix = Instruction(PumpAddresses.PROGRAM, data, accounts)
            priority_fee = await self.priority_fee_manager.calculate_priority_fee([acc.pubkey for acc in accounts])
            tx_sig = await self.client.build_and_send_transaction([buy_ix], self.wallet.keypair, skip_preflight=True, max_retries=self.max_tx_retries, priority_fee=priority_fee, compute_unit_limit=600_000)
            return tx_sig
        except Exception as e:
            logger.error(f"Failed build/send buy tx for {token_info.symbol}: {e}", exc_info=True)
            return None