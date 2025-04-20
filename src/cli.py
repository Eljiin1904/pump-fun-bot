import argparse
import importlib
import os
import sys
import asyncio
from dotenv import load_dotenv
from solders.pubkey import Pubkey

# Load environment variables
print("Attempting to load environment variables...")
load_dotenv()
print(".env variables loaded (if file exists).")

# Add src directory to Python path for module resolution
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.client import SolanaClient
from src.trading.trader import PumpTrader
from src.utils.logger import get_logger

logger = get_logger(__name__)

def load_config(config_path: str):
    """Load configuration from module or environment variables."""
    logger.info(f"Attempting to load configuration from: {config_path}")
    # Convert path to importable module
    import_path = config_path.replace(os.path.sep, '.')
    if not import_path.startswith('src.'):
        import_path = f"src.{import_path}"
    logger.info(f"Using import path: {import_path}")
    try:
        config_module = importlib.import_module(import_path)
        logger.info(f"Successfully loaded configuration module: {import_path}")
        # Required variables
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
        # Optional variables with defaults
        optional_vars_defaults = {
            'BUY_AMOUNT': 0.01,
            'BUY_SLIPPAGE_BPS': 1500,
            'SELL_SLIPPAGE_BPS': 2500,
            'SELL_PROFIT_THRESHOLD_PCT': 100.0,
            'SELL_STOPLOSS_THRESHOLD_PCT': 30.0,
            'MAX_TOKEN_AGE_SECONDS': 45,
            'WAIT_TIME_AFTER_CREATION_SECONDS': 3,
            'WAIT_TIME_AFTER_BUY_SECONDS': 10,
            'WAIT_TIME_BEFORE_NEW_TOKEN_SECONDS': 5,
            'MAX_RETRIES': 3,
            'MAX_BUY_RETRIES': 3,
            'CONFIRM_TIMEOUT_SECONDS': 60,
            'LISTENER_TYPE': 'logs',
            'ENABLE_DYNAMIC_FEE': True,
            'ENABLE_FIXED_FEE': False,
            'PRIORITY_FEE_FIXED_AMOUNT': 10000,
            'EXTRA_PRIORITY_FEE': 0,
            'HARD_CAP_PRIOR_FEE': 1000000,
            'RUG_CHECK_CREATOR': False,
            'RUG_MAX_CREATOR_HOLD_PCT': 20.0,
            'RUG_CHECK_PRICE_DROP': False,
            'RUG_PRICE_DROP_PCT': 40.0,
            'RUG_CHECK_LIQUIDITY_DROP': False,
            'RUG_LIQUIDITY_DROP_PCT': 50.0,
            'CLEANUP_MODE': 'after_sell',
            'CLEANUP_FORCE_CLOSE': False,
            'CLEANUP_PRIORITY_FEE': True,
            # New hybrid mode and logging parameters
            'TRADE_MODE': 'marry',
            'PROFIT_TARGET': 1.0,
            'DROP_TRIGGER_AFTER_PROFIT': 0.05,
            'DROP_WINDOW_SEC': 30,
            'FALLBACK_EXIT_TIMEOUT': 30,
            'ENABLE_AUDIT_LOG': False
        }
        for var, default in optional_vars_defaults.items():
            config_val = getattr(config_module, var, os.getenv(var))
            config[var.lower()] = config_val if config_val is not None else default
        logger.info("Configuration loaded successfully.")
        return config
    except ModuleNotFoundError:
        logger.error(f"Config module not found: '{import_path}'. Ensure the path is correct and file exists.")
        raise
    except ValueError as e:
        logger.error(f"Config error in '{import_path}': {e}")
        raise

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to the configuration module (e.g., src.config_default)")
    # ... (other args like --yolo, --marry, etc.)
    args = parser.parse_args()
    config = load_config(args.config)
    # Initialize Solana components
    logger.debug("Initializing SolanaClient...")
    trader_client = SolanaClient(config['solana_node_rpc_endpoint'])
    logger.debug("Initializing SolanaPy AsyncClient for Fee Manager...")
    fee_manager_client = trader_client._async_client  # raw AsyncClient
    logger.debug("Initializing Wallet...")
    wallet = trader_client.wallet
    logger.info(f"Wallet Pubkey: {wallet.pubkey}")
    logger.debug("Initializing PriorityFeeManager...")
    priority_fee_manager = trader_client.priority_fee_manager
    logger.info("Components (Wallet, Client, FeeManager) Initialized.")
    logger.debug("Initializing PumpTrader...")
    buy_slip_decimal = float(config['buy_slippage_bps']) / 10000.0
    sell_slip_decimal = float(config['sell_slippage_bps']) / 10000.0
    sell_profit_decimal = float(config['sell_profit_threshold_pct']) / 100.0
    sell_stoploss_decimal = float(config['sell_stoploss_threshold_pct']) / 100.0
    trader = PumpTrader(
        client=trader_client,
        wss_endpoint=config['solana_node_wss_endpoint'],
        private_key=config['solana_private_key'],
        buy_amount=float(config['buy_amount']),
        buy_slippage=buy_slip_decimal,
        sell_slippage=sell_slip_decimal,
        sell_profit_threshold=sell_profit_decimal,
        sell_stoploss_threshold=sell_stoploss_decimal,
        max_token_age=float(config['max_token_age_seconds']),
        wait_time_after_creation=float(config['wait_time_after_creation_seconds']),
        wait_time_after_buy=float(config['wait_time_after_buy_seconds']),
        wait_time_before_new_token=float(config['wait_time_before_new_token_seconds']),
        max_retries=int(config['max_retries']),
        max_buy_retries=int(config['max_buy_retries']),
        confirm_timeout=int(config['confirm_timeout_seconds']),
        listener_type=config['listener_type'],
        enable_dynamic_fee=bool(config['enable_dynamic_fee']),
        enable_fixed_fee=bool(config['enable_fixed_fee']),
        priority_fee_manager=priority_fee_manager,
        trade_mode=config['trade_mode'],
        profit_target=float(config['profit_target']),
        drop_trigger_after_profit=float(config['drop_trigger_after_profit']),
        drop_window_sec=int(config['drop_window_sec']),
        fallback_exit_timeout=int(config['fallback_exit_timeout']),
        enable_audit_log=bool(config['enable_audit_log'])
    )
    # ... (start trader tasks, event loop, etc.)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError:
        # Handle event loop issues
        import nest_asyncio
        nest_asyncio.apply()
        asyncio.run(main())