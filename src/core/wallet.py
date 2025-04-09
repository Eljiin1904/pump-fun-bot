# src/core/wallet.py
import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash # If needed for message signing
from solders.message import Message # If needed for message signing
from solders.signature import Signature # If needed for message signing
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID

class Wallet:
    """Represents a Solana wallet."""

    def __init__(self, private_key_base58: str):
        """
        Initializes the wallet from a Base58 encoded private key.
        """
        try:
            # Decode the full 64-byte key from Base58
            private_key_bytes = base58.b58decode(private_key_base58)
            if len(private_key_bytes) != 64:
                 # Sometimes the export might just be the 32-byte secret key part
                 if len(private_key_bytes) == 32:
                      # Reconstruct the 64-byte keypair representation if needed,
                      # or use Keypair.from_secret_key() if the library supports it.
                      # Using Keypair.from_bytes assumes 64 bytes.
                      # Let's try from_secret_key assuming standard export.
                      self._keypair = Keypair.from_secret_key(private_key_bytes)
                 else:
                      raise ValueError("Invalid private key length. Expected 32 or 64 bytes.")
            else:
                 # If it's 64 bytes, use from_bytes
                 self._keypair = Keypair.from_bytes(private_key_bytes)

        except ValueError as e:
            raise ValueError(f"Invalid Base58 private key: {e}") from e

        self._pubkey = self._keypair.pubkey()

    @property
    def keypair(self) -> Keypair:
        """Returns the underlying Keypair object."""
        return self._keypair

    # --- ADDED PAYER PROPERTY ---
    @property
    def payer(self) -> Keypair:
         """Returns the Keypair object, used for signing transactions."""
         return self._keypair
    # --- END ADDED ---

    @property
    def pubkey(self) -> Pubkey:
        """Returns the public key of the wallet."""
        return self._pubkey

    def sign_message(self, message: bytes) -> Signature:
         """Signs a message using the wallet's private key."""
         # Assuming message is already serialized correctly
         return self._keypair.sign_message(message)

    @staticmethod
    def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
        """Calculates the associated token address for a given owner and mint."""
        address, _ = Pubkey.find_program_address(
            [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
            ASSOCIATED_TOKEN_PROGRAM_ID
        )
        return address

    # Add other utility methods if needed, e.g., sign_transaction