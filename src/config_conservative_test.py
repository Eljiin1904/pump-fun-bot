

# --- Trading Parameters (Conservative Defaults) ---
BUY_AMOUNT = 0.001 # SOL - Start VERY small for testing
BUY_SLIPPAGE_BPS = 1500 # 15% slippage BPS for buys (moderate for new launches)
SELL_SLIPPAGE_BPS = 2500 # 25% slippage BPS for sells (higher to ensure exit if needed)

# Note: These might be overridden by --marry flag for bonding curve phase selling,
# but useful for Stage 2 (Raydium) later or if --marry is off.
SELL_PROFIT_THRESHOLD_PCT = 100.0 # Target 100% profit (2x) before selling based on profit
SELL_STOPLOSS_THRESHOLD_PCT = 30.0 # Sell if price drops 30% below buy price

# --- Timing Parameters ---
MAX_TOKEN_AGE_SECONDS = 45 # Don't trade tokens older than 45 seconds from detection
WAIT_TIME_AFTER_CREATION_SECONDS = 3 # Short wait before buy attempt (allows basic checks)
WAIT_TIME_AFTER_BUY_SECONDS = 60 # Wait time before checking normal sell conditions (if not --marry) - Less relevant for this test config
WAIT_TIME_BEFORE_NEW_TOKEN_SECONDS = 10 # Wait between processing new tokens in queue

# --- Transaction Settings ---
MAX_RETRIES = 2 # Max general transaction retries
MAX_BUY_RETRIES = 3 # Max retries specifically for the buy transaction
CONFIRM_TIMEOUT_SECONDS = 60 # Max time to wait for transaction confirmation

# --- Listener Configuration ---
LISTENER_TYPE = 'logs' # Use 'logs' or 'block' based on your preference/setup

# --- Priority Fee Configuration ---
ENABLE_DYNAMIC_FEE = True # Use dynamic priority fees based on network conditions
ENABLE_FIXED_FEE = False # Don't use a fixed fee amount
PRIORITY_FEE_FIXED_AMOUNT = 10000 # MicroLamports (used only if ENABLE_FIXED_FEE=True)
EXTRA_PRIORITY_FEE = 5000 # ADD extra MicroLamports to dynamic/fixed fee for better priority (adjust as needed)
HARD_CAP_PRIOR_FEE = 1500000 # Max MicroLamports for priority fee (e.g., 0.0015 SOL) - Important safety cap

# --- Rug Protection Settings (ENABLED & STRICTER) ---
RUG_CHECK_CREATOR = True # ENABLE creator holding check
RUG_MAX_CREATOR_HOLD_PCT = 20.0 # Max % creator can hold (e.g., 20%) - Stricter

RUG_CHECK_PRICE_DROP = True # ENABLE post-buy rapid price drop check
RUG_PRICE_DROP_PCT = 40.0 # Sell if price drops > 40% immediately after buy

RUG_CHECK_LIQUIDITY_DROP = True # ENABLE post-buy rapid liquidity drop check
RUG_LIQUIDITY_DROP_PCT = 50.0 # Sell if virtual SOL drops > 50% immediately after buy

# --- Account Cleanup ---
CLEANUP_MODE = 'after_sell' # Options: 'disabled', 'on_fail', 'after_sell', 'post_session'
                            # 'after_sell' cleans up ATA after a successful sell
CLEANUP_FORCE_CLOSE = False # Do not force close ATAs with dust balance
CLEANUP_PRIORITY_FEE = True # Use priority fees for cleanup transactions

# --- Birdeye API Key (Loaded from .env by raydium_data.py) ---
# No need to define BIRDEYE_API_KEY here if raydium_data loads it internally

print(f"DEBUG: Config module 'src.config_conservative_test.py' loaded by importer.") # Add print for debug