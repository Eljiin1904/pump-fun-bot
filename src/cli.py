# src/cli.py

import argparse
import asyncio
import importlib
import os
import sys

# --- Add project root and src to path more robustly ---
# Assuming cli.py is in the 'src' directory
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_path = os.path.dirname(os.path.abspath(__file__)) # This is 'src'
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# --- End Path Setup -- -

from dotenv import load_dotenv
load_dotenv()
print("Attempting to load environment variables...") # Keep for startup feedback

# Setup logging AFTER path adjustments and dotenv
from src.utils.logger import get_logger, setup_file_logging # Use absolute import from src
# Ensure this path is correct relative to where you run the bot
setup_file_logging("pump_trading.log")
logger = get_logger(__name__)

# --- Core Imports ---
# Import necessary modules using absolute paths from src
from src.trading.trader import PumpTrader
from src.core.client import SolanaClient # Needed for async with block
from src.core.wallet import Wallet
# Removed unused ListenerFactory import

# --- Function to load config dynamically ---
def load_config(config_module_path='src.config'):
    """Dynamically loads configuration variables from a given module path."""
    import_path = None
    absolute_file_path = None
    try:
        # Clean the path for importlib (removes .py, uses dots)
        import_path = config_module_path.replace('/', '.').replace('\\', '.').removesuffix('.py')

        # Convert import path back to file path for checking existence
        # Assumes config files are in 'src/' relative to project_root
        relative_file_path = import_path.replace('.', os.sep) + '.py'
        absolute_file_path = os.path.join(project_root, relative_file_path)

        logger.debug(f"Checking for config file at: {absolute_file_path}")
        if not os.path.exists(absolute_file_path):
            # Try checking relative to 'src/' directly if project_root logic failed
            alt_path = os.path.join(src_path, os.path.basename(absolute_file_path))
            if os.path.exists(alt_path):
                logger.warning(f"Config found at {alt_path}, expected {absolute_file_path}. Adjusting...")
                absolute_file_path = alt_path
                # Adjust import path if necessary? Usually importlib handles src. prefix well if src's parent is in path
            else:
                raise FileNotFoundError(f"Config file not found. Checked: {absolute_file_path} and {alt_path}. Ensure it exists directly inside the 'src/' directory.")

        config_module = importlib.import_module(import_path)
        logger.info(f"Successfully loaded configuration from: {import_path}")
        return config_module

    except (ModuleNotFoundError, FileNotFoundError) as e:
        logger.error(f"Config load error (path='{config_module_path}' -> import='{import_path or 'N/A'}' -> file='{absolute_file_path or 'N/A'}'): {e}. Ensure the file exists in 'src/' and path is like 'src.config_name'.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error loading configuration from {config_module_path}: {e}", exc_info=True)
        sys.exit(1)

# --- parse_args function ---
def parse_args(default_config_module) -> argparse.Namespace:
    """Parse command line arguments, using defaults from loaded config."""
    parser = argparse.ArgumentParser(description="Trade tokens on pump.fun.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    # Define arguments
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

    # Set defaults safely using getattr from the loaded config module
    parser.set_defaults(
         amount=getattr(default_config_module, 'BUY_AMOUNT', 0.000001),
         buy_slippage=getattr(default_config_module, 'BUY_SLIPPAGE', 0.25),
         sell_slippage=getattr(default_config_module, 'SELL_SLIPPAGE', 0.25),
         rug_max_creator_hold_pct=getattr(default_config_module, 'RUG_MAX_CREATOR_HOLD_PERCENT', 0.25),
         rug_price_drop_pct=getattr(default_config_module, 'SELL_PRICE_DROP_THRESHOLD', 0.5),
         rug_liquidity_drop_pct=getattr(default_config_module, 'SELL_LIQUIDITY_DROP_THRESHOLD', 0.6),
    )
    final_args = parser.parse_args()
    final_args.loaded_config = default_config_module # Attach module for easy access
    return final_args


# --- main function ---
async def main() -> None:
    """Main entry point for the CLI."""
    # Load Config FIRST using temporary parse
    temp_parser = argparse.ArgumentParser(add_help=False); temp_parser.add_argument("--config", default="src.config"); temp_args, _ = temp_parser.parse_known_args()
    config = load_config(temp_args.config)
    # Parse all args using loaded config for defaults
    args = parse_args(config)

    # Get Config Values & Apply Overrides
    try:
        # Prioritize .env, then loaded config
        rpc_endpoint = os.environ.get("SOLANA_NODE_RPC_ENDPOINT") or getattr(config, "RPC_ENDPOINT", None)
        wss_endpoint = os.environ.get("SOLANA_NODE_WSS_ENDPOINT") or getattr(config, "WSS_ENDPOINT", None)
        # Use SOLANA_PRIVATE_KEY from .env as priority
        private_key = os.environ.get("SOLANA_PRIVATE_KEY") or getattr(config, "PRIVATE_KEY", None)

        if not rpc_endpoint or not rpc_endpoint.startswith(("http://", "https://")): raise ValueError("Invalid/missing RPC endpoint (check .env: SOLANA_NODE_RPC_ENDPOINT or config: RPC_ENDPOINT)")
        if not private_key or len(private_key) < 80: raise ValueError("Invalid/missing private key (check .env: SOLANA_PRIVATE_KEY or config: PRIVATE_KEY)")

        listener_type = getattr(config, "LISTENER_TYPE", "logs")
        if listener_type == "logs" and (not wss_endpoint or not wss_endpoint.startswith(("ws://", "wss://"))):
             logger.warning(f"WSS endpoint missing/invalid for logs listener. Deriving from RPC: {rpc_endpoint}")
             wss_fallback = rpc_endpoint.replace("https", "wss").replace("http", "ws")
             if wss_fallback == rpc_endpoint: raise ValueError("Cannot derive WSS endpoint from RPC for logs listener.")
             wss_endpoint = wss_fallback; logger.info(f"Using derived WSS: {wss_endpoint}")

        # Apply CLI overrides (already handled by args object from parse_args)
        buy_amount = args.amount; buy_slippage = args.buy_slippage; sell_slippage = args.sell_slippage
        rug_check_creator = args.rug_check_creator; rug_max_creator_hold_pct = args.rug_max_creator_hold_pct
        rug_check_price_drop = args.rug_check_price_drop; rug_price_drop_pct = args.rug_price_drop_pct
        rug_check_liquidity_drop = args.rug_check_liquidity_drop; rug_liquidity_drop_pct = args.rug_liquidity_drop_pct

        # Fetch remaining params from config using getattr with safe defaults
        # ** Use names matching PriorityFeeManager **
        cfg_fixed_fee = getattr(config, "FIXED_PRIORITY_FEE", 2000)
        cfg_extra_fee = getattr(config, "EXTRA_PRIORITY_FEE", 0.0) # Name matches PriorityFeeManager `extra_fee`
        cfg_cap = getattr(config, "HARD_CAP_PRIOR_FEE", 200000)     # Name matches PriorityFeeManager `cap`
        cfg_enable_dynamic_fee = getattr(config, "ENABLE_DYNAMIC_PRIORITY_FEE", False) # Name matches PFM `enable_dynamic_fee`
        cfg_enable_fixed_fee = getattr(config, "ENABLE_FIXED_PRIORITY_FEE", True) # Name matches PFM `enable_fixed_fee`
        # Other configs
        cfg_wait_after_creation = getattr(config, "WAIT_TIME_AFTER_CREATION", 15)
        cfg_wait_after_buy = getattr(config, "WAIT_TIME_AFTER_BUY", 60)
        cfg_wait_before_new = getattr(config, "WAIT_TIME_BEFORE_NEW_TOKEN", 15)
        cfg_sell_profit_threshold = getattr(config, "SELL_PROFIT_THRESHOLD", 0.5)
        cfg_sell_stoploss_threshold = getattr(config, "SELL_STOPLOSS_THRESHOLD", 0.2)
        cfg_max_token_age = getattr(config, "MAX_TOKEN_AGE", 60)
        cfg_max_retries = getattr(config, "MAX_RETRIES", 10)
        cfg_max_buy_retries = getattr(config, "MAX_BUY_RETRIES", cfg_max_retries) # Default to max_retries if specific one missing
        cfg_cleanup_mode = getattr(config, "CLEANUP_MODE", "disabled")
        cfg_cleanup_force_close = getattr(config, "CLEANUP_FORCE_CLOSE", False)
        cfg_cleanup_priority_fee = getattr(config, "CLEANUP_PRIORITY_FEE", False)
        cfg_confirm_timeout = getattr(config, "CONFIRM_TIMEOUT", 60) # Add CONFIRM_TIMEOUT to configs or keep default

    except (AttributeError, ValueError, FileNotFoundError) as e:
        logger.critical(f"Configuration error: {e}. Ensure required variables are set in config/env.", exc_info=True); sys.exit(1)

    # Initialize Components
    try:
        wallet = Wallet(private_key)
        logger.info(f"Wallet Pubkey: {wallet.pubkey}")
        # Use SolanaClient as context manager for auto-close
        async with SolanaClient(rpc_endpoint) as client:
             # --- Pass correctly named arguments matching PumpTrader.__init__ ---
             trader = PumpTrader(
                 client=client, # Pass client instance created by async with
                 rpc_endpoint=rpc_endpoint, # Pass informational values
                 wss_endpoint=wss_endpoint, # Pass informational values
                 private_key=private_key, # Needed by trader to init Wallet internally again? If not, remove.
                 buy_amount=buy_amount,
                 buy_slippage=buy_slippage,
                 sell_slippage=sell_slippage,
                 sell_profit_threshold=cfg_sell_profit_threshold,
                 sell_stoploss_threshold=cfg_sell_stoploss_threshold,
                 max_token_age=cfg_max_token_age,
                 wait_time_after_creation=cfg_wait_after_creation,
                 wait_time_after_buy=cfg_wait_after_buy,
                 wait_time_before_new_token=cfg_wait_before_new,
                 max_retries=cfg_max_retries,
                 max_buy_retries=cfg_max_buy_retries,
                 confirm_timeout=cfg_confirm_timeout,
                 listener_type=listener_type,
                 # Pass correct priority fee names matching PumpTrader.__init__
                 fixed_fee=cfg_fixed_fee,
                 extra_fee=cfg_extra_fee,
                 cap=cfg_cap,
                 enable_dynamic_fee=cfg_enable_dynamic_fee,
                 enable_fixed_fee=cfg_enable_fixed_fee,
                 # Pass rug check args
                 rug_check_creator=rug_check_creator,
                 rug_max_creator_hold_pct=rug_max_creator_hold_pct,
                 rug_check_price_drop=rug_check_price_drop,
                 rug_price_drop_pct=rug_price_drop_pct,
                 rug_check_liquidity_drop=rug_check_liquidity_drop,
                 rug_liquidity_drop_pct=rug_liquidity_drop_pct,
                 # Pass cleanup args
                 cleanup_mode=cfg_cleanup_mode,
                 cleanup_force_close=cfg_cleanup_force_close,
                 cleanup_priority_fee=cfg_cleanup_priority_fee,
             )
             # --- Start Trader ---
             await trader.start(
                 match_string=args.match,
                 bro_address=args.bro,
                 marry_mode=args.marry,
                 yolo_mode=args.yolo,
             )
    except Exception as e:
        logger.critical(f"Critical error initializing or running trader: {e}", exc_info=True)
        sys.exit(1)
    # No explicit client.close() needed here because 'async with' handles it

# --- __main__ block ---
if __name__ == "__main__":
    logger.info("Starting pump.fun bot CLI...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually (KeyboardInterrupt).")
    except Exception as e:
        # Catch errors during asyncio.run() itself or unhandled errors from main()
        logger.critical(f"Unhandled critical error during execution: {e}", exc_info=True)