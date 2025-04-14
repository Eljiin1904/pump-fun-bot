# src/cli.py

import argparse
import importlib
import os
import sys
import asyncio
from dotenv import load_dotenv
from typing import Optional # Added Optional for type hinting

# Early environment variables loading
print("Attempting to load environment variables...")
# Load .env from the project root, not necessarily the current working directory
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)
print(".env variables loaded (if file exists).")

# Add project root to sys.path
if project_root not in sys.path:
    sys.path.insert(0, project_root)
    print(f"Added project root to sys.path: {project_root}")

# Now import project modules - Use absolute imports from src
try:
    from src.core.client import SolanaClient
    # Import the specific AsyncClient class from solana-py for type hinting
    from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
    from src.core.wallet import Wallet
    from src.core.priority_fee.manager import PriorityFeeManager
    from src.trading.trader import PumpTrader
    from src.utils.logger import get_logger
except ImportError as e:
    print(f"[Critical Import Error] Failed to import core modules: {e}")
    print("Please ensure all dependencies are installed correctly in the virtual environment.")
    print("Check paths and ensure 'src' is recognized as a source root.")
    sys.exit(1)


logger = get_logger(__name__) # Initialize logger after imports

# --- Check Environment Variables ---
rpc_check = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
wss_check = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
key_check = os.getenv("SOLANA_PRIVATE_KEY")
bird_check = os.getenv("BIRDEYE_API_KEY") # Optional check
logger.debug(f"Post-load env check: RPC={rpc_check is not None}, "
             f"WSS={wss_check is not None}, PRIV_KEY={key_check is not None}, BIRD={bird_check is not None}")

def load_config(config_path: str):
    """Loads configuration from a Python module path."""
    logger.info(f"Attempting to load configuration from: {config_path}")
    # Assume config_path is like 'src.config_conservative_test'
    import_path = config_path # Use directly if it's already a module path
    if import_path.endswith(".py"):
        import_path = import_path[:-3]
    if not import_path.startswith('src.'):
         # Attempt to make it relative to src if just filename provided
         if os.path.exists(os.path.join(project_root, 'src', f"{import_path}.py")):
              import_path = f"src.{import_path}"
         else:
              # Try converting potential file path
              import_path = config_path.replace(os.path.sep, '.')
              if not import_path.startswith('src.'): # Last attempt to prefix
                    import_path = f"src.{import_path}"

    logger.info(f"Using import path: {import_path}")
    try:
        config_module = importlib.import_module(import_path)
        logger.info(f"Successfully loaded configuration module: {import_path}")
        # Define required vars from env or config file
        required_vars = ['SOLANA_NODE_RPC_ENDPOINT', 'SOLANA_NODE_WSS_ENDPOINT', 'SOLANA_PRIVATE_KEY']
        config = {}
        missing_vars = []
        for var in required_vars:
            value = getattr(config_module, var, os.getenv(var))
            if value is None:
                missing_vars.append(var)
            config[var.lower()] = value

        if missing_vars:
            raise ValueError(f"Missing required config variables: {', '.join(missing_vars)}")

        # Define optional vars with defaults
        optional_vars_defaults = {
            'BUY_AMOUNT': 0.01, 'BUY_SLIPPAGE_BPS': 1500, 'SELL_SLIPPAGE_BPS': 2500, # Adjusted defaults slightly
            'SELL_PROFIT_THRESHOLD_PCT': 100.0, 'SELL_STOPLOSS_THRESHOLD_PCT': 30.0,
            'MAX_TOKEN_AGE_SECONDS': 45, 'WAIT_TIME_AFTER_CREATION_SECONDS': 3,
            'WAIT_TIME_AFTER_BUY_SECONDS': 10, 'WAIT_TIME_BEFORE_NEW_TOKEN_SECONDS': 5,
            'MAX_RETRIES': 3, 'MAX_BUY_RETRIES': 3, 'CONFIRM_TIMEOUT_SECONDS': 60,
            'LISTENER_TYPE': 'logs', 'ENABLE_DYNAMIC_FEE': True, 'ENABLE_FIXED_FEE': False,
            'PRIORITY_FEE_FIXED_AMOUNT': 10000, 'EXTRA_PRIORITY_FEE': 0,
            'HARD_CAP_PRIOR_FEE': 1000000, 'RUG_CHECK_CREATOR': False,
            'RUG_MAX_CREATOR_HOLD_PCT': 20.0, 'RUG_CHECK_PRICE_DROP': False, 'RUG_PRICE_DROP_PCT': 40.0,
            'RUG_CHECK_LIQUIDITY_DROP': False, 'RUG_LIQUIDITY_DROP_PCT': 50.0, 'CLEANUP_MODE': 'after_sell',
            'CLEANUP_FORCE_CLOSE': False, 'CLEANUP_PRIORITY_FEE': True
        }
        for var, default in optional_vars_defaults.items():
            # Get from config module first, then env, then default
            config_val = getattr(config_module, var, os.getenv(var))
            config[var.lower()] = config_val if config_val is not None else default

        logger.info("Configuration loaded successfully.")
        return config
    except ModuleNotFoundError:
        logger.error(f"Config module not found: '{import_path}'. Ensure the path is correct (e.g., src.config_name) and the file exists.")
        raise
    except ValueError as e:
        logger.error(f"Config error in '{import_path}': {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error loading config {import_path}: {e}", exc_info=True)
        raise

async def main():
    args = parser.parse_args()
    logger.info("--- Starting main() ---")
    try:
        config = load_config(args.config)
    except Exception:
        logger.critical("Exiting due to config load failure.")
        sys.exit(1) # Exit if config fails

    logger.info("Configuration loaded, initializing components...")
    # Use the specific SolanaPyAsyncClient for the fee manager dependency check
    fee_manager_client: Optional[SolanaPyAsyncClient] = None
    # Use our wrapper SolanaClient for the trader
    trader_client: Optional[SolanaClient] = None
    trader: Optional[PumpTrader] = None # Initialize trader as None

    try:
        logger.debug("Initializing SolanaClient wrapper...")
        trader_client = SolanaClient(config['solana_node_rpc_endpoint'])

        # Initialize the raw SolanaPy AsyncClient needed by PriorityFeeManager
        logger.debug("Initializing SolanaPy AsyncClient for Fee Manager...")
        fee_manager_client = SolanaPyAsyncClient(config['solana_node_rpc_endpoint'])

        logger.debug("Initializing Wallet...")
        wallet = Wallet(config['solana_private_key'])
        logger.info(f"Wallet Pubkey: {wallet.pubkey}")

        logger.debug("Initializing PriorityFeeManager...")
        # Pass the raw AsyncClient here
        priority_fee_manager = PriorityFeeManager(
            client=fee_manager_client,
            enable_dynamic_fee=config['enable_dynamic_fee'],
            enable_fixed_fee=config['enable_fixed_fee'],
            fixed_fee=config['priority_fee_fixed_amount'],
            extra_fee=int(config['extra_priority_fee']), # Ensure extra fee is int
            hard_cap=config['hard_cap_prior_fee']
        )
        logger.info("Components (Wallet, Client, FeeManager) Initialized.")

        logger.debug("Initializing PumpTrader...")
        # Convert percentages/BPS from config/args
        buy_slip_decimal = float(config['buy_slippage_bps']) / 10000.0
        sell_slip_decimal = float(config['sell_slippage_bps']) / 10000.0
        sell_profit_decimal = float(config['sell_profit_threshold_pct']) / 100.0
        sell_stoploss_decimal = float(config['sell_stoploss_threshold_pct']) / 100.0
        # Use arg override if provided, else config, ensuring float conversion
        rug_creator_pct = float(args.rug_max_creator_hold_pct) if args.rug_max_creator_hold_pct is not None else float(config['rug_max_creator_hold_pct'])
        rug_creator_pct_decimal = rug_creator_pct / 100.0
        rug_price_drop_decimal = float(config['rug_price_drop_pct']) / 100.0
        rug_liq_drop_decimal = float(config['rug_liquidity_drop_pct']) / 100.0

        # Create PumpTrader instance
        trader = PumpTrader(
            client=trader_client, # Pass the SolanaClient wrapper
            wss_endpoint=config['solana_node_wss_endpoint'],
            private_key=config['solana_private_key'],
            buy_amount=float(args.amount) if args.amount is not None else float(config['buy_amount']),
            buy_slippage=buy_slip_decimal,
            sell_slippage=sell_slip_decimal,
            sell_profit_threshold=sell_profit_decimal,
            sell_stoploss_threshold=sell_stoploss_decimal,
            max_token_age=float(config['max_token_age_seconds']),
            wait_time_after_creation=float(config['wait_time_after_creation_seconds']),
            wait_time_after_buy=float(config['wait_time_after_buy_seconds']), # Ensure these are float
            wait_time_before_new_token=float(config['wait_time_before_new_token_seconds']),
            max_retries=int(config['max_retries']),
            max_buy_retries=int(config['max_buy_retries']),
            confirm_timeout=int(config['confirm_timeout_seconds']),
            listener_type=str(config['listener_type']),
            enable_dynamic_fee=bool(config['enable_dynamic_fee']),
            enable_fixed_fee=bool(config['enable_fixed_fee']),
            fixed_fee=int(config['priority_fee_fixed_amount']),
            extra_fee=int(config['extra_priority_fee']), # Pass int fee
            cap=int(config['hard_cap_prior_fee']),
            # Use CLI arg directly if provided via action='store_true', else config bool
            rug_check_creator=args.rug_check_creator or bool(config['rug_check_creator']),
            rug_max_creator_hold_pct=rug_creator_pct_decimal,
            rug_check_price_drop=bool(config['rug_check_price_drop']),
            rug_price_drop_pct=rug_price_drop_decimal,
            rug_check_liquidity_drop=bool(config['rug_check_liquidity_drop']),
            rug_liquidity_drop_pct=rug_liq_drop_decimal,
            cleanup_mode=str(config['cleanup_mode']),
            cleanup_force_close=bool(config['cleanup_force_close']),
            cleanup_priority_fee=bool(config['cleanup_priority_fee'])
        )
        logger.info("PumpTrader Initialized.")
    except KeyError as e:
        logger.critical(f"FATAL: Missing config key during initialization: {e}")
        # Ensure fee_manager_client is closed if initialization failed mid-way
        if fee_manager_client: await fee_manager_client.close()
        sys.exit(1)
    except Exception as e:
        logger.critical(f"FATAL: Error instantiating components: {e}", exc_info=True)
        if fee_manager_client: await fee_manager_client.close()
        sys.exit(1)

    # --- Start the Trader ---
    logger.info("Starting trader...")
    try:
        # --- FIX: Ensure trader object exists and has start method ---
        if trader and hasattr(trader, 'start'):
            await trader.start(
                match_string=args.match,
                bro_address=args.bro,
                marry_mode=args.marry,
                yolo_mode=args.yolo
            )
        elif trader is None:
             logger.critical("FATAL: Trader object was not initialized successfully.")
        else:
             logger.critical("FATAL: Trader object does not have a 'start' method.")
        # --- End Fix ---
    except Exception as e:
        logger.critical(f"FATAL: Critical error running trader: {e}", exc_info=True)
    finally:
        # --- FIX: Use robust closing logic for fee_manager_client ---
        logger.info("Attempting final client cleanup...")
        if fee_manager_client: # Close the raw client used by fee manager
            close_method = getattr(fee_manager_client, 'aclose', getattr(fee_manager_client, 'close', None))
            if close_method and asyncio.iscoroutinefunction(close_method):
                try: await close_method(); logger.info("Fee manager async client closed.")
                except Exception as e_close: logger.error(f"Error closing fee manager async client: {e_close}")
            elif close_method:
                 try: close_method(); logger.info("Fee manager async client closed (sync).")
                 except Exception as e_close: logger.error(f"Error closing fee manager async client (sync): {e_close}")
            else:
                 logger.warning("Fee manager async client has no close method or was already closed.")
        # Note: trader_client uses the same underlying connection via SolanaClient's __aexit__
        # if used within an `async with` block, which is not the case here.
        # We rely on the fee_manager_client close above.
        # If SolanaClient implemented __aenter__/__aexit__, we'd use that:
        # if trader_client: await trader_client.__aexit__(None, None, None) # Example if context managed

    logger.info("--- main() finished ---")

# Argument parser setup
parser = argparse.ArgumentParser(description="Pump.fun Trading Bot")
parser.add_argument("--config", required=True, help="Python module path to config (e.g., src.config_default)")
parser.add_argument("--yolo", action="store_true", help="Enable YOLO mode (process multiple tokens concurrently)")
parser.add_argument("--marry", action="store_true", help="Enable Marry mode (only process first detected token)")
parser.add_argument("--amount", type=float, help="Override buy amount in SOL (e.g., 0.01)")
parser.add_argument("--rug-check-creator", action="store_true", help="Enable creator holding percentage rug check")
# --- FIX: Escape % sign in help string ---
parser.add_argument("--rug-max-creator-hold-pct", type=float, help="Override creator hold %% threshold (e.g., 20.0 for 20%%)")
# --- End Fix ---
parser.add_argument("--match", type=str, help="Filter tokens by name/symbol containing this string (case-insensitive)")
parser.add_argument("--bro", type=str, help="Filter tokens created by this specific wallet address")
# Add other arguments as needed

if __name__ == "__main__":
    # Set logger level early if possible, or rely on default/config
    logger.info("Starting script execution...")

    # Check if required --config argument is provided before parsing fully
    if '--config' not in sys.argv:
         print("ERROR: the following arguments are required: --config", file=sys.stderr)
         parser.print_help(sys.stderr)
         sys.exit(1)

    try:
        # Ensure asyncio runs correctly, handle potential nesting issues
        try:
             asyncio.run(main())
        except RuntimeError as e:
             if "cannot run loop while another loop is running" in str(e) and 'nest_asyncio' not in sys.modules:
                  logger.warning("Detected potential nested asyncio loop. Applying nest_asyncio patch.")
                  import nest_asyncio
                  nest_asyncio.apply()
                  asyncio.run(main()) # Retry after applying patch
             else:
                  raise # Re-raise other RuntimeErrors
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")
    except Exception as e:
         logger.critical(f"Unhandled exception at top level: {e}", exc_info=True)
    finally:
        logger.info("Script execution finished.")