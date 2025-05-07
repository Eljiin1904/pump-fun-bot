# src/config_hybrid_strategy.py

import os
from dotenv import load_dotenv

# Load .env from the project root
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir) # Go up one level from src
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)

# --- Solana Node Connection (Required - MUST be in .env or environment) ---
SOLANA_NODE_RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
SOLANA_NODE_WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
if not SOLANA_NODE_RPC_ENDPOINT or not SOLANA_NODE_WSS_ENDPOINT:
    raise ValueError("SOLANA_NODE_RPC_ENDPOINT and SOLANA_NODE_WSS_ENDPOINT must be set in .env or environment variables.")

# --- Wallet (Required - MUST be in .env or environment) ---
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
if not SOLANA_PRIVATE_KEY:
    raise ValueError("SOLANA_PRIVATE_KEY must be set in .env or environment variables.")

# --- Trading Parameters ---
BUY_AMOUNT_SOL = 0.00007851 # Override base default if needed, matches your example
BUY_SLIPPAGE_BPS = 1500      # 15% slippage BPS for buys
# --- V3: Add Raydium Sell Slippage ---
# Use a different value for DEX swaps, typically much lower
RAYDIUM_SELL_SLIPPAGE_BPS = 100 # 1% slippage for Raydium sells

# --- Sell Logic (Applies to Curve TP/SL and Raydium TP/SL) ---
# These thresholds will be checked against the initial buy value from the curve.
TAKE_PROFIT_MULTIPLIER = 15.0 # Target 15x gain from initial buy value
STOP_LOSS_PERCENTAGE = 70.0   # Sell if value drops 70% (i.e., to 30% of buy value)

# --- Timing Parameters ---
MAX_TOKEN_AGE_SECONDS = 45
WAIT_TIME_AFTER_CREATION_SECONDS = 3
MONITORING_INTERVAL_SECONDS = 5.0 # How often to check price/state during monitoring

# --- Transaction Settings ---
MAX_BUY_RETRIES = 3 # Retries specific to the BUYER logic
MAX_SELL_RETRIES = 2 # Retries specific to the CURVE SELLER logic
MAX_RAYDIUM_SELL_RETRIES = 3 # Retries specific to the RAYDIUM SELLER logic
CONFIRM_TIMEOUT_SECONDS = 60

# --- Listener Configuration ---
LISTENER_TYPE = 'logs'

# --- Priority Fee Configuration ---
# These apply globally unless overridden per component
ENABLE_DYNAMIC_FEE = True
ENABLE_FIXED_FEE = False
PRIORITY_FEE_FIXED_AMOUNT_MICROLAMPORTS = 10000
EXTRA_PRIORITY_FEE_MICROLAMPORTS = 5000
HARD_CAP_PRIORITY_FEE_MICROLAMPORTS = 1_500_000

# --- Compute Unit Limits ---
# Define limits per action type
BUY_COMPUTE_UNIT_LIMIT = 300_000  # For curve buy (includes potential ATA creation)
SELL_COMPUTE_UNIT_LIMIT = 200_000 # For curve sell
RAYDIUM_SELL_COMPUTE_UNIT_LIMIT = 600_000 # For Raydium swap
CLEANUP_COMPUTE_UNIT_LIMIT = 50_000 # For closing ATA

# --- Rug Protection Settings ---
RUG_CHECK_CREATOR_ENABLED = True
RUG_MAX_CREATOR_HOLD_PCT = 20.0 # Max % creator can hold (20%)

RUG_CHECK_PRICE_DROP_ENABLED = True # Check for price drop from peak during CURVE monitoring
RUG_PRICE_DROP_PCT = 40.0 # Sell if value drops > 40% from peak on curve

RUG_CHECK_LIQUIDITY_DROP_ENABLED = True # Check for liquidity drop during CURVE monitoring
RUG_LIQUIDITY_DROP_PCT = 50.0 # Sell if virtual SOL drops > 50% from peak on curve

# --- V3: Raydium Transition Settings ---
RAYDIUM_ENABLED = True # Enable the transition check
SOL_THRESHOLD_FOR_RAYDIUM = 300.0 # SOL amount in REAL curve reserves to trigger transition
RAYDIUM_POOL_FETCH_RETRIES = 5 # How many times to try finding the pool via API
RAYDIUM_POOL_FETCH_DELAY_SECONDS = 10.0 # Delay between pool fetch retries

# --- Account Cleanup ---
CLEANUP_ATAS_ON_SELL = True # Clean up ATAs after successful sells (curve or Raydium)

print(f"DEBUG: Config module 'src.config_hybrid_strategy.py' loaded.")