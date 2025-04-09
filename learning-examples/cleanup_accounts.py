# learning-examples/cleanup_accounts.py (Corrected Imports and Logic)

import asyncio
import os
import sys
from dotenv import load_dotenv
from typing import List, Optional

from solders.pubkey import Pubkey
from solders.instruction import Instruction
from solders.keypair import Keypair
# SPL Token Instructions
from spl.token.instructions import BurnParams, CloseAccountParams, burn, close_account
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAmount # For type hint
from solana.rpc.async_api import AsyncClient # Use standard async client
from solana.exceptions import SolanaRpcException

# --- Add Project Root to Path ---
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# --- End Path ---

# --- Use Absolute Imports from src ---
try:
    # Import the correct class name
    from src.core.pubkeys import SolanaProgramAddresses
    # Import helper functions and classes needed
    from src.core.wallet import Wallet
    from src.core.transactions import build_and_send_transaction, TransactionResult # Import helper
    from src.utils.logger import get_logger
    # Note: We don't need the full SolanaClient wrapper or PriorityFeeManager for this basic script
except ImportError as e:
    print(f"ERROR: Could not import required modules from src: {e}")
    sys.exit(1)
# --- End Imports ---

load_dotenv()
logger = get_logger(__name__)

RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# --- !!! IMPORTANT: Update this address !!! ---
# Replace with a MINT address of a token account YOU OWN and WANT TO CLOSE
MINT_ADDRESS_TO_CLOSE_STR = "9WHpYbqG6LJvfCYfMjvGbyo1wHXgroCrixPb33s2pump" # EXAMPLE ONLY


async def close_account_if_exists(
    async_client: AsyncClient, # Expect standard async client
    wallet: Wallet,           # Expect Wallet object
    account_to_close: Pubkey, # The ATA to close
    mint: Pubkey              # The mint of the ATA
    ):
    """Safely close a token account if it exists and reclaim rent."""
    logger.info(f"Attempting to process account: {account_to_close}")
    try:
        # 1. Check if account exists
        acc_info_res = await async_client.get_account_info(account_to_close, commitment=Confirmed)
        if acc_info_res.value is None:
            logger.info(f"Account does not exist or already closed: {account_to_close}")
            return TransactionResult(success=True, error_message="Skipped: Account does not exist", error_type="Skip")

        # 2. Check balance (optional, closing fails if balance > 0 anyway unless owner burns first)
        balance_res = await async_client.get_token_account_balance(account_to_close, commitment=Confirmed)
        balance_amount = 0
        if balance_res and balance_res.value and hasattr(balance_res.value, 'amount') and balance_res.value.amount.isdigit():
             balance_amount = int(balance_res.value.amount)

        if balance_amount > 0:
             logger.error(f"Cannot close account {account_to_close}: Balance is {balance_amount}. Tokens must be transferred or burned first.")
             # You could add burning logic here if desired, like in the original attempt
             # burn_ix = burn(...)
             # burn_result = await build_and_send_transaction(...)
             # if not burn_result.success: return TransactionResult(...) # Return burn failure
             return TransactionResult(success=False, error_message="Failed: Non-zero balance", error_type="Balance > 0")


        # 3. Build Close Instruction
        logger.info(f"Account {account_to_close} exists with zero balance. Proceeding with closure.")
        close_params = CloseAccountParams(
            account=account_to_close,
            dest=wallet.pubkey, # Send reclaimed SOL to wallet owner
            owner=wallet.pubkey,
            program_id=SolanaProgramAddresses.TOKEN_PROGRAM_ID # Use correct constant
        )
        ix = close_account(close_params)

        # 4. Send Transaction using the helper function
        logger.info(f"Sending transaction to close ATA: {account_to_close}")
        # Pass the async client TO the helper function if it needs it,
        # OR modify the helper if it only needs the RPC endpoint URL.
        # Assuming build_and_send_transaction uses the client passed to it.
        # Need to wrap the async client if build_and_send expects our SolanaClient wrapper
        # For simplicity, let's assume build_and_send can use the AsyncClient directly for now
        # OR modify build_and_send to accept AsyncClient. Let's assume it needs the wrapper:
        # temp_solana_client_wrapper = SolanaClient(RPC_ENDPOINT) # Temporary wrapper if needed by helper
        # temp_solana_client_wrapper._async_client = async_client # Inject async client

        tx_result = await build_and_send_transaction(
            # client=temp_solana_client_wrapper, # Pass wrapper if needed by helper
            client=async_client, # Pass AsyncClient directly if helper accepts it
            payer=wallet.payer, # Pass the Keypair for signing
            instructions=[ix],
            label=f"CloseATA_{str(mint)[:5]}",
            confirm_commitment="confirmed" # Ensure confirmation
        )

        if tx_result.success:
            logger.info(f"Closed successfully: {account_to_close}. Tx: {tx_result.signature}")
        else:
            logger.error(f"Failed to close account {account_to_close}: {tx_result.error_message}")

        return tx_result

    except SolanaRpcException as e:
         logger.error(f"RPC Error processing account {account_to_close}: {e}")
         return TransactionResult(success=False, error_message=f"RPC Error: {e}", error_type="RPCError")
    except Exception as e:
        logger.error(f"Unexpected error while processing account {account_to_close}: {e}", exc_info=True)
        return TransactionResult(success=False, error_message=f"Unexpected Error: {e}", error_type="Unknown")


async def main():
    global logger
    try: from src.utils.logger import get_logger; logger = get_logger(__name__)
    except ImportError: import logging; logging.basicConfig(level=logging.INFO); logger = logging.getLogger(__name__)

    if not RPC_ENDPOINT or not PRIVATE_KEY:
        logger.critical("Error: SOLANA_NODE_RPC_ENDPOINT and SOLANA_PRIVATE_KEY must be set in environment/.env")
        return
    if "EXAMPLE_MINT" in MINT_ADDRESS_TO_CLOSE_STR or "9WHpYbq" in MINT_ADDRESS_TO_CLOSE_STR: # Check if placeholder is still there
         logger.critical("Error: Please update MINT_ADDRESS_TO_CLOSE_STR in the script with the actual mint address of the ATA you want to close.")
         return

    try:
        mint_to_close = Pubkey.from_string(MINT_ADDRESS_TO_CLOSE_STR)
        wallet = Wallet(PRIVATE_KEY)
        logger.info(f"Wallet: {wallet.pubkey}")
        logger.info(f"Mint to close ATA for: {mint_to_close}")

        ata_to_close = Wallet.get_associated_token_address(wallet.pubkey, mint_to_close)
        logger.info(f"Derived ATA address: {ata_to_close}")

        async with AsyncClient(RPC_ENDPOINT) as client: # Use standard async client context
             result = await close_account_if_exists(client, wallet, ata_to_close, mint_to_close)
             logger.info(f"Closure attempt result: Success={result.success}, Message={result.error_message}, Signature={result.signature}")

    except ValueError as e: logger.error(f"Error: Invalid address format - {str(e)}")
    except Exception as e: logger.error(f"Unexpected error in main: {e}", exc_info=True)


if __name__ == "__main__":
    # Load .env for standalone execution
    print("Loading .env variables for cleanup script...")
    load_dotenv()
    RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
    # MINT_ADDRESS_TO_CLOSE_STR = os.getenv("MINT_TO_CLOSE", MINT_ADDRESS_TO_CLOSE_STR) # Optionally override via env

    try: asyncio.run(main())
    except KeyboardInterrupt: print("\nOperation cancelled.")