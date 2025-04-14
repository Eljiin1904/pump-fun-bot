# scripts/fetch_bonding_curve.py

import asyncio
import base64 # Keep this for b64decode
import sys
import traceback

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
# Need core for RPCException if still used
from solana.rpc.core import RPCException
from solders.pubkey import Pubkey

# --- Project Imports ---
# --- FIX: Correct imports using absolute path from src ---
# Assuming BondingCurveState and decode_bonding_curve_account are needed
from src.core.curve import BondingCurveState, decode_bonding_curve_account
# BONDING_CURVE_LAYOUT might not be needed if decode function handles length internally
# from src.core.curve import BONDING_CURVE_LAYOUT
# --- End Fix ---

# No need to define EXPECTED_DATA_LENGTH here if decode_bonding_curve_account handles it


async def fetch_raw_account_data(account_address: str, rpc_endpoint: str) -> bytes | None:
    """Fetches raw account data from the Solana blockchain."""
    # Ensure client handles context management or close manually
    client = AsyncClient(rpc_endpoint, commitment=Confirmed)
    pubkey = None
    try:
        print(f"Attempting to fetch account info for: {account_address}")
        pubkey = Pubkey.from_string(account_address)
        # Use base64 encoding which often returns bytes directly now
        resp = await client.get_account_info(pubkey, encoding="base64")

        if resp is None or resp.value is None:
            print(f"Error: No response or value received for account {account_address}.")
            return None

        account_info = resp.value
        if account_info.data is None:
            print(f"Account {account_address} found, but has no data field (lamports: {account_info.lamports}).")
            return None

        data_field = account_info.data
        raw_data: bytes | None = None

        if isinstance(data_field, bytes):
            raw_data = data_field
            print(f"Successfully fetched {len(raw_data)} bytes of raw data (already bytes).")
        elif isinstance(data_field, (list, tuple)) and data_field and isinstance(data_field[0], str):
             print(f"Received data as {type(data_field)}, decoding base64 string.")
             try:
                 raw_data = base64.b64decode(data_field[0])
             # --- FIX: Catch correct base64 error ---
             except base64.binascii.Error as b64e: # Correct exception type
             # --- End Fix ---
                 print(f"Error decoding base64 string from list/tuple: {b64e}")
                 return None
        elif isinstance(data_field, str):
             print("Received data as string, decoding base64 string.")
             try:
                 raw_data = base64.b64decode(data_field)
             # --- FIX: Catch correct base64 error ---
             except base64.binascii.Error as b64e: # Correct exception type
             # --- End Fix ---
                 print(f"Error decoding base64 string: {b64e}")
                 return None
        else:
             print(f"Error: Unexpected data type received for account data: {type(data_field)}")
             return None

        if raw_data:
             print(f"Successfully obtained {len(raw_data)} bytes of raw data.")
        return raw_data

    except RPCException as e: # Keep specific RPC exception handling
        print(f"RPC Error fetching account {account_address} (Pubkey: {pubkey}): {e}")
        return None
    except ValueError as ve:
         print(f"Error creating Pubkey from address '{account_address}': {ve}")
         return None
    except Exception as e:
        print(f"An unexpected error occurred in fetch_raw_account_data: {e}")
        traceback.print_exc()
        return None
    finally:
        # Ensure client is closed
        if client:
             await client.close()
             print("Connection closed.")


# --- Wrapper is no longer needed if decode_bonding_curve_account handles length ---
# def decode_curve_wrapper(raw_data: bytes) -> BondingCurveState | None:
#     ...

async def main():
    if len(sys.argv) < 3:
        print("Usage: python -m scripts.fetch_bonding_curve <account_address> <rpc_endpoint>")
        sys.exit(1)
    account_address = sys.argv[1]
    rpc_endpoint = sys.argv[2]

    print("-" * 20)
    raw_data = await fetch_raw_account_data(account_address, rpc_endpoint)
    print("-" * 20)

    if raw_data:
        print(f"Successfully fetched raw account data ({len(raw_data)} bytes).")
        # print("Raw account data (hex):", raw_data.hex()) # Optional debug

        # Directly call the decoder function from curve.py
        print(f"Attempting to decode {len(raw_data)} bytes using decode_bonding_curve_account...")
        decoded_state = decode_bonding_curve_account(raw_data)

        print("-" * 20)
        if decoded_state:
            print("Decoded Bonding Curve State:")
            print(decoded_state)
            # Example field access:
            # print(f"  Virtual SOL Reserves: {decoded_state.virtual_sol_reserves}")
        else:
            print("Failed to decode bonding curve state.")
            # Reason should be logged by decode_bonding_curve_account
    else:
        print("Failed to fetch account data, account not found, or account has no data.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())