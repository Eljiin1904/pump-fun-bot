# src/utils/audit_logger.py
import os
import csv
from datetime import datetime
from typing import Optional
from src.trading.base import TokenInfo

class TradeAuditLogger:
    """Logger that appends trade actions to a CSV file (logs/trade_audit.csv)."""
    def __init__(self, log_file: str = "logs/trade_audit.csv"):
        self.log_file = log_file
        # Ensure logs directory exists
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        # Write CSV header if file is new or empty
        if not os.path.exists(self.log_file) or os.stat(self.log_file).st_size == 0:
            with open(self.log_file, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "action", "token_symbol", "token_mint",
                    "price_sol_per_token", "token_amount", "slippage_percent", "tx_hash"
                ])

    def log_trade(self, action: str, token_info: TokenInfo, price: Optional[float],
                  amount: Optional[float], tx_hash: Optional[str], slippage_pct: float):
        """Append a trade record (buy/sell event) to the audit CSV log."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.log_file, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp,
                    action,
                    token_info.symbol if token_info else "",
                    str(token_info.mint) if token_info and token_info.mint else "",
                    f"{price:.9f}" if price is not None else "",
                    f"{amount:.6f}" if amount is not None else "",
                    f"{slippage_pct:.2f}%",
                    tx_hash or ""
                ])
        except Exception as e:
            print(f"TradeAuditLogger error writing to CSV: {e}")
