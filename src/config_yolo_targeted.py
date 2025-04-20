# Config for Targeted YOLO Buy (Continuous, Filtered, Sell Logic Active)
# Run with: python -m src.cli --config src.config_yolo_targeted --yolo --match "TOKEN_NAME" --amount 0.02 --buy-slippage 0.2

import os
from dotenv import load_dotenv
load_dotenv()
SOLANA_NODE_RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT", "DEFAULT_IF_MISSING")
SOLANA_NODE_WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT") # No default means None if not set
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# Trading parameters
BUY_AMOUNT: int | float = 0.02
BUY_SLIPPAGE: float = 0.20
SELL_SLIPPAGE: float = 0.20 # Match buy or adjust

# Sell thresholds
SELL_PROFIT_THRESHOLD: float = 0.50 # Example target
SELL_STOPLOSS_THRESHOLD: float = 0.20 # Example stop

# Priority fee configuration
ENABLE_DYNAMIC_PRIORITY_FEE: bool = False
ENABLE_FIXED_PRIORITY_FEE: bool = True
FIXED_PRIORITY_FEE: int = 50_000
EXTRA_PRIORITY_FEE: float = 0.0
HARD_CAP_PRIOR_FEE: int = 200_000

# Listener configuration
LISTENER_TYPE = "logs"

# Retry and timeout settings
MAX_RETRIES: int = 10

# Waiting periods
WAIT_TIME_AFTER_CREATION: int | float = 5
WAIT_TIME_AFTER_BUY: int | float = 60
WAIT_TIME_BEFORE_NEW_TOKEN: int | float = 10 # Pause for YOLO mode

# Token and account management
MAX_TOKEN_AGE: int | float = 60
CLEANUP_MODE: str = "disabled"
CLEANUP_FORCE_CLOSE_WITH_BURN: bool = False
CLEANUP_WITH_PRIORITY_FEE: bool = False

# Node provider configuration (TODO)
MAX_RPS: int = 50

# --- Keep the validation function ---
def validate_configuration() -> None:
    # (Validation code copied from original config.py - same as standard snipe)
    config_checks = [(BUY_AMOUNT, (int, float), 0, float('inf'), "BUY_AMOUNT must be a positive number"), (BUY_SLIPPAGE, float, 0, 1, "BUY_SLIPPAGE must be between 0 and 1"), (SELL_SLIPPAGE, float, 0, 1, "SELL_SLIPPAGE must be between 0 and 1"), (SELL_PROFIT_THRESHOLD, float, 0, float('inf'), "SELL_PROFIT_THRESHOLD must be non-negative"), (SELL_STOPLOSS_THRESHOLD, float, 0, 1, "SELL_STOPLOSS_THRESHOLD must be between 0 and 1"), (FIXED_PRIORITY_FEE, int, 0, float('inf'), "FIXED_PRIORITY_FEE must be a non-negative integer"), (EXTRA_PRIORITY_FEE, float, 0, float('inf'), "EXTRA_PRIORITY_FEE must be non-negative"), (HARD_CAP_PRIOR_FEE, int, 0, float('inf'), "HARD_CAP_PRIOR_FEE must be a non-negative integer"), (MAX_RETRIES, int, 0, 100, "MAX_RETRIES must be between 0 and 100")]
    for value, expected_type, min_val, max_val, error_msg in config_checks:
        if not isinstance(value, expected_type): raise ValueError(f"Type error: {error_msg}")
        if isinstance(value, (int, float)) and not (min_val <= value <= max_val): raise ValueError(f"Range error: {error_msg}")
    if ENABLE_DYNAMIC_PRIORITY_FEE and ENABLE_FIXED_PRIORITY_FEE: raise ValueError("Cannot enable both dynamic and fixed priority fees")
    if LISTENER_TYPE not in ["logs", "blocks"]: raise ValueError("LISTENER_TYPE must be 'logs' or 'blocks'")
    valid_cleanup_modes = ["disabled", "on_fail", "after_sell", "post_session"];
    if CLEANUP_MODE not in valid_cleanup_modes: raise ValueError(f"CLEANUP_MODE must be one of {valid_cleanup_modes}")
validate_configuration()