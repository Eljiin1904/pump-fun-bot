# src/cli.py (Cleaned Syntax, Correct PumpTrader Instantiation)

import argparse
import importlib
import os
import sys
import asyncio
import logging
from dotenv import load_dotenv
from typing import Optional

# --- Early Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)
print("Attempting to load environment variables...")
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir) # Assumes cli.py is in src/
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)
print(f".env variables loaded (from {dotenv_path}, if exists).")
if project_root not in sys.path:
    sys.path.insert(0, project_root)
    logger.info(f"Added project root to sys.path: {project_root}")

# --- Core Imports ---
try:
    from src.core.client import SolanaClient
    from src.trading.trader import PumpTrader
    from src.utils.logger import get_logger # Re-get logger after potential path change
    logger = get_logger(__name__)
except ImportError as e:
    # Use print as logger might not be fully configured yet
    print(f"[Critical Import Error in cli.py] {e}")
    sys.exit(1)

# --- load_config Function (Keep Your Robust Version) ---
def load_config(config_path: str) -> dict:
    """Loads configuration from a Python module path."""
    logger.info(f"Attempting to load configuration from: {config_path}")
    # --- Make sure this function correctly parses ALL V3 config variables ---
    # --- from the specified python file (e.g., config_hybrid_strategy.py) ---
    # --- including the new fee parameters, rug check params, etc. ---
    # --- It should return a dictionary containing all config values. ---
    # (Pasting your robust load_config function here)
    import_path = config_path
    if import_path.endswith(".py"): import_path = import_path[:-3]
    if os.path.sep in import_path: import_path = import_path.replace(os.path.sep, '.')
    # Ensure it uses 'src.' prefix if referencing files within src
    if not import_path.startswith('src.') and 'src' + os.path.sep in config_path:
        import_path = 'src.' + import_path.split('src' + os.path.sep)[-1].replace(os.path.sep,'.')
    elif not import_path.startswith('src.'): # Simple case if path doesn't include 'src'
         potential_src_path = os.path.join(project_root, 'src', f"{import_path.replace('.', os.path.sep)}.py")
         if os.path.exists(potential_src_path): import_path = f"src.{import_path}"

    logger.info(f"Using import path: {import_path}")
    try:
        config_module = importlib.import_module(import_path)
        logger.info(f"Successfully loaded configuration module: {import_path}")
        # Define ALL expected variables and their defaults
        required_vars = ['SOLANA_NODE_RPC_ENDPOINT', 'SOLANA_NODE_WSS_ENDPOINT', 'SOLANA_PRIVATE_KEY']
        optional_vars_defaults = {
            'BUY_AMOUNT': 0.01, 'BUY_SLIPPAGE_BPS': 1500, 'SELL_SLIPPAGE_BPS': 2500,
            'SELL_PROFIT_THRESHOLD_PCT': 100.0, 'SELL_STOPLOSS_THRESHOLD_PCT': 30.0,
            'MAX_TOKEN_AGE_SECONDS': 45, 'WAIT_TIME_AFTER_CREATION_SECONDS': 3,
            'WAIT_TIME_AFTER_BUY_SECONDS': 10, 'WAIT_TIME_BEFORE_NEW_TOKEN_SECONDS': 5,
            'MAX_RETRIES': 3, 'MAX_BUY_RETRIES': 3, 'CONFIRM_TIMEOUT_SECONDS': 60,
            'LISTENER_TYPE': 'logs',
            # V3 Fee Params Defaults
            'ENABLE_DYNAMIC_FEE': True, 'ENABLE_FIXED_FEE': False,
            'PRIORITY_FEE_FIXED_AMOUNT': 10000, 'EXTRA_PRIORITY_FEE': 0,
            'HARD_CAP_PRIOR_FEE': 1000000,
             # V3 Rug Check Defaults
            'RUG_CHECK_CREATOR': False, 'RUG_MAX_CREATOR_HOLD_PCT': 20.0,
            'RUG_CHECK_PRICE_DROP': False, 'RUG_PRICE_DROP_PCT': 40.0,
            'RUG_CHECK_LIQUIDITY_DROP': False, 'RUG_LIQUIDITY_DROP_PCT': 50.0,
            # V3 Cleanup Defaults
            'CLEANUP_MODE': 'after_sell', 'CLEANUP_FORCE_CLOSE': False,
            'CLEANUP_PRIORITY_FEE': True,
            # V3 Raydium Defaults
            'SOL_THRESHOLD_FOR_RAYDIUM': 300.0
        }
        config = {}
        missing_vars = []
        # Load required vars
        for var in required_vars:
            value = getattr(config_module, var, os.getenv(var))
            config[var.lower()] = value # Use lowercase keys
            if value is None: missing_vars.append(var)
        if missing_vars: raise ValueError(f"Missing required config variables: {', '.join(missing_vars)}")
        # Load optional vars
        for var, default in optional_vars_defaults.items():
            config_val = getattr(config_module, var, os.getenv(var))
            key_lower = var.lower() # Use lowercase keys
            try: target_type = type(default);
            if config_val is None: config[key_lower] = default
            elif target_type == bool: config[key_lower] = str(config_val).lower() in ('true', '1', 't', 'yes', 'y')
            elif target_type == int: config[key_lower] = int(float(config_val)) # Handle float strings then cast
            elif target_type == float: config[key_lower] = float(config_val)
            else: config[key_lower] = str(config_val) # Default to string
            except (ValueError, TypeError) as type_e: logger.warning(f"Config '{var}': Can't convert '{config_val}' to {target_type}. Using default '{default}'. Error: {type_e}"); config[key_lower] = default
        logger.info("Configuration loaded successfully.")
        return config
    except ModuleNotFoundError: logger.error(f"Config module not found: '{import_path}'. Check --config path."); raise
    except ValueError as e: logger.error(f"Config error in '{import_path}': {e}"); raise
    except Exception as e: logger.error(f"Unexpected error loading config {import_path}: {e}", exc_info=True); raise

# --- Argument Parser Setup (Ensure it matches V3 flags/args) ---
parser = argparse.ArgumentParser(description="Pump.fun Trading Bot")
parser.add_argument("--config", required=True, help="Python module path to config (e.g., src.config_hybrid_strategy)")
parser.add_argument("--yolo", action="store_true", help="Enable YOLO mode (affects trader logic)")
parser.add_argument("--marry", action="store_true", help="Enable Marry mode (affects trader logic)")
parser.add_argument("--amount", type=float, help="Override BUY_AMOUNT from config")
# Add arguments to override specific config values if desired
parser.add_argument("--dynamic-fee", dest='enable_dynamic_fee', action='store_true', default=None, help="Force enable dynamic priority fee")
parser.add_argument("--no-dynamic-fee", dest='enable_dynamic_fee', action='store_false', help="Force disable dynamic priority fee")
parser.add_argument("--fixed-fee", dest='enable_fixed_fee', action='store_true', default=None, help="Force enable fixed priority fee")
parser.add_argument("--fixed-fee-amount", type=int, help="Override PRIORITY_FEE_FIXED_AMOUNT")
# Rug check overrides
parser.add_argument("--rug-check-creator", dest='rug_check_creator_override', action='store_true', default=None, help="Enable creator holding rug check")
parser.add_argument("--no-rug-check-creator", dest='rug_check_creator_override', action='store_false', help="Disable creator holding rug check")
parser.add_argument("--rug-max-creator-hold-pct", type=float, help="Override RUG_MAX_CREATOR_HOLD_PCT threshold")
# Add other rug/cleanup/timeout overrides as needed...
# Filters
parser.add_argument("--match", type=str, help="Filter tokens by name/symbol (passed to listener)")
parser.add_argument("--bro", type=str, help="Filter tokens created by this address (passed to listener)")


# --- Async Main Function ---
async def main():
    args = parser.parse_args()
    logger.info("--- Starting main() ---")
    try:
        config = load_config(args.config)
    except Exception:
        logger.critical("Exiting due to config load failure."); sys.exit(1)

    # --- Apply CLI overrides to config dict ---
    if args.amount is not None: config['buy_amount'] = args.amount; logger.info(f"CLI override: buy_amount={config['buy_amount']}")
    if args.enable_dynamic_fee is not None: config['enable_dynamic_fee'] = args.enable_dynamic_fee; logger.info(f"CLI override: enable_dynamic_fee={config['enable_dynamic_fee']}")
    if args.enable_fixed_fee is not None: config['enable_fixed_fee'] = args.enable_fixed_fee; logger.info(f"CLI override: enable_fixed_fee={config['enable_fixed_fee']}")
    if args.fixed_fee_amount is not None: config['priority_fee_fixed_amount'] = args.fixed_fee_amount; logger.info(f"CLI override: priority_fee_fixed_amount={config['priority_fee_fixed_amount']}")
    # Apply rug check override
    if args.rug_check_creator_override is not None: config['rug_check_creator'] = args.rug_check_creator_override; logger.info(f"CLI override: rug_check_creator={config['rug_check_creator']}")
    if args.rug_max_creator_hold_pct is not None: config['rug_max_creator_hold_pct'] = args.rug_max_creator_hold_pct; logger.info(f"CLI override: rug_max_creator_hold_pct={config['rug_max_creator_hold_pct']}")
    # Add other overrides...


    logger.info("Configuration processed, initializing components...")
    trader_client: Optional[SolanaClient] = None
    trader: Optional[PumpTrader] = None

    try:
        # 1. Initialize SolanaClient Wrapper
        logger.info("Initializing SolanaClient wrapper...")
        trader_client = SolanaClient(
            rpc_endpoint=config['solana_node_rpc_endpoint'],
            wss_endpoint=config.get('solana_node_wss_endpoint')
        )
        logger.info("SolanaClient wrapper initialized.")

        # 2. Wallet and PFM initialized inside PumpTrader

        # 3. Initialize PumpTrader (Passing ALL config values from dict)
        logger.info("Initializing PumpTrader...")
        # Convert percentages/BPS from config to decimals needed by trader init
        config['buy_slippage'] = float(config['buy_slippage_bps']) / 10000.0
        config['sell_slippage'] = float(config['sell_slippage_bps']) / 10000.0
        config['sell_profit_threshold'] = float(config['sell_profit_threshold_pct']) / 100.0
        config['sell_stoploss_threshold'] = float(config['sell_stoploss_threshold_pct']) / 100.0
        config['rug_max_creator_hold_pct'] = float(config['rug_max_creator_hold_pct']) # Keep as % for trader init to convert
        config['rug_price_drop_pct'] = float(config['rug_price_drop_pct']) # Keep as % for trader init
        config['rug_liquidity_drop_pct'] = float(config['rug_liquidity_drop_pct']) # Keep as % for trader init

        # Instantiate PumpTrader, passing all required config values
        trader = PumpTrader(
            client=trader_client,
            rpc_endpoint=config['solana_node_rpc_endpoint'],
            wss_endpoint=config['solana_node_wss_endpoint'],
            private_key=config['solana_private_key'],
            # Pass all other config values directly using lowercase keys
            enable_dynamic_fee=config['enable_dynamic_fee'],
            enable_fixed_fee=config['enable_fixed_fee'],
            fixed_fee=config['priority_fee_fixed_amount'], # Ensure names match trader __init__
            extra_fee=config['extra_priority_fee'],
            hard_cap=config['hard_cap_prior_fee'],
            buy_amount=config['buy_amount'],
            buy_slippage=config['buy_slippage'],
            sell_slippage=config['sell_slippage'],
            sell_profit_threshold=config['sell_profit_threshold'],
            sell_stoploss_threshold=config['sell_stoploss_threshold'],
            max_token_age=config['max_token_age_seconds'],
            wait_time_after_creation=config['wait_time_after_creation_seconds'],
            wait_time_after_buy=config['wait_time_after_buy_seconds'],
            wait_time_before_new_token=config['wait_time_before_new_token_seconds'],
            max_retries=config['max_retries'],
            max_buy_retries=config['max_buy_retries'],
            confirm_timeout=config['confirm_timeout_seconds'],
            listener_type=config['listener_type'],
            rug_check_creator=config['rug_check_creator'],
            rug_max_creator_hold_pct=config['rug_max_creator_hold_pct'], # Pass percentage
            rug_check_price_drop=config['rug_check_price_drop'],
            rug_price_drop_pct=config['rug_price_drop_pct'], # Pass percentage
            rug_check_liquidity_drop=config['rug_check_liquidity_drop'],
            rug_liquidity_drop_pct=config['rug_liquidity_drop_pct'], # Pass percentage
            cleanup_mode=config['cleanup_mode'],
            cleanup_force_close=config['cleanup_force_close'],
            cleanup_priority_fee=config['cleanup_priority_fee'],
            sol_threshold_for_raydium=config['sol_threshold_for_raydium']
        )
        logger.info("PumpTrader Initialized.")

    # --- Catch Initialization Errors ---
    except KeyError as e: logger.critical(f"FATAL: Missing key during init, check config load/pass: {e}"); sys.exit(1)
    except TypeError as e: logger.critical(f"FATAL: TypeError during init (check PumpTrader signature): {e}", exc_info=True); sys.exit(1)
    except Exception as e: logger.critical(f"FATAL: Error initializing: {e}", exc_info=True); sys.exit(1)

    # --- Start the Trader Task ---
    trader_task: Optional[asyncio.Task] = None
    try:
        if trader is None: raise RuntimeError("Trader failed initialization.") # Should be caught above but defensive

        start_method_name = 'start_tasks' # Method name in trader.py
        if hasattr(trader, start_method_name):
             logger.info(f"Starting trader task '{start_method_name}'...")
             # Pass CLI args for modes/filters to start_tasks
             trader_task = asyncio.create_task(
                 getattr(trader, start_method_name)(
                    match_string=args.match, bro_address=args.bro,
                    marry_mode=args.marry, yolo_mode=args.yolo
                ), name="PumpTrader_Run"
             )
             await trader_task # Wait for completion or cancellation
             logger.info("Trader task finished.")
        else: logger.critical(f"FATAL: Trader object missing '{start_method_name}' method.")

    except asyncio.CancelledError: logger.info("Main trader task cancelled.")
    except Exception as e: logger.critical(f"FATAL: Error running trader task: {e}", exc_info=True)
    finally:
        # --- Final Cleanup ---
        logger.info("Attempting final cleanup...")
        if trader and hasattr(trader, 'stop'): logger.info("Stopping trader..."); try: await trader.stop() except Exception as e_stop: logger.error(f"Error stopping trader: {e_stop}")
        if trader_client and hasattr(trader_client, 'close') and not getattr(trader_client, '_closed', False): logger.info("Closing SolanaClient from cli.py..."); await trader_client.close(); logger.info("SolanaClient closed from cli.py.")
        if trader_task and not trader_task.done(): logger.warning("Force cancelling trader task..."); trader_task.cancel(); try: await asyncio.wait_for(trader_task, timeout=2.0) except Exception: pass
    logger.info("--- main() finished ---")

# --- Script Entry Point ---
if __name__ == "__main__":
    if '--config' not in sys.argv: print("\nERROR: --config required", file=sys.stderr); parser.print_help(sys.stderr); sys.exit(1)
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Interrupted by user.")
    except Exception as e: logger.critical(f"Top-level exception: {e}", exc_info=True)
    finally: logger.info("Script execution finished."); logging.shutdown()