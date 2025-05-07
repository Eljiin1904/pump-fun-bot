# src/config.py

import os
from dotenv import load_dotenv

# Load .env from the project root
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir) 
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)

# --- Solana Node Connection (Required - MUST be in .env or environment) ---
SOLANA_NODE_RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com") # Default public RPC
SOLANA_NODE_WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT", "wss://api.mainnet-beta.solana.com") # Default public WSS
# It's highly recommended to use a private RPC provider (Helius, QuickNode, etc.) via .env

# --- Wallet (Required - MUST be in .env or environment) ---
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY") 
# No default for private key

# --- Trading Parameters ---
BUY_AMOUNT_SOL = 0.01      # Default buy amount in SOL
BUY_SLIPPAGE_BPS = 1500    # Default 15%
SELL_SLIPPAGE_BPS = 2500   # Default 25% for curve sells
RAYDIUM_SELL_SLIPPAGE_BPS = 100 # Default 1% for Raydium sells

# --- Sell Logic ---
TAKE_PROFIT_MULTIPLIER = 10.0 # Default 10x
STOP_LOSS_PERCENTAGE = 50.0   # Default 50% loss (value drops to 50% of buy)

# --- Timing Parameters ---
MAX_TOKEN_AGE_SECONDS = 60.0 
WAIT_TIME_AFTER_CREATION_SECONDS = 5.0 
MONITORING_INTERVAL_SECONDS = 7.5 # Slightly longer default check interval

# --- Transaction Settings ---
MAX_BUY_RETRIES = 3 
MAX_SELL_RETRIES = 2 
MAX_RAYDIUM_SELL_RETRIES = 3 
CONFIRM_TIMEOUT_SECONDS = 60 

# --- Listener Configuration ---
LISTENER_TYPE = 'logs' # Default to logs listener

# --- Priority Fee Configuration ---
ENABLE_DYNAMIC_FEE = True
ENABLE_FIXED_FEE = False
PRIORITY_FEE_FIXED_AMOUNT_MICROLAMPORTS = 10000 
EXTRA_PRIORITY_FEE_MICROLAMPORTS = 1000 
HARD_CAP_PRIORITY_FEE_MICROLAMPORTS = 1_000_000 # 0.001 SOL cap default

# --- Compute Unit Limits ---
BUY_COMPUTE_UNIT_LIMIT = 300_000  
SELL_COMPUTE_UNIT_LIMIT = 200_000 
RAYDIUM_SELL_COMPUTE_UNIT_LIMIT = 600_000 
CLEANUP_COMPUTE_UNIT_LIMIT = 50_000 

# --- Rug Protection Settings ---
RUG_CHECK_CREATOR_ENABLED = True 
RUG_MAX_CREATOR_HOLD_PCT = 25.0 # Default 25% 

RUG_CHECK_PRICE_DROP_ENABLED = True 
RUG_PRICE_DROP_PCT = 50.0 # Default 50% price drop trigger

RUG_CHECK_LIQUIDITY_DROP_ENABLED = True 
RUG_LIQUIDITY_DROP_PCT = 60.0 # Default 60% liquidity drop trigger

# --- Account Cleanup ---
CLEANUP_ATAS_ON_SELL = True # Default name used in Trader v2
CLEANUP_MODE = 'after_sell' # Options: 'after_sell', 'on_fail', 'post_session', 'never'
CLEANUP_WITH_PRIORITY_FEE = False # Default to False for cleanup TXs

# --- V3: Raydium Transition Settings ---
RAYDIUM_ENABLED = True # Default to enabled
SOL_THRESHOLD_FOR_RAYDIUM = 300.0 # Default 300 SOL
RAYDIUM_POOL_FETCH_RETRIES = 5 
RAYDIUM_POOL_FETCH_DELAY_SECONDS = 10.0 

# --- Account Cleanup ---
CLEANUP_ATAS_ON_SELL = True 

print(f"DEBUG: Base config module 'src.config.py' loaded (used only if no specific strategy config is provided).")