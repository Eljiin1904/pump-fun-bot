# src/config_hybrid_strategy.py

import os
from dotenv import load_dotenv

# Load env variables first to allow overrides
# Load .env from the project root
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)


# --- Solana Node Connection (Required - MUST be in .env or environment) ---
SOLANA_NODE_RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
SOLANA_NODE_WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT")

# --- Wallet (Required - MUST be in .env or environment) ---
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# --- Birdeye API Key (Optional - for Raydium checks, loaded from .env by default) ---
# BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY") # No need to define here usually

# --- Trading Parameters ---
# --- FIX: Set BUY_AMOUNT to $0.01 equivalent ---
BUY_AMOUNT = 0.00007851 # SOL (Approx $0.01 USD at time of calculation)
# --- End Fix ---
BUY_SLIPPAGE_BPS = 1500 # 15% slippage BPS for buys (moderate starting point)
SELL_SLIPPAGE_BPS = 2500 # 25% slippage BPS for sells (higher to ensure exit)

# --- Sell Logic ---
# Approximating "hold until 69k MC". 69k from ~4k is ~17x gain.
# Using 1500% (15x) as a high target. Adjust if needed.
SELL_PROFIT_THRESHOLD_PCT = 1500.0
SELL_STOPLOSS_THRESHOLD_PCT = 30.0 # Sell if price drops 30% below buy price

# --- Timing Parameters ---
MAX_TOKEN_AGE_SECONDS = 45 # Don't trade tokens older than 45 seconds from detection
WAIT_TIME_AFTER_CREATION_SECONDS = 3 # Short wait before buy attempt
# WAIT_TIME_AFTER_BUY_SECONDS = 60 # Less relevant for target-based selling
# Increased wait between tokens slightly to help rate limits
WAIT_TIME_BEFORE_NEW_TOKEN_SECONDS = 15

# --- Transaction Settings ---
MAX_RETRIES = 2 # Max general transaction retries (keep low)
MAX_BUY_RETRIES = 5 # Max retries specifically for the BUYER logic (increased)
CONFIRM_TIMEOUT_SECONDS = 60 # Max time to wait for transaction confirmation

# --- Listener Configuration ---
LISTENER_TYPE = 'logs' # Use 'logs' for pump.fun events

# --- Priority Fee Configuration (Using dynamic as base) ---
ENABLE_DYNAMIC_FEE = True
ENABLE_FIXED_FEE = False
PRIORITY_FEE_FIXED_AMOUNT = 10000 # Microlamports (only used if ENABLE_FIXED_FEE=True)
EXTRA_PRIORITY_FEE = 5000 # ADD extra Microlamports (0.000005 SOL) - Adjust as needed
HARD_CAP_PRIOR_FEE = 1500000 # Max Microlamports (0.0015 SOL) - Safety cap

# --- Rug Protection Settings (Heavy Protection Enabled) ---
RUG_CHECK_CREATOR = True # ENABLE creator holding check
RUG_MAX_CREATOR_HOLD_PCT = 20.0 # Max % creator can hold (20%) - Fairly strict

RUG_CHECK_PRICE_DROP = True # ENABLE post-buy rapid price drop check
RUG_PRICE_DROP_PCT = 40.0 # Sell if price drops > 40% immediately after buy

RUG_CHECK_LIQUIDITY_DROP = True # ENABLE post-buy rapid liquidity drop check
RUG_LIQUIDITY_DROP_PCT = 50.0 # Sell if virtual SOL drops > 50% immediately after buy

# --- Account Cleanup ---
CLEANUP_MODE = 'after_sell' # Clean up ATAs after successful sells
CLEANUP_FORCE_CLOSE = False # Don't force close dusty accounts
CLEANUP_PRIORITY_FEE = True # Use priority fees for cleanup

print(f"DEBUG: Config module 'src.config_hybrid_strategy.py' loaded.")