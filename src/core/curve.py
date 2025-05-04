# src/core/curve.py

import asyncio
import base64
import time
import random
from dataclasses import dataclass
from typing import Optional, Tuple, Any, List, Union

from borsh_construct import CStruct, U64
from construct import ConstructError
from solders.pubkey import Pubkey
from solders.account import Account
from solders.commitment_config import CommitmentLevel, CommitmentConfig
from solana.exceptions import SolanaRpcException
from solana.rpc.types import AccountEncoding

from .client import SolanaClient # Import your client wrapper
from src.utils.logger import get_logger

# Initialize logger at module level
logger = get_logger(__name__)

LAMPORTS_PER_SOL = 1000000000
DEFAULT_TOKEN_DECIMALS = 6
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
GLOBAL_ACCOUNT = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQgFGdjCFWXJJWfaKkU")
FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1G9QUkUCzREBiXiC8")
DEFAULT_CURVE_COMMITMENT = CommitmentLevel.Confirmed # Use Solders type

BONDING_CURVE_LAYOUT = CStruct( "virtual_token_reserves" / U64, "virtual_sol_reserves" / U64, "real_token_reserves" / U64, "real_sol_reserves" / U64, "token_total_supply" / U64, "complete_raw" / U64 )
EXPECTED_CURVE_DATA_SIZE = BONDING_CURVE_LAYOUT.sizeof() # Should be 48

@dataclass
class BondingCurveState:
    virtual_token_reserves: int; virtual_sol_reserves: int; real_token_reserves: int
    real_sol_reserves: int; token_total_supply: int; complete_raw: int
    @property
    def complete(self) -> bool: return self.complete_raw != 0
    def calculate_price(self, decimals: int = DEFAULT_TOKEN_DECIMALS) -> float:
        if self.virtual_token_reserves == 0: return 0.0
        price_lamports_per_base_unit = (self.virtual_sol_reserves * 2) / self.virtual_token_reserves if self.virtual_token_reserves else 0
        price_sol_per_ui_token = (price_lamports_per_base_unit / LAMPORTS_PER_SOL) * (10 ** decimals); return price_sol_per_ui_token
    def calculate_tokens_out_for_sol(self, sol_in_lamports: int) -> int:
        if sol_in_lamports <= 0: return 0; denominator = self.virtual_sol_reserves + sol_in_lamports
        if denominator == 0: return 0; tokens_out = int((sol_in_lamports * self.virtual_token_reserves) / denominator); return max(0, tokens_out)
    def calculate_sol_out_for_tokens(self, tokens_in: int) -> int:
        if tokens_in <= 0: return 0; denominator = self.virtual_token_reserves + tokens_in
        if denominator == 0: return 0; sol_out = int((tokens_in * self.virtual_sol_reserves) / denominator); return max(0, sol_out)

def decode_bonding_curve_account(raw_data: bytes) -> Optional[BondingCurveState]:
    """Decodes raw bonding curve data, handling 48/49 byte cases."""
    expected_size = EXPECTED_CURVE_DATA_SIZE; data_to_parse: Optional[bytes] = None; data_len = len(raw_data)
    if data_len == expected_size: data_to_parse = raw_data; logger.debug(f"Received expected {expected_size} bytes.")
    elif data_len == expected_size + 1: logger.debug(f"Received {expected_size + 1} bytes. Stripping first byte."); data_to_parse = raw_data[1:]
    elif data_len == 256: logger.error(f"Received 256 bytes. Cannot decode."); return None
    else: logger.error(f"Unexpected data length: {data_len} (expected {expected_size} or {expected_size + 1})."); return None
    if data_to_parse:
        try:
            parsed = BONDING_CURVE_LAYOUT.parse(data_to_parse)
            if any(val < 0 for val in [parsed.virtual_token_reserves, parsed.virtual_sol_reserves, parsed.real_token_reserves, parsed.real_sol_reserves, parsed.token_total_supply]): logger.error(f"Decoded curve state contains negative values: {parsed}"); return None
            return BondingCurveState( virtual_token_reserves=parsed.virtual_token_reserves, virtual_sol_reserves=parsed.virtual_sol_reserves, real_token_reserves=parsed.real_token_reserves, real_sol_reserves=parsed.real_sol_reserves, token_total_supply=parsed.token_total_supply, complete_raw=parsed.complete_raw )
        except Exception as e: logger.error(f"Error decoding curve data (len {len(data_to_parse)}): {e}", exc_info=True); return None
    return None

class BondingCurveManager:
    # Class-level constants for retry logic
    FETCH_STATE_MAX_RETRIES = 5
    FETCH_STATE_INITIAL_DELAY = 0.5
    FETCH_STATE_BACKOFF_FACTOR = 1.5
    FETCH_STATE_MAX_DELAY = 8.0

    def __init__(self, client: SolanaClient):
        self.client = client
        self.logger = logger # Use module logger

    async def get_curve_state( self, account_pubkey: Pubkey, commitment: Optional[CommitmentLevel] = None ) -> Optional[BondingCurveState]:
        """Fetches, decodes, and retries getting the bonding curve state."""
        # --- FIX: Access class constants via class name ---
        current_delay = BondingCurveManager.FETCH_STATE_INITIAL_DELAY
        # --- END FIX ---
        last_exception: Optional[Exception] = None
        fetch_commitment = commitment if commitment is not None else DEFAULT_CURVE_COMMITMENT

        # --- FIX: Access class constant via class name ---
        for attempt in range(1, BondingCurveManager.FETCH_STATE_MAX_RETRIES + 1):
            # --- FIX: Log commitment using str() ---
            self.logger.debug(f"Fetching curve state for {account_pubkey} (Attempt {attempt}/{BondingCurveManager.FETCH_STATE_MAX_RETRIES}, Commitment: {str(fetch_commitment)})")
            # --- END FIX ---
            try:
                # --- FIX: Use correct encoding value ---
                # AccountEncoding uses strings 'base64', 'base64+zstd', 'jsonParsed' etc.
                # It does NOT have attributes like AccountEncoding.Base64
                resp_value: Optional[Any] = await self.client.get_account_info(
                    account_pubkey,
                    encoding='base64', # Pass the string 'base64'
                    commitment=fetch_commitment
                )
                # --- END FIX ---

                if resp_value is None: self.logger.warning(f"Curve account {account_pubkey} not found (Attempt {attempt}). Stopping."); return None

                raw_data_field = getattr(resp_value, 'data', None)
                if raw_data_field is None:
                    last_exception = ValueError("Account found but no data field.")
                    self.logger.warning(f"{last_exception} Response: {resp_value}")
                    raise last_exception
                else: # Process data
                    raw_data: Optional[bytes] = None
                    if isinstance(raw_data_field, bytes): raw_data = raw_data_field
                    elif isinstance(raw_data_field, str):
                        try: raw_data = base64.b64decode(raw_data_field); logger.debug("Decoded base64 string.")
                        except Exception as b64e: last_exception = ValueError(f"B64 decode error (str): {b64e}"); raise last_exception
                    elif isinstance(raw_data_field, (list, tuple)) and len(raw_data_field) >= 1 and isinstance(raw_data_field[0], str):
                        try: raw_data = base64.b64decode(raw_data_field[0]); logger.debug("Decoded base64 list.")
                        except Exception as b64e: last_exception = ValueError(f"B64 decode error (list): {b64e}"); raise last_exception
                    else: last_exception = ValueError(f"Cannot extract data. Type: {type(raw_data_field)}"); raise last_exception

                    if raw_data:
                        state = decode_bonding_curve_account(raw_data)
                        if state is None: self.logger.error(f"Failed decode curve {account_pubkey}. Stopping."); return None
                        else: self.logger.debug(f"Success fetch/decode {account_pubkey}"); return state
                    else: last_exception = ValueError("Raw data None after extraction."); raise last_exception

            except Exception as e: # Catch exceptions raised within the try or from RPC calls
                last_exception = e
                # Check if the specific error is the AccountEncoding attribute error
                if isinstance(e, AttributeError) and "AccountEncoding" in str(e):
                    self.logger.critical(f"FATAL: Incorrect AccountEncoding used in get_curve_state call: {e}. Check curve.py line 90.")
                    # This is likely unrecoverable without code change, stop retrying
                    return None
                else:
                    self.logger.warning(f"Error curve fetch attempt {attempt} for {account_pubkey}: {e}", exc_info=True)


            # Retry Logic (only if not returned successfully or stopped)
            # --- FIX: Access class constants via class name ---
            if attempt < BondingCurveManager.FETCH_STATE_MAX_RETRIES:
                wait_time = min(current_delay, BondingCurveManager.FETCH_STATE_MAX_DELAY)
                jitter = wait_time * 0.1 * (random.random() * 2 - 1); actual_wait = max(0.2, wait_time + jitter)
                self.logger.info(f"Retrying curve fetch {account_pubkey} in {actual_wait:.2f}s...")
                await asyncio.sleep(actual_wait)
                current_delay *= BondingCurveManager.FETCH_STATE_BACKOFF_FACTOR
            else: # Max retries reached
                self.logger.error(f"Curve fetch failed {account_pubkey} after {BondingCurveManager.FETCH_STATE_MAX_RETRIES} attempts. Last error: {last_exception}"); return None
            # --- END FIX ---

        self.logger.error(f"Exited get_curve_state loop unexpectedly {account_pubkey}. Last exception: {last_exception}"); return None

    # Static methods remain the same
    @staticmethod
    def find_bonding_curve_pda(mint_pubkey: Pubkey) -> Tuple[Pubkey, int]: seeds = [b"bonding-curve", bytes(mint_pubkey)]; return Pubkey.find_program_address(seeds, PUMP_FUN_PROGRAM_ID)
    @staticmethod
    def find_associated_bonding_curve_pda(mint_pubkey: Pubkey) -> Tuple[Pubkey, int]: seeds = [b"token", bytes(mint_pubkey)]; return Pubkey.find_program_address(seeds, PUMP_FUN_PROGRAM_ID)