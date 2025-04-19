import os
from dotenv import load_dotenv

# Load .env for any necessary variables
load_dotenv()

# --- Solana Network Configuration ---
SOLANA_NODE_RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
SOLANA_NODE_WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# --- Trading Parameters (Conservative Defaults) ---
BUY_AMOUNT = 0.001  # SOL - very small for testing
BUY_SLIPPAGE_BPS = 1500  # 15% slippage for buys
SELL_SLIPPAGE_BPS = 2500  # 25% slippage for sells
SELL_PROFIT_THRESHOLD_PCT = 100.0  # 100% profit target (2x)
SELL_STOPLOSS_THRESHOLD_PCT = 30.0  # 30% stop-loss

# --- Timing Parameters ---
MAX_TOKEN_AGE_SECONDS = 45
WAIT_TIME_AFTER_CREATION_SECONDS = 3
WAIT_TIME_AFTER_BUY_SECONDS = 60
WAIT_TIME_BEFORE_NEW_TOKEN_SECONDS = 10

# --- Transaction Settings ---
MAX_RETRIES = 2
MAX_BUY_RETRIES = 3
CONFIRM_TIMEOUT_SECONDS = 60

# --- Listener Configuration ---
LISTENER_TYPE = 'logs'

# --- Priority Fee Configuration ---
ENABLE_DYNAMIC_FEE = True
ENABLE_FIXED_FEE = False
PRIORITY_FEE_FIXED_AMOUNT = 0
EXTRA_PRIORITY_FEE = 0
HARD_CAP_PRIOR_FEE = 1000000

# --- Rug Pull Checks ---
RUG_CHECK_CREATOR = True
RUG_MAX_CREATOR_HOLD_PCT = 20.0
RUG_CHECK_PRICE_DROP = False
RUG_PRICE_DROP_PCT = 40.0
RUG_CHECK_LIQUIDITY_DROP = False
RUG_LIQUIDITY_DROP_PCT = 50.0

# --- Cleanup Configuration ---
CLEANUP_MODE = 'after_sell'
CLEANUP_FORCE_CLOSE = False
CLEANUP_PRIORITY_FEE = True

# --- Hybrid Strategy Configuration ---
TRADE_MODE = "hybrid"
PROFIT_TARGET = 0.10
DROP_TRIGGER_AFTER_PROFIT = 0.05
DROP_WINDOW_SEC = 30
FALLBACK_EXIT_TIMEOUT = 30
ENABLE_AUDIT_LOG = True
