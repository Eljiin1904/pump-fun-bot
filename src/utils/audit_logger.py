# src/utils/audit_logger.py

import json
from datetime import datetime, timezone
from typing import Optional

# Assuming these are defined in src.trading.base
from src.trading.base import TokenInfo, TradeResult
from src.core.curve import LAMPORTS_PER_SOL  # For converting lamports
from .logger import get_logger  # Use the main logger setup

audit_log = get_logger("AuditLogger")  # Dedicated logger instance


class AuditLogger:
    """
    Handles logging of trade events for auditing and analysis.
    (Basic console implementation)
    """

    def __init__(self, log_to_file: bool = False, filepath: str = "trade_audit.log"):
        self.log_to_file = log_to_file
        self.filepath = filepath
        # Could initialize file handling here if needed
        audit_log.info("AuditLogger initialized.")

    async def log_trade_event(
            self,
            event_type: str,  # e.g., "BUY_ATTEMPT", "BUY_SUCCESS", "SELL_CURVE_TP", "SELL_FAIL"
            token_info: Optional[TokenInfo],
            trade_result: Optional[TradeResult],
            buy_trade_result: Optional[TradeResult] = None,  # Provide the initial buy result for sell events
            extra_data: Optional[dict] = None
    ):
        """Logs a trade-related event."""
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type.upper(),
            "token_symbol": token_info.symbol if token_info else "N/A",
            "token_mint": token_info.mint_str if token_info else "N/A",
            "success": trade_result.success if trade_result else None,
            "signature": trade_result.signature if trade_result else None,
            "error": trade_result.error if trade_result else None,
            "error_type": trade_result.error_type if trade_result else None,
        }

        # Add details based on event type and available results
        if token_info:
            log_entry["token_decimals"] = token_info.decimals
            log_entry["token_creator"] = str(token_info.creator)

        if "BUY" in event_type and trade_result:
            log_entry[
                "buy_sol_spent"] = trade_result.spent_lamports / LAMPORTS_PER_SOL if trade_result.spent_lamports is not None else None
            log_entry["buy_tokens_acquired_lamports"] = trade_result.acquired_tokens
            if token_info and trade_result.acquired_tokens is not None:
                log_entry["buy_tokens_acquired_ui"] = trade_result.acquired_tokens / (10 ** token_info.decimals)
            log_entry["buy_initial_curve_liq_lamports"] = trade_result.initial_sol_liquidity

        if "SELL" in event_type and trade_result:
            log_entry["sell_tokens_sold_lamports"] = trade_result.sold_tokens
            if token_info and trade_result.sold_tokens is not None:
                log_entry["sell_tokens_sold_ui"] = trade_result.sold_tokens / (10 ** token_info.decimals)
            log_entry[
                "sell_sol_acquired"] = trade_result.acquired_sol / LAMPORTS_PER_SOL if trade_result.acquired_sol is not None else None
            log_entry[
                "sell_curve_liq_at_sell_lamports"] = trade_result.initial_sol_liquidity  # Liquidity at time of sell

            # Calculate PnL if buy result is provided
            if buy_trade_result and buy_trade_result.success and trade_result.success and \
                    buy_trade_result.spent_lamports is not None and trade_result.acquired_sol is not None:
                pnl_sol = (trade_result.acquired_sol - buy_trade_result.spent_lamports) / LAMPORTS_PER_SOL
                log_entry["pnl_sol"] = round(pnl_sol, 9)
                # Note: This doesn't account for transaction fees unless they are implicitly included in acquired_sol/spent_lamports

        if extra_data:
            log_entry.update(extra_data)

        # Log to console using the dedicated logger instance
        log_message = json.dumps(log_entry)
        audit_log.info(log_message)

        # Optionally log to file
        if self.log_to_file:
            try:
                with open(self.filepath, "a") as f:
                    f.write(log_message + "\n")
            except Exception as e:
                audit_log.error(f"Failed to write audit log to file {self.filepath}: {e}")