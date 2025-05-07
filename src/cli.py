# src/cli.py (Complete with PumpTrader init fix)

import argparse
import importlib
import os
import sys
import asyncio
import logging
from dotenv import load_dotenv
from typing import Optional, Dict, Any  # Added Dict, Any for type hinting

# Early environment variables loading
print("Attempting to load environment variables...")
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)
print(".env variables loaded (if file exists).")

# Add project root to sys.path if not already present
if project_root not in sys.path:
    sys.path.insert(0, project_root)
    print(f"Added project root to sys.path: {project_root}")

# --- Core Imports ---
# Wrap in try/except to catch early import failures
try:
    from src.core.client import SolanaClient
    # Import the specific AsyncClient from solana library for type hinting/direct use
    from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
    from src.core.wallet import Wallet
    from src.core.priority_fee.manager import PriorityFeeManager
    from src.trading.trader import PumpTrader
    from src.utils.logger import get_logger
except ImportError as e:
    print(f"[Critical Import Error] Failed to import core modules: {e}")
    print("Please ensure all dependencies are installed and paths are set up correctly.")
    sys.exit(1)
# --- End Core Imports ---

# Initialize logger after potential path modifications and successful core imports
logger = get_logger(__name__)


# --- Environment Variable Checks (Optional Debug) ---
# rpc_check = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
# wss_check = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
# key_check = os.getenv("SOLANA_PRIVATE_KEY")
# logger.debug(f"Post-load env check: RPC={rpc_check is not None}, WSS={wss_check is not None}, PRIV_KEY={key_check is not None}")


def load_config(config_path: str) -> Dict[str, Any]:
    """Loads configuration from a Python module path into a dictionary."""
    logger.info(f"Attempting to load configuration from: {config_path}")

    # Normalize path to module import format
    import_path = config_path.replace('/', '.').replace('\\', '.')
    if import_path.endswith(".py"):
        import_path = import_path[:-3]

    # Ensure it starts with 'src.' if it refers to a file within src
    if not import_path.startswith('src.'):
        potential_src_path = os.path.join(project_root, 'src', f"{import_path.replace('.', os.sep)}.py")
        if os.path.exists(potential_src_path):
            import_path = f"src.{import_path}"
        else:  # Fallback if it's already a full path maybe? Less common.
            pass  # Keep original path if not clearly within src

    logger.info(f"Using import path: {import_path}")

    try:
        config_module = importlib.import_module(import_path)
        logger.info(f"Successfully loaded configuration module: {import_path}")

        # --- Extract Config Values ---
        config: Dict[str, Any] = {}

        # Required variables (fetch from module or environment)
        required_vars = ['SOLANA_NODE_RPC_ENDPOINT', 'SOLANA_NODE_WSS_ENDPOINT', 'SOLANA_PRIVATE_KEY']
        missing_vars = []
        for var in required_vars:
            value = getattr(config_module, var, os.getenv(var))
            if value is None:
                missing_vars.append(var)
            else:
                config[var] = value  # Store with original casing for init, lowercased later if needed

        if missing_vars:
            raise ValueError(f"Missing required config variables: {', '.join(missing_vars)}")

        # Optional variables with defaults (use uppercase matching file variables)
        # Define defaults here or import from a defaults module
        optional_vars_defaults = {
            'BUY_AMOUNT_SOL': 0.01, 'BUY_SLIPPAGE_BPS': 1500, 'SELL_SLIPPAGE_BPS': 2500,
            'RAYDIUM_SELL_SLIPPAGE_BPS': 100, 'TAKE_PROFIT_MULTIPLIER': 10.0,
            'STOP_LOSS_PERCENTAGE': 50.0, 'MAX_TOKEN_AGE_SECONDS': 60.0,
            'WAIT_TIME_AFTER_CREATION_SECONDS': 5.0, 'MONITORING_INTERVAL_SECONDS': 7.5,
            'MAX_BUY_RETRIES': 3, 'MAX_SELL_RETRIES': 2, 'MAX_RAYDIUM_SELL_RETRIES': 3,
            'CONFIRM_TIMEOUT_SECONDS': 60, 'LISTENER_TYPE': 'logs',
            'ENABLE_DYNAMIC_FEE': True, 'ENABLE_FIXED_FEE': False,
            'PRIORITY_FEE_FIXED_AMOUNT_MICROLAMPORTS': 10000,
            'EXTRA_PRIORITY_FEE_MICROLAMPORTS': 1000, 'HARD_CAP_PRIORITY_FEE_MICROLAMPORTS': 1_000_000,
            'BUY_COMPUTE_UNIT_LIMIT': 300_000, 'SELL_COMPUTE_UNIT_LIMIT': 200_000,
            'RAYDIUM_SELL_COMPUTE_UNIT_LIMIT': 600_000, 'CLEANUP_COMPUTE_UNIT_LIMIT': 50_000,
            'RUG_CHECK_CREATOR_ENABLED': True, 'RUG_MAX_CREATOR_HOLD_PCT': 25.0,
            'RUG_CHECK_PRICE_DROP_ENABLED': True, 'RUG_PRICE_DROP_PCT': 50.0,
            'RUG_CHECK_LIQUIDITY_DROP_ENABLED': True, 'RUG_LIQUIDITY_DROP_PCT': 60.0,
            'RAYDIUM_ENABLED': True, 'SOL_THRESHOLD_FOR_RAYDIUM': 300.0,
            'RAYDIUM_POOL_FETCH_RETRIES': 5, 'RAYDIUM_POOL_FETCH_DELAY_SECONDS': 10.0,
            'CLEANUP_ATAS_ON_SELL': True, 'CLEANUP_MODE': 'after_sell',
            'CLEANUP_WITH_PRIORITY_FEE': False
        }

        for var, default in optional_vars_defaults.items():
            config_val = getattr(config_module, var, os.getenv(var))  # Check module first, then env
            if config_val is not None:
                # Attempt type conversion based on default type, log warning on failure
                try:
                    config[var] = type(default)(config_val)
                except (ValueError, TypeError):
                    logger.warning(
                        f"Config Warning: Invalid type for {var} ('{config_val}'). Using default '{default}'."); config[
                        var] = default
            else:  # Use default if not found in module or env
                config[var] = default

        logger.info("Configuration loaded successfully.")
        # logger.debug(f"Loaded Config Dict: {config}") # Optional: Debug log loaded config
        return config

    except ModuleNotFoundError:
        logger.critical(
            f"FATAL: Config module not found: '{import_path}'. Ensure the path is correct and the file exists.")
        raise
    except ValueError as e:
        logger.critical(f"FATAL: Config error in '{import_path}': {e}")
        raise
    except Exception as e:
        logger.critical(f"FATAL: Unexpected error loading config {import_path}: {e}", exc_info=True)
        raise


async def main():
    parser = argparse.ArgumentParser(description="Pump.fun Trading Bot - Eljin")  # Added name
    # --- Define arguments ---
    parser.add_argument("--config", required=True,
                        help="Python module path to config (e.g., src.config_hybrid_strategy)")
    parser.add_argument("--yolo", action="store_true", help="Enable YOLO mode (process multiple tokens concurrently)")
    parser.add_argument("--marry", action="store_true",
                        help="Enable Marry mode (buy & hold first token, monitor until sell/rug/end)")
    # Allow overrides via CLI
    parser.add_argument("--amount", type=float, help="Override BUY_AMOUNT_SOL from config (e.g., 0.01)")
    parser.add_argument("--rug-check-creator", action='store_true', default=None,
                        help="Force enable creator holding check (overrides config False)")
    parser.add_argument("--no-rug-check-creator", action='store_true', default=None,
                        help="Force disable creator holding check (overrides config True)")
    parser.add_argument("--rug-max-creator-hold-pct", type=float,
                        help="Override RUG_MAX_CREATOR_HOLD_PCT %% from config (e.g., 15.0 for 15%%)")
    parser.add_argument("--match", type=str, help="Filter tokens by name/symbol (case-insensitive)")
    parser.add_argument("--bro", type=str, help="Filter tokens by creator wallet address")
    # --- End arguments ---

    args = parser.parse_args()
    logger.info("--- Starting main() ---")

    try:
        config = load_config(args.config)
    except Exception:
        logger.critical("Exiting due to config load failure.")
        sys.exit(1)

    # --- Apply CLI Overrides to Config Dictionary ---
    if args.amount is not None: config['BUY_AMOUNT_SOL'] = args.amount; logger.info(
        f"CLI Override: BUY_AMOUNT_SOL set to {args.amount}")
    if args.rug_check_creator is True: config['RUG_CHECK_CREATOR_ENABLED'] = True; logger.info(
        "CLI Override: Creator rug check ENABLED")
    if args.no_rug_check_creator is True: config['RUG_CHECK_CREATOR_ENABLED'] = False; logger.info(
        "CLI Override: Creator rug check DISABLED")
    if args.rug_max_creator_hold_pct is not None: config[
        'RUG_MAX_CREATOR_HOLD_PCT'] = args.rug_max_creator_hold_pct; logger.info(
        f"CLI Override: RUG_MAX_CREATOR_HOLD_PCT set to {args.rug_max_creator_hold_pct}%")
    # Note: rug_max_creator_hold_pct needs conversion to decimal (0.xx) inside PumpTrader init

    # --- Initialize Components ---
    logger.info("Initializing components...")
    trader_client: Optional[SolanaClient] = None
    fee_manager_client: Optional[SolanaPyAsyncClient] = None
    trader: Optional[PumpTrader] = None
    wallet: Optional[Wallet] = None
    priority_fee_manager: Optional[PriorityFeeManager] = None

    try:
        logger.debug("Initializing SolanaClient wrapper...")
        trader_client = SolanaClient(config['SOLANA_NODE_RPC_ENDPOINT'])

        logger.debug("Initializing SolanaPy AsyncClient for Fee Manager...")
        fee_manager_client = SolanaPyAsyncClient(config['SOLANA_NODE_RPC_ENDPOINT'])

        logger.debug("Initializing Wallet...")
        wallet = Wallet(config['SOLANA_PRIVATE_KEY'])
        logger.info(f"Wallet Pubkey: {wallet.pubkey}")

        logger.debug("Initializing PriorityFeeManager...")
        priority_fee_manager = PriorityFeeManager(
            client=fee_manager_client,  # Pass the raw client
            enable_dynamic_fee=bool(config['ENABLE_DYNAMIC_FEE']),
            enable_fixed_fee=bool(config['ENABLE_FIXED_FEE']),
            fixed_fee=int(config['PRIORITY_FEE_FIXED_AMOUNT_MICROLAMPORTS']),
            extra_fee=int(config['EXTRA_PRIORITY_FEE_MICROLAMPORTS']),
            hard_cap=int(config['HARD_CAP_PRIORITY_FEE_MICROLAMPORTS'])
        )
        logger.info("Components (Wallet, Client, FeeManager) Initialized.")

        # --- Initialize PumpTrader (Passing config dict) ---
        logger.debug("Initializing PumpTrader...")
        trader = PumpTrader(
            client=trader_client,
            wallet=wallet,
            priority_fee_manager=priority_fee_manager,
            config=config  # Pass the loaded and potentially overridden config dictionary
        )
        logger.info("PumpTrader Initialized.")
        # --- End PumpTrader Initialization ---

    except KeyError as e:
        logger.critical(f"FATAL: Missing essential config key during initialization: {e}")
        if fee_manager_client: await fee_manager_client.close()
        if trader_client: await trader_client.close()
        sys.exit(1)
    except Exception as e:
        logger.critical(f"FATAL: Error instantiating components: {e}", exc_info=True)
        if fee_manager_client: await fee_manager_client.close()
        if trader_client: await trader_client.close()
        sys.exit(1)

    # --- Start the Trader ---
    trader_task: Optional[asyncio.Task] = None
    try:
        if trader:  # Check if trader was initialized successfully
            logger.info("Starting trader task...")
            # Pass CLI mode flags to the start method
            trader_task = asyncio.create_task(
                trader.start(
                    listener_type=str(config['LISTENER_TYPE']),  # Get from config
                    wss_endpoint=str(config['SOLANA_NODE_WSS_ENDPOINT']),  # Get from config
                    match_string=args.match,
                    creator_address=args.bro,
                    yolo_mode=args.yolo,
                    marry_mode=args.marry
                ),
                name="PumpTrader_Start"
            )
            await trader_task
        else:
            logger.critical("FATAL: Trader object was not initialized successfully. Cannot start.")

    except asyncio.CancelledError:
        logger.info("Main trader task was cancelled by stop() or external signal.")
    except Exception as e:
        logger.critical(f"FATAL: Unhandled error running trader task: {e}", exc_info=True)
    finally:
        # --- Final Cleanup ---
        logger.info("Main task finished or terminated. Attempting final cleanup...")
        # Trader's internal stop() method should handle cancelling its own tasks.
        # We mainly need to ensure clients are closed here.

        # Close clients - Close wrapper first
        if trader_client:
            logger.info("Closing SolanaClient wrapper...")
            await trader_client.close()

        # Close raw client used by fee manager
        if fee_manager_client:
            # Check if already closed (httpx uses _is_closed)
            is_closed = getattr(fee_manager_client, '_is_closed', None)
            if is_closed is False:
                logger.info("Closing raw SolanaPy AsyncClient (fee manager)...")
                close_method = getattr(fee_manager_client, 'aclose', getattr(fee_manager_client, 'close', None))
                if close_method and asyncio.iscoroutinefunction(close_method):
                    try:
                        await close_method()
                    except Exception as e_close:
                        logger.error(f"Error closing raw fee manager async client: {e_close}")
                elif close_method:  # Sync close, less likely for AsyncClient but check
                    try:
                        close_method()
                    except Exception as e_close:
                        logger.error(f"Error closing raw fee manager sync client: {e_close}")
            elif is_closed is None:
                logger.warning("Could not determine if raw fee manager client is closed. Attempting close anyway.")
                # Add close attempt here if desired as a fallback

    logger.info("--- main() exiting ---")


if __name__ == "__main__":
    # Setup basic logging config before loading user config
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    # Check for required --config argument early
    if '--config' not in sys.argv:
        print("\nERROR: the following arguments are required: --config", file=sys.stderr)
        # Use ArgumentParser to show help correctly even before parsing
        temp_parser = argparse.ArgumentParser()
        temp_parser.add_argument("--config", required=True)
        temp_parser.print_help(sys.stderr)
        sys.exit(1)

    logger.info("Starting script execution...")

    try:
        # Use try/except for asyncio.run for cleaner exit logging
        try:
            asyncio.run(main())
        except RuntimeError as e_runtime:
            # Handle potential nested loop issue with nest_asyncio
            if "cannot run loop while another loop is running" in str(e_runtime):
                logger.warning("Detected potential nested asyncio loop. Applying nest_asyncio patch.")
                try:
                    import nest_asyncio

                    nest_asyncio.apply()
                    logger.info("nest_asyncio patch applied. Retrying asyncio.run(main()).")
                    asyncio.run(main())
                except ImportError:
                    logger.error("nest_asyncio not installed. Cannot patch nested loop. `pip install nest_asyncio`")
                    raise e_runtime  # Re-raise original error if patch fails
                except Exception as patch_e:
                    logger.error(f"Error applying nest_asyncio patch: {patch_e}")
                    raise e_runtime  # Re-raise original error if patch fails
            else:
                raise e_runtime  # Re-raise other runtime errors

    except KeyboardInterrupt:
        logger.info("Process interrupted by user (KeyboardInterrupt detected in __main__).")
    except Exception as e_top:
        logger.critical(f"Unhandled exception at top level (__main__): {e_top}", exc_info=True)
    finally:
        logger.info("Script execution finished.")
        logging.shutdown()  # Ensure all log handlers are flushed