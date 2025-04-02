# src/cli.py

import argparse
import asyncio
import importlib
import os
import sys

# --- Path Setup ---
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_path = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path: sys.path.insert(0, project_root)

from dotenv import load_dotenv
load_dotenv()
print("Attempting to load environment variables...")

# --- Logging Setup ---
from src.utils.logger import get_logger, setup_file_logging
setup_file_logging("pump_trading.log")
logger = get_logger(__name__)

# --- Core Imports ---
from src.trading.trader import PumpTrader
from src.core.wallet import Wallet
from src.core.client import SolanaClient
# Removed unused ListenerFactory import


# --- load_config Function (Keep as is) ---
def load_config(config_module_path='src.config'):
    """Dynamically loads configuration variables from a given module path."""
    import_path = None; absolute_file_path = None
    try:
        import_path = config_module_path.replace('/', '.').replace('\\', '.').removesuffix('.py')
        relative_file_path = import_path.replace('.', os.sep) + '.py'
        absolute_file_path = os.path.join(project_root, relative_file_path)
        logger.debug(f"Checking for config file at: {absolute_file_path}")
        if not os.path.exists(absolute_file_path):
            raise FileNotFoundError(f"Config file not found: {absolute_file_path}. Check path/location (should be in 'src/').")
        config_module = importlib.import_module(import_path)
        logger.info(f"Successfully loaded configuration from: {import_path}")
        return config_module
    except (ModuleNotFoundError, FileNotFoundError) as e:
        logger.error(f"Config load error ('{config_module_path}' -> '{absolute_file_path or 'N/A'}'): {e}. Check path/location.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error loading configuration from {config_module_path}: {e}", exc_info=True)
        sys.exit(1)

# --- parse_args Function (Keep as is) ---
def parse_args(default_config_module) -> argparse.Namespace:
    """Parse command line arguments, using defaults from loaded config."""
    parser = argparse.ArgumentParser(description="Trade tokens on pump.fun.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--config", default="src.config", help="Path to config module (e.g., src.config_yolo_marry)")
    parser.add_argument("--yolo", action="store_true", help="Run continuously")
    parser.add_argument("--marry", action="store_true", help="Buy only, no selling")
    parser.add_argument("--match", help="Filter token name/symbol")
    parser.add_argument("--bro", help="Filter by creator address")
    parser.add_argument("--amount", type=float, default=argparse.SUPPRESS, help="SOL amount per buy (override)")
    parser.add_argument("--buy-slippage", type=float, default=argparse.SUPPRESS, help="Buy slippage (override)")
    parser.add_argument("--sell-slippage", type=float, default=argparse.SUPPRESS, help="Sell slippage (override)")
    parser.add_argument("--rug-check-creator", action="store_true", help="Enable creator hold check")
    parser.add_argument("--rug-max-creator-hold-pct", type=float, default=argparse.SUPPRESS, help="Max creator hold %% (override)")
    parser.add_argument("--rug-check-price-drop", action="store_true", help="Enable post-buy price drop check")
    parser.add_argument("--rug-price-drop-pct", type=float, default=argparse.SUPPRESS, help="Price drop %% threshold (override)")
    parser.add_argument("--rug-check-liquidity-drop", action="store_true", help="Enable post-buy liquidity drop check")
    parser.add_argument("--rug-liquidity-drop-pct", type=float, default=argparse.SUPPRESS, help="Liquidity drop %% threshold (override)")

    parser.set_defaults(
         amount=getattr(default_config_module, 'BUY_AMOUNT', 0.000001),
         buy_slippage=getattr(default_config_module, 'BUY_SLIPPAGE', 0.25),
         sell_slippage=getattr(default_config_module, 'SELL_SLIPPAGE', 0.25),
         rug_max_creator_hold_pct=getattr(default_config_module, 'RUG_MAX_CREATOR_HOLD_PERCENT', 0.25),
         rug_price_drop_pct=getattr(default_config_module, 'SELL_PRICE_DROP_THRESHOLD', 0.5),
         rug_liquidity_drop_pct=getattr(default_config_module, 'SELL_LIQUIDITY_DROP_THRESHOLD', 0.6),
    )
    final_args = parser.parse_args()
    final_args.loaded_config = default_config_module
    return final_args

# --- main function (Correct Priority Fee arg names passed to PumpTrader) ---
async def main() -> None:
    """Main entry point for the CLI."""
    temp_parser = argparse.ArgumentParser(add_help=False); temp_parser.add_argument("--config", default="src.config"); temp_args, _ = temp_parser.parse_known_args()
    config = load_config(temp_args.config)
    args = parse_args(config)

    try:
        # Get base configs
        rpc_endpoint = os.environ.get("SOLANA_NODE_RPC_ENDPOINT") or getattr(config, "RPC_ENDPOINT", None)
        wss_endpoint = os.environ.get("SOLANA_NODE_WSS_ENDPOINT") or getattr(config, "WSS_ENDPOINT", None)
        private_key = os.environ.get("SOLANA_PRIVATE_KEY") or getattr(config, "PRIVATE_KEY", None) # Check .env for SOLANA_PRIVATE_KEY
        listener_type = getattr(config, "LISTENER_TYPE", "logs")

        # Validate base configs
        if not rpc_endpoint or not rpc_endpoint.startswith(("http://", "https://")): raise ValueError("Invalid/missing RPC endpoint")
        if not private_key or len(private_key) < 80: raise ValueError("Invalid/missing private key (check .env: SOLANA_PRIVATE_KEY)") # Updated error msg
        if listener_type == "logs" and (not wss_endpoint or not wss_endpoint.startswith(("ws://", "wss://"))):
             logger.warning(f"Deriving WSS from RPC: {rpc_endpoint}")
             wss_fallback = rpc_endpoint.replace("https", "wss").replace("http", "ws")
             if wss_fallback == rpc_endpoint: raise ValueError("Cannot derive WSS from RPC.")
             wss_endpoint = wss_fallback; logger.info(f"Using derived WSS: {wss_endpoint}")

        # Apply CLI overrides
        buy_amount = args.amount; buy_slippage = args.buy_slippage; sell_slippage = args.sell_slippage
        rug_check_creator = args.rug_check_creator; rug_max_creator_hold_pct = args.rug_max_creator_hold_pct
        rug_check_price_drop = args.rug_check_price_drop; rug_price_drop_pct = args.rug_price_drop_pct
        rug_check_liquidity_drop = args.rug_check_liquidity_drop; rug_liquidity_drop_pct = args.rug_liquidity_drop_pct

        # Fetch remaining params from config using getattr
        # --- V V V FIX: Use variable names matching PriorityFeeManager's expected __init__ args V V V ---
        cfg_fixed_fee = getattr(config, "FIXED_PRIORITY_FEE", 2000)
        cfg_extra_fee = getattr(config, "EXTRA_PRIORITY_FEE", 0.0)             # Renamed: extra_fee
        cfg_cap = getattr(config, "HARD_CAP_PRIOR_FEE", 200000)         # Renamed: cap
        cfg_enable_dynamic_fee = getattr(config, "ENABLE_DYNAMIC_PRIORITY_FEE", False) # Renamed: enable_dynamic_fee
        cfg_enable_fixed_fee = getattr(config, "ENABLE_FIXED_PRIORITY_FEE", True)       # Renamed: enable_fixed_fee
        # --- END FIX ---
        cfg_wait_after_creation = getattr(config, "WAIT_TIME_AFTER_CREATION", 15)
        cfg_wait_after_buy = getattr(config, "WAIT_TIME_AFTER_BUY", 60)
        cfg_wait_before_new = getattr(config, "WAIT_TIME_BEFORE_NEW_TOKEN", 15)
        cfg_sell_profit_threshold = getattr(config, "SELL_PROFIT_THRESHOLD", 0.5)
        cfg_sell_stoploss_threshold = getattr(config, "SELL_STOPLOSS_THRESHOLD", 0.2)
        cfg_max_token_age = getattr(config, "MAX_TOKEN_AGE", 60)
        cfg_max_retries = getattr(config, "MAX_RETRIES", 10)
        cfg_max_buy_retries = getattr(config, "MAX_BUY_RETRIES", cfg_max_retries) # Default to max_retries
        cfg_cleanup_mode = getattr(config, "CLEANUP_MODE", "disabled")
        cfg_cleanup_force_close = getattr(config, "CLEANUP_FORCE_CLOSE", False)
        cfg_cleanup_priority_fee = getattr(config, "CLEANUP_PRIORITY_FEE", False)
        cfg_confirm_timeout = getattr(config, "CONFIRM_TIMEOUT", 60)

    except (AttributeError, ValueError, FileNotFoundError) as e:
        logger.critical(f"Configuration error: {e}.", exc_info=True); sys.exit(1)

    # Initialize Components
    try:
        wallet = Wallet(private_key)
        logger.info(f"Wallet Pubkey: {wallet.pubkey}")
        async with SolanaClient(rpc_endpoint) as client:
             # --- FIX: Pass correct keyword args to PumpTrader ---
             trader = PumpTrader(
                 client=client,
                 rpc_endpoint=rpc_endpoint, wss_endpoint=wss_endpoint, private_key=private_key,
                 buy_amount=buy_amount, buy_slippage=buy_slippage, sell_slippage=sell_slippage,
                 sell_profit_threshold=cfg_sell_profit_threshold, sell_stoploss_threshold=cfg_sell_stoploss_threshold,
                 max_token_age=cfg_max_token_age, wait_time_after_creation=cfg_wait_after_creation,
                 wait_time_after_buy=cfg_wait_after_buy, wait_time_before_new_token=cfg_wait_before_new,
                 max_retries=cfg_max_retries, max_buy_retries=cfg_max_buy_retries, confirm_timeout=cfg_confirm_timeout,
                 listener_type=listener_type,
                 # Pass the correctly named priority fee args
                 fixed_fee=cfg_fixed_fee,                     # Use cfg_ prefix for clarity
                 extra_fee=cfg_extra_fee,                   # Use cfg_ prefix for clarity
                 cap=cfg_cap,                             # Use cfg_ prefix for clarity
                 enable_dynamic_fee=cfg_enable_dynamic_fee, # Use cfg_ prefix for clarity
                 enable_fixed_fee=cfg_enable_fixed_fee,     # Use cfg_ prefix for clarity
                 # Rug check args
                 rug_check_creator=rug_check_creator, rug_max_creator_hold_pct=rug_max_creator_hold_pct,
                 rug_check_price_drop=rug_check_price_drop, rug_price_drop_pct=rug_price_drop_pct,
                 rug_check_liquidity_drop=rug_check_liquidity_drop, rug_liquidity_drop_pct=rug_liquidity_drop_pct,
                 # Cleanup args
                 cleanup_mode=cfg_cleanup_mode, cleanup_force_close=cfg_cleanup_force_close, cleanup_priority_fee=cfg_cleanup_priority_fee,
             )
             # --- END FIX ---
             await trader.start(
                 match_string=args.match, bro_address=args.bro,
                 marry_mode=args.marry, yolo_mode=args.yolo,
             )
    except Exception as e:
        logger.critical(f"Critical error initializing/running trader: {e}", exc_info=True)
        sys.exit(1)

# --- __main__ block (Keep as is) ---
if __name__ == "__main__":
    logger.info("Starting pump.fun bot CLI...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Unhandled critical error in main execution: {e}", exc_info=True)