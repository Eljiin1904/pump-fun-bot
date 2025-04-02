import json

import base58
from solana.keypair import Keypair

# Update the file path to point to your keypair file.
KEYPAIR_PATH = "C:/Users/eljin/my_solana_keypair.json"

with open(KEYPAIR_PATH, "r") as f:
    secret = json.load(f)

# Convert the list of integers to bytes.
secret_bytes = bytes(secret)

# Create a Keypair object.
keypair = Keypair.from_secret_key(secret_bytes)

# Encode the secret key to a base58 string.
private_key_b58 = base58.b58encode(keypair.secret_key).decode("utf-8")
print("Your base58-encoded private key is:")
print(private_key_b58)
