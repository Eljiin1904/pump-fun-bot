# src/scripts/fetch_bonding_curve.py

import asyncio
import os
import sys
from typing import Optional # Added Optional

# --- Solana/Solders Imports ---
from solders.pubkey import Pubkey
from solana.rpc.commitment import Commitment # Keep if used for client init

# --- Project Imports ---
# Adjust path to run script from project root (e.g., python -m src.scripts.fetch_bonding_curve)
# Or use relative imports if running as part of a larger package execution
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Use relative imports now that path is adjusted or if run via module flag
from src.core.client import SolanaClient # Example path
from src.core.curve import BondingCurveManager, BondingCurveState # Corrected imports
# Removed old decode function import if not needed: from src.core.curve import decode_bonding_curve_account

# --- Configuration ---
RPC_URL = os.getenv("RPC_URL", "YOUR_DEFAULT_RPC_ENDPOINT") # Replace with your RPC endpoint
# Example Bonding Curve Address (Replace with the one you want to fetch)
BONDING_CURVE_ADDRESS = "YOUR_BONDING_CURVE_PUBKEY_HERE"

async def main():
    """Fetches and prints the state of a specific bonding curve."""
    if BONDING_CURVE_ADDRESS == "YOUR_BONDING_CURVE_PUBKEY_HERE":
        print("Please replace 'YOUR_BONDING_CURVE_PUBKEY_HERE' with an actual bonding curve address.")
        return

    print(f"Using RPC Endpoint: {RPC_URL}")
    solana_client = SolanaClient(RPC_URL)
    curve_manager = BondingCurveManager(solana_client) # Instantiate the manager

    try:
        bonding_curve_pubkey = Pubkey.from_string(BONDING_CURVE_ADDRESS)
        print(f"Fetching state for bonding curve: {bonding_curve_pubkey}")

        # Use the manager to get the curve state
        curve_state: Optional[BondingCurveState] = await curve_manager.get_curve_state(bonding_curve_pubkey)

        if curve_state:
            print("\n--- Bonding Curve State ---")
            print(f"Virtual SOL Reserves: {curve_state.virtual_sol_reserves / 1e9:.6f} SOL")
            print(f"Virtual Token Reserves: {curve_state.virtual_token_reserves}") # Adjust based on token decimals if known
            print(f"Real SOL Reserves: {curve_state.real_sol_reserves / 1e9:.6f} SOL")
            print(f"Real Token Reserves: {curve_state.real_token_reserves}") # Adjust based on token decimals if known
            print(f"Mint Address: {curve_state.token_mint}")
            print(f"Complete: {curve_state.complete}")
            # Add other relevant fields from BondingCurveState if needed
        else:
            print("Could not fetch or decode bonding curve state.")

    except ValueError as e:
        print(f"Error: Invalid Pubkey format - {e}")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        await solana_client.close() # Close the underlying HTTPX client
        print("\nConnection closed.")

if __name__ == "__main__":
    asyncio.run(main())