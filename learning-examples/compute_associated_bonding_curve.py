# learning-examples/compute_associated_bonding_curve.py

import os
import sys
import asyncio
import base64
from typing import Optional, Tuple

# --- Add Project Root to Path for 'src.' imports ---
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# --- End Path Addition ---

# --- Use Absolute Imports from src ---
try:
    from solders.pubkey import Pubkey
    from solana.rpc.async_api import AsyncClient # Use standard async client
    from solana.exceptions import SolanaRpcException
    # Import necessary components using absolute paths
    from src.core.pubkeys import PumpAddresses, SolanaProgramAddresses # Corrected class name
    from src.core.curve import BONDING_CURVE_LAYOUT, PUMP_FUN_PROGRAM_ID # Import schema and program ID
    from src.utils.logger import get_logger
    # Import borsh/construct types if needed by schema definition (likely handled in curve.py)
    from borsh_construct import CStruct, U8, U64, I64, Bool, Bytes, ConstructError
except ImportError as e:
    print(f"ERROR: Could not import required modules. Check installation and paths.")
    print(f"Import Error: {e}")
    sys.exit(1)
# --- End Absolute Imports ---

logger = get_logger(__name__) # Initialize logger

# --- Configuration ---
RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com")


# --- PDA Calculation ---
def get_bonding_curve_pda(mint: Pubkey) -> tuple[Pubkey, int]:
    """ Derives the bonding curve STATE PDA address for a given mint. """
    seeds = [b"bonding-curve", bytes(mint)]
    pda, bump = Pubkey.find_program_address(seeds, PUMP_FUN_PROGRAM_ID)
    return pda, bump

# --- Function to get SOL Vault (uses deserialization) ---
async def get_sol_vault_address_from_curve_pda(
    rpc_client: AsyncClient,
    bonding_curve_pda: Pubkey
) -> Optional[Pubkey]:
    """
    Fetches the bonding curve PDA data, deserializes it using the LAYOUT
    from src.core.curve, and extracts the SOL vault address.
    """
    logger.debug(f"Fetching account data for curve PDA: {bonding_curve_pda}")
    if BONDING_CURVE_LAYOUT is None: # Check if layout loaded correctly in curve.py
         logger.critical("Cannot deserialize: BONDING_CURVE_LAYOUT schema not defined (check src/core/curve.py).")
         return None
    try:
        res = await rpc_client.get_account_info(bonding_curve_pda, encoding="base64", commitment="confirmed")
        if res.value is None: logger.error(f"Account info not found for {bonding_curve_pda}"); return None
        account_data = res.value.data
        if not isinstance(account_data, list) or len(account_data) < 1: logger.error(f"Unexpected account data format {bonding_curve_pda}"); return None
        raw_data = base64.b64decode(account_data[0]); logger.debug(f"Fetched {len(raw_data)} bytes.")

        try: parsed_data = BONDING_CURVE_LAYOUT.parse(raw_data); logger.debug("Deserialization successful.")
        except Exception as e: logger.error(f"FAIL Deserialize {bonding_curve_pda}. Schema likely incorrect. Err: {e}", exc_info=True); return None

        sol_vault_field_name = "bonding_curve_sol_vault"; # Name must match layout in curve.py
        if not hasattr(parsed_data, sol_vault_field_name): logger.critical(f"Schema missing vault field: '{sol_vault_field_name}'"); return None
        sol_vault_pubkey = getattr(parsed_data, sol_vault_field_name)
        if not isinstance(sol_vault_pubkey, Pubkey): logger.critical(f"Field '{sol_vault_field_name}' not Pubkey."); return None
        logger.info(f"Successfully extracted SOL Vault address: {sol_vault_pubkey}")
        return sol_vault_pubkey

    except SolanaRpcException as e: logger.error(f"RPC Error fetching {bonding_curve_pda}: {e}"); return None
    except Exception as e: logger.error(f"Unexpected error fetching/parsing {bonding_curve_pda}: {e}", exc_info=True); return None


async def main():
    global logger
    try: from src.utils.logger import get_logger; logger = get_logger(__name__)
    except ImportError: import logging; logging.basicConfig(level=logging.INFO); logger = logging.getLogger(__name__)

    if "api.mainnet-beta.solana.com" in RPC_ENDPOINT: logger.warning("Using public RPC endpoint.")

    mint_address_str = input("Enter the token mint address: ")
    try:
        mint = Pubkey.from_string(mint_address_str)
        bonding_curve_pda, bump = get_bonding_curve_pda(mint)
        sol_vault_address = None
        async with AsyncClient(RPC_ENDPOINT) as client:
             sol_vault_address = await get_sol_vault_address_from_curve_pda(client, bonding_curve_pda)

        print("\nResults:"); print("-" * 50)
        print(f"Token Mint:              {mint}")
        print(f"Bonding Curve PDA:       {bonding_curve_pda} (Bump: {bump})")
        if sol_vault_address: print(f"SOL Vault Address:       {sol_vault_address}")
        else: print(f"SOL Vault Address:       FAILED TO RETRIEVE (Check logs/schema)")
        print("-" * 50)

    except ValueError as e: print(f"Error: Invalid address format - {str(e)}")
    except ImportError as e: print(f"Error: Could not import necessary modules. {e}")
    except Exception as e: print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    print("Loading .env variables for script...")
    load_dotenv()
    RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com")
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\nOperation cancelled by user.")