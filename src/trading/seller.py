# src/trading/seller.py

from typing import Optional
from ..core.curve import BondingCurveManager
from ..core.priority_fee.manager import PriorityFeeManager
from ..trading.base import TradeResult
from ..utils.logger import get_logger

logger = get_logger(__name__)

class TokenSeller:
    def __init__(
        self,
        client,
        wallet,
        curve_manager: BondingCurveManager,
        fee_manager: PriorityFeeManager,
        slippage: float,
        max_retries: int,
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.fee_manager = fee_manager
        self.slippage = slippage
        self.max_retries = max_retries

    async def execute(self, info, buy_result: TradeResult) -> TradeResult:
        # 1) calculate target price based on buy_result.initial_sol_liquidity
        # 2) build & send sell tx on curve with slippage & priority fee
        success = True
        error = ""
        return TradeResult(
            success=success,
            error=error,
            initial_sol_liquidity=buy_result.initial_sol_liquidity,
            spent_lamports=0,
            acquired_tokens=0,
        )
