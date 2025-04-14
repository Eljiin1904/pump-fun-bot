# convert_key.py

import json
import base58
# --- FIX: Import Keypair from solders ---
from solders.keypair import Keypair
# --- End Fix ---

def update_keypair_file(input_path="keypair.json", output_path="updated_keypair.json"):
    """ Reads a keypair, potentially updates format, saves."""
    try:
        with open(input_path, 'r') as f:
            # Assuming the JSON contains a list of numbers (bytes)
            secret_key_list = json.load(f)
            secret_key_bytes = bytes(secret_key_list)

        # Load using the correct Keypair class from solders
        kp = Keypair.from_secret_key(secret_key_bytes)

        print(f"Successfully loaded keypair. Public Key: {kp.pubkey()}")

        # Example: Save back in the same format if needed
        # with open(output_path, 'w') as f:
        #     json.dump(list(kp.secret()), f) # kp.secret() returns bytes
        # print(f"Keypair potentially updated and saved to {output_path}")

    except FileNotFoundError:
        print(f"Error: Input keypair file not found at {input_path}")
    except json.JSONDecodeError:
         print(f"Error: Could not decode JSON from {input_path}")
    except Exception as e:
        print(f"An error occurred: {e}")

# Example usage:
# if __name__ == "__main__":
#    update_keypair_file()