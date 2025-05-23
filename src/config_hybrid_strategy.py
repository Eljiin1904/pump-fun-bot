# src/config_hybrid_strategy.py

import os
from dotenv import load_dotenv
from solders.pubkey import Pubkey  # ← import Pubkey here

# Load .env from the project root
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)

# --- Solana Node Connection (Required) ---
SOLANA_NODE_RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
SOLANA_NODE_WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
if not SOLANA_NODE_RPC_ENDPOINT or not SOLANA_NODE_WSS_ENDPOINT:
    raise ValueError(
        "SOLANA_NODE_RPC_ENDPOINT and SOLANA_NODE_WSS_ENDPOINT must be set in .env or environment variables."
    )

# --- Wallet (Required) ---
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
if not SOLANA_PRIVATE_KEY:
    raise ValueError("SOLANA_PRIVATE_KEY must be set in .env or environment variables.")

# --- Trading Parameters ---
BUY_AMOUNT_SOL = 0.00007851
BUY_SLIPPAGE_BPS = 1500
RAYDIUM_SELL_SLIPPAGE_BPS = 100

# --- Sell Logic (Curve & Raydium TP/SL) ---
TAKE_PROFIT_MULTIPLIER = 15.0
STOP_LOSS_PERCENTAGE = 70.0

# --- Timing Parameters ---
MAX_TOKEN_AGE_SECONDS = 45
WAIT_TIME_AFTER_CREATION_SECONDS = 3
MONITORING_INTERVAL_SECONDS = 5.0

# --- Transaction Settings ---
MAX_BUY_RETRIES = 3
MAX_SELL_RETRIES = 2
MAX_RAYDIUM_SELL_RETRIES = 3
CONFIRM_TIMEOUT_SECONDS = 60

# --- Listener Configuration ---
LISTENER_TYPE = 'logs'

# --- Pump.fun Program ID (Base58) ---
# Convert your PROGRAM ID into a Pubkey up front.
# Make sure the string below is the **exact** 44‐character base58 address
# of the on‐chain pump.fun program on mainnet.
_DEFAULT_PUMP_FUN_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FUN_PROGRAM_ID = Pubkey.from_string(
    os.getenv("PUMP_FUN_PROGRAM_ID", _DEFAULT_PUMP_FUN_ID)
)

# --- Event Discriminator (8‐byte hex) ---
_CREATE_DISC = os.getenv("CREATE_EVENT_DISCRIMINATOR", "1b72a94ddeeb6376")
CREATE_EVENT_DISCRIMINATOR = bytes.fromhex(_CREATE_DISC)

# --- Priority Fee Configuration ---
ENABLE_DYNAMIC_FEE = True
ENABLE_FIXED_FEE = False
PRIORITY_FEE_FIXED_AMOUNT_MICROLAMPORTS = 10000
EXTRA_PRIORITY_FEE_MICROLAMPORTS = 5000
HARD_CAP_PRIORITY_FEE_MICROLAMPORTS = 1_500_000

# --- Compute Unit Limits ---
BUY_COMPUTE_UNIT_LIMIT = 300_000
SELL_COMPUTE_UNIT_LIMIT = 200_000
RAYDIUM_SELL_COMPUTE_UNIT_LIMIT = 600_000
CLEANUP_COMPUTE_UNIT_LIMIT = 50_000

# --- Rug Protection Settings ---
RUG_CHECK_CREATOR_ENABLED = True
RUG_MAX_CREATOR_HOLD_PCT = 20.0
RUG_CHECK_PRICE_DROP_ENABLED = True
RUG_PRICE_DROP_PCT = 40.0
RUG_CHECK_LIQUIDITY_DROP_ENABLED = True
RUG_LIQUIDITY_DROP_PCT = 50.0

# --- Raydium Transition Settings ---
RAYDIUM_ENABLED = True
SOL_THRESHOLD_FOR_RAYDIUM = 300.0
RAYDIUM_POOL_FETCH_RETRIES = 5
RAYDIUM_POOL_FETCH_DELAY_SECONDS = 10.0

# --- Account Cleanup ---
CLEANUP_ATAS_ON_SELL = True

print(f"DEBUG: Config module '{__name__}' loaded.")
