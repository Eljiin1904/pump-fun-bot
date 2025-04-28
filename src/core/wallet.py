# src/core/wallet.py (Adjusted Default & Audit Recommendation)

import base58
from typing import Optional, List
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction # Assuming Transaction is needed for signing
from solders.instruction import Instruction
# --- FIX: Change default RPC ---
DEFAULT_RPC: Optional[str] = None # Set to None to force config
# Or use public mainnet:
# DEFAULT_RPC: str = "https://api.mainnet-beta.solana.com"
# --- END FIX ---

# Conditional import for SolanaClient only if Wallet needs to create one (SHOULD BE AVOIDED)
try:
    from .client import SolanaClient # Use the wrapper
    # REMOVE direct Client/AsyncClient imports if present
    # from solana.rpc.api import Client as SolanaSyncClient
    # from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
except ImportError:
    print("ERROR importing SolanaClient in wallet.py")
    SolanaClient = object # Dummy

# Import necessary constants (using the correct approach)
try:
    from .pubkeys import PumpAddresses
except ImportError:
    print("ERROR importing PumpAddresses in wallet.py")
    PumpAddresses = type('DummyPumpAddr', (object,), {'TOKEN_PROGRAM_ID': None, 'ASSOCIATED_TOKEN_PROGRAM_ID': None})()

# Import SPL token instructions if needed for cleanup
try:
    from spl.token.instructions import close_account, CloseAccountParams
except ImportError:
    print("ERROR importing spl-token instructions in wallet.py")
    close_account = None; CloseAccountParams = None

# Import transaction helper
try:
    from .transactions import build_and_send_transaction # Assuming helper exists
except ImportError:
     print("ERROR importing build_and_send_transaction in wallet.py")
     build_and_send_transaction = None

from ..utils.logger import get_logger
logger = get_logger(__name__)

class Wallet:
    """ Represents the user's wallet with keypair for signing. """
    def __init__(self, private_key_bs58: str):
        try:
            # Decode the base58 private key string
            private_key_bytes: bytes = base58.b58decode(private_key_bs58)
            # Create the Keypair object
            self.keypair = Keypair.from_bytes(private_key_bytes) # Use from_bytes
            self.pubkey = self.keypair.pubkey()
            logger.info(f"Wallet initialized for pubkey: {self.pubkey}")
        except ValueError as e:
            logger.error(f"Invalid base58 private key provided: {e}")
            raise ValueError("Invalid private key format") from e
        except Exception as e:
            logger.error(f"Error initializing Keypair from private key: {e}", exc_info=True)
            raise ValueError("Failed to create Keypair") from e

    @staticmethod
    def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
        """ Computes the associated token account address for a given owner and mint. """
        if PumpAddresses.TOKEN_PROGRAM_ID is None or PumpAddresses.ASSOCIATED_TOKEN_PROGRAM_ID is None:
             raise ValueError("TOKEN_PROGRAM_ID or ASSOCIATED_TOKEN_PROGRAM_ID not available from PumpAddresses.")

        pda, _ = Pubkey.find_program_address(
            [bytes(owner), bytes(PumpAddresses.TOKEN_PROGRAM_ID), bytes(mint)],
            PumpAddresses.ASSOCIATED_TOKEN_PROGRAM_ID
        )
        return pda

    # --- AUDIT POINT ---
    # Review ALL methods in this class. Ensure NONE of them instantiate
    # SolanaSyncClient() or SolanaPyAsyncClient() directly.
    # They should ALL take a `client: SolanaClient` argument if they need to interact
    # with the network.

    # Example: close_token_account SHOULD take client
    async def close_token_account(
        self,
        client: SolanaClient, # <--- TAKES client instance
        token_mint: Pubkey,
        force: bool = False,
        use_priority_fee: bool = True,
        destination_wallet: Optional[Pubkey] = None,
        fee_manager = None # Expect PriorityFeeManager if needed by build_and_send
        ) -> Optional[str]:
        """ Closes the associated token account for the wallet's pubkey and given mint. """

        account_to_close = self.get_associated_token_address(self.pubkey, token_mint)
        destination = destination_wallet or self.pubkey

        logger.info(f"Attempting to close token account: {account_to_close} for owner {self.pubkey}")

        if not force:
            try:
                balance_resp = await client.get_token_account_balance(account_to_close)
                if balance_resp and balance_resp.amount and int(balance_resp.amount) > 0:
                    logger.warning(f"Token account {account_to_close} has balance ({balance_resp.ui_amount_string}). Skipping close (force=False).")
                    return None
            except Exception as e:
                 logger.warning(f"Could not verify token balance for {account_to_close} before closing: {e}")

        try:
            if close_account is None or CloseAccountParams is None or PumpAddresses.TOKEN_PROGRAM_ID is None:
                 logger.error("spl-token close_account or required constants not available.")
                 return None

            ix = close_account(
                 CloseAccountParams(
                     program_id=PumpAddresses.TOKEN_PROGRAM_ID,
                     account=account_to_close,
                     dest=destination,
                     owner=self.pubkey, # Owner is the wallet pubkey
                     signers=[]
                 )
            )

            if build_and_send_transaction is None:
                 logger.error("build_and_send_transaction helper not available.")
                 return None
            if fee_manager is None:
                logger.warning("fee_manager not provided to close_token_account, cannot apply priority fees.")
                # Decide if this should be an error or just proceed without fees

            # Ensure build_and_send takes fee_manager
            tx_result = await build_and_send_transaction(
                client=client, # Use the passed client
                wallet=self, # Pass self (Wallet instance containing keypair)
                instructions=[ix],
                fee_manager=fee_manager, # Pass the fee manager
                use_priority_fee=use_priority_fee,
                skip_preflight=True
            )

            if tx_result and tx_result.success:
                logger.info(f"Successfully closed ATA {account_to_close}. Transaction: {tx_result.signature}")
                return tx_result.signature
            else:
                err_msg = tx_result.error_message if tx_result else "Transaction helper returned None"
                logger.error(f"Failed to close ATA {account_to_close}: {err_msg}")
                return None

        except Exception as e:
            logger.error(f"Error building/sending close account tx for {account_to_close}: {e}", exc_info=True)
            return None

    # --- END AUDIT POINT ---


# --- END OF FILE ---