# learning-examples/cleanup_accounts.py

import asyncio
import os
import sys
from dotenv import load_dotenv
from typing import List, Optional, Any # Use Any for balance response type hint

# --- Solana/SPL Imports ---
from solders.pubkey import Pubkey
from spl.token.instructions import CloseAccountParams, close_account # Only need close_account here
from solana.rpc.commitment import Confirmed
# --- FIX: Remove incorrect TokenAmount import ---
# from solana.rpc.types import TokenAmount # REMOVE THIS LINE
# --- End Fix ---
from solana.rpc.async_api import AsyncClient
from solana.exceptions import SolanaRpcException
from solders.signature import Signature # Import for type hinting/usage

# --- Add Project Root to Path ---
# This allows importing from 'src' when run from the learning-examples dir
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir) # This should be the main project root
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# --- End Path ---

# --- Use Absolute Imports from src ---
try:
    # Import necessary components from your project
    from src.core.pubkeys import SolanaProgramAddresses, TOKEN_PROGRAM_ID # Import class and constant
    from src.core.wallet import Wallet
    from src.core.transactions import build_and_send_transaction, TransactionResult
    # Assuming basic logger setup if running standalone
    try: from src.utils.logger import get_logger
    except ImportError: import logging; logging.basicConfig(level=logging.INFO); logger = logging.getLogger(__name__) # Basic fallback logger
except ImportError as e:
    print(f"ERROR: Could not import required modules from src: {e}")
    print("Ensure you run this script from the project root or that src is in PYTHONPATH.")
    sys.exit(1)
# --- End Imports ---

# Load .env file from project root
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)
logger = get_logger(__name__) # Use the imported logger

RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# --- !!! IMPORTANT: Update this address !!! ---
# Replace with a MINT address of a token account YOU OWN and WANT TO CLOSE
# Using a placeholder that should cause an error if not changed
MINT_ADDRESS_TO_CLOSE_STR = "EXAMPLE_MINT_ADDRESS_REPLACE_ME"


async def close_account_if_exists(
    async_client: AsyncClient,
    wallet: Wallet,
    account_to_close: Pubkey,
    mint: Pubkey # Keep mint for logging/label
    ):
    """Safely close a token account if it exists and has zero balance."""
    logger.info(f"Attempting to process ATA: {account_to_close} for Mint: {mint}")
    try:
        # 1. Check if account exists
        acc_info_res = await async_client.get_account_info(account_to_close, commitment=Confirmed)
        # Check the 'value' attribute of the response object
        if acc_info_res is None or getattr(acc_info_res, 'value', None) is None:
            logger.info(f"Account does not exist or already closed: {account_to_close}")
            return TransactionResult(success=True, error_message="Skipped: Account does not exist", error_type="Skip")

        # 2. Check balance
        # get_token_account_balance returns a response object, check its 'value'
        balance_res = await async_client.get_token_account_balance(account_to_close, commitment=Confirmed)
        balance_value: Optional[Any] = getattr(balance_res, 'value', None) # Get the value part
        balance_amount = 0

        # --- FIX: Check attributes on balance_value ---
        if balance_value and hasattr(balance_value, 'amount') and hasattr(balance_value, 'ui_amount_string'):
            try:
                # The 'amount' attribute is a string representing the raw u64 balance
                balance_amount = int(balance_value.amount)
            except (ValueError, TypeError):
                logger.warning(f"Could not parse balance amount '{getattr(balance_value, 'amount', 'N/A')}' for {account_to_close}. Assuming 0.")
        elif balance_value is not None:
             logger.warning(f"Unexpected balance response structure for {account_to_close}: {balance_value}")
        # If balance_value is None, balance_amount remains 0

        if balance_amount > 0:
             logger.error(f"Cannot close account {account_to_close}: Balance is {balance_amount}. Tokens must be transferred or burned first.")
             return TransactionResult(success=False, error_message="Failed: Non-zero balance", error_type="Balance > 0")
        # --- End Attribute Check Fix ---

        # 3. Build Close Instruction
        logger.info(f"Account {account_to_close} exists with zero balance. Proceeding with closure.")
        close_params = CloseAccountParams(
            account=account_to_close,
            dest=wallet.pubkey, # Send reclaimed SOL to wallet owner
            owner=wallet.pubkey,
            program_id=TOKEN_PROGRAM_ID # Use the imported constant
        )
        ix = close_account(close_params)

        # 4. Send Transaction using the helper function
        logger.info(f"Sending transaction to close ATA: {account_to_close}")
        # Assuming build_and_send_transaction expects the AsyncClient directly
        # or modify the helper function signature / create a temporary wrapper if needed
        tx_result = await build_and_send_transaction(
            client=async_client, # Pass AsyncClient directly
            payer=wallet.payer, # Pass the Keypair for signing
            instructions=[ix],
            label=f"CloseATA_{str(mint)[:5]}",
            confirm_commitment="confirmed" # Pass commitment level
        )

        # 5. Process result from helper
        if tx_result.success and tx_result.signature:
            logger.info(f"Close transaction sent: {tx_result.signature}. Confirming...")
            # Confirmation logic might already be in build_and_send_transaction
            # If not, add confirmation loop here using async_client.confirm_transaction
            # Assuming the helper already confirmed or confirmation isn't strictly needed for this example script
            logger.info(f"Closed successfully: {account_to_close}. Tx: {tx_result.signature}")
        elif tx_result.success and not tx_result.signature:
             logger.warning(f"Close transaction reported success but no signature returned for {account_to_close}.")
        else:
            logger.error(f"Failed to close account {account_to_close}: Type={tx_result.error_type}, Msg={tx_result.error_message}")

        return tx_result

    except SolanaRpcException as e:
         logger.error(f"RPC Error processing account {account_to_close}: {e}")
         return TransactionResult(success=False, error_message=f"RPC Error: {e}", error_type="RPCError")
    except Exception as e:
        logger.error(f"Unexpected error while processing account {account_to_close}: {e}", exc_info=True)
        return TransactionResult(success=False, error_message=f"Unexpected Error: {e}", error_type="Unknown")


async def main():
    logger = get_logger(__name__) # Ensure logger is available

    if not RPC_ENDPOINT or not PRIVATE_KEY:
        logger.critical("Error: SOLANA_NODE_RPC_ENDPOINT and SOLANA_PRIVATE_KEY must be set.")
        return
    if MINT_ADDRESS_TO_CLOSE_STR == "EXAMPLE_MINT_ADDRESS_REPLACE_ME":
         logger.critical("Error: Please update MINT_ADDRESS_TO_CLOSE_STR in the script.")
         return

    try:
        mint_to_close = Pubkey.from_string(MINT_ADDRESS_TO_CLOSE_STR)
        wallet = Wallet(PRIVATE_KEY)
        logger.info(f"Wallet: {wallet.pubkey}")
        logger.info(f"Mint to close ATA for: {mint_to_close}")

        ata_to_close = Wallet.get_associated_token_address(wallet.pubkey, mint_to_close)
        logger.info(f"Derived ATA address: {ata_to_close}")

        # Use standard AsyncClient context manager
        async with AsyncClient(RPC_ENDPOINT) as client:
             result = await close_account_if_exists(client, wallet, ata_to_close, mint_to_close)
             logger.info(f"Closure attempt result: Success={result.success}, Message={result.error_message or 'N/A'}, Signature={result.signature or 'N/A'}")

    except ValueError as e: logger.error(f"Error: Invalid address format - {str(e)}")
    except Exception as e: logger.error(f"Unexpected error in main: {e}", exc_info=True)


if __name__ == "__main__":
    print("Loading .env variables for cleanup script...")
    # Load .env from project root when run standalone
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    load_dotenv(dotenv_path=dotenv_path)
    RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
    # MINT_ADDRESS_TO_CLOSE_STR = os.getenv("MINT_TO_CLOSE", MINT_ADDRESS_TO_CLOSE_STR)

    try: asyncio.run(main())
    except KeyboardInterrupt: print("\nOperation cancelled.")