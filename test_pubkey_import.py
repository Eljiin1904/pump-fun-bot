# test_pubkey_import.py
import sys
import os
import base58 # Import the pure python library

# Ensure src is in the path for this test script (optional here, but good practice)
# ... (sys.path modification code can be kept or removed for this test) ...

print("--- Starting Test ---")
try:
    # Import Pubkey directly
    from solders.pubkey import Pubkey
    print("solders.pubkey.Pubkey imported successfully.")

    # Test the failing string
    failing_string = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsL"
    print(f"\nTesting string: '{failing_string}' (Length: {len(failing_string)})")

    # --- Test with pure Python base58 ---
    print(f"\nAttempting base58.b58decode('{failing_string}')...")
    try:
        decoded_bytes = base58.b58decode(failing_string)
        print(f"  base58 SUCCESS. Decoded length: {len(decoded_bytes)}")
        if len(decoded_bytes) != 32:
            print(f"  !!! WARNING: Decoded length is NOT 32 bytes using pure Python base58!")
        else:
            print(f"  (Correct length of 32 bytes confirmed)")
    except Exception as b58_e:
        print(f"  !!! FAILED decoding with base58 library: {b58_e}")
    # --- End pure Python test ---

    # --- Test with solders ---
    print(f"\nAttempting solders.pubkey.Pubkey.from_string('{failing_string}')...")
    pk1 = Pubkey.from_string(failing_string)
    print(f"  solders SUCCESS: {pk1}")
    # --- End solders test ---

    # Test the string that worked previously (optional now)
    # working_string = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    # print(f"\nAttempting Pubkey.from_string('{working_string}')...")
    # pk2 = Pubkey.from_string(working_string)
    # print(f"  SUCCESS: {pk2}")

    print("\n--- Test Finished (Reached End) ---")

except ValueError as ve:
    print(f"\n!!! ValueError encountered (likely from solders): {ve}")
    print("--- Test Failed (ValueError) ---")
    import traceback
    traceback.print_exc()
except ImportError as ie:
    print(f"\n!!! ImportError encountered: {ie}")
    print("--- Test Failed (ImportError) ---")
except Exception as e:
    print(f"\n!!! Unexpected Error encountered: {type(e).__name__} - {e}")
    print("--- Test Failed (Other Error) ---")
    import traceback
    traceback.print_exc()

input("\nPress Enter to exit...")