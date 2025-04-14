# src/core/curve.py

import asyncio
import base64
import time
import random # Import random for jitter
from dataclasses import dataclass
from typing import Optional, Tuple, Any

# --- Solana/Borsh Imports ---
from borsh_construct import CStruct, U64
# --- FIX: Add ConstructError import ---
from construct import ConstructError # Import the exception class
# --- End Fix ---
from solders.pubkey import Pubkey
from solana.rpc.commitment import Commitment, Confirmed
from solana.exceptions import SolanaRpcException

# --- Project Imports (Corrected) ---
from src.core.pubkeys import PUMP_FUN_PROGRAM_ID
from src.utils.logger import get_logger

# --- Standard constants ---
LAMPORTS_PER_SOL = 1000000000
DEFAULT_TOKEN_DECIMALS = 6

# --- Bonding Curve Layout ---
BONDING_CURVE_LAYOUT = CStruct(
    "virtual_token_reserves" / U64,
    "virtual_sol_reserves" / U64,
    "real_token_reserves" / U64,
    "real_sol_reserves" / U64,
    "token_total_supply" / U64,
    "complete_raw" / U64
)

# --- Bonding Curve State Dataclass ---
@dataclass
class BondingCurveState:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete_raw: int

    @property
    def complete(self) -> bool: return self.complete_raw != 0
    def calculate_price(self, decimals: int = DEFAULT_TOKEN_DECIMALS) -> float:
        if self.virtual_token_reserves == 0: return 0.0
        price_lamports_per_unit = (2 * self.virtual_sol_reserves) / self.virtual_token_reserves
        price_sol_per_ui_token = (price_lamports_per_unit / LAMPORTS_PER_SOL) * (10 ** decimals)
        return price_sol_per_ui_token
    def calculate_tokens_out_for_sol(self, sol_in_lamports: int) -> int:
        if sol_in_lamports <= 0: return 0
        if self.virtual_sol_reserves + sol_in_lamports == 0: return 0
        tokens_out = int((sol_in_lamports * self.virtual_token_reserves) // (self.virtual_sol_reserves + sol_in_lamports))
        return tokens_out
    def calculate_sol_out_for_tokens(self, tokens_in: int) -> int:
        if tokens_in <= 0: return 0
        if self.virtual_token_reserves + tokens_in == 0: return 0
        sol_out = int((tokens_in * self.virtual_sol_reserves) // (self.virtual_token_reserves + tokens_in))
        return sol_out

# --- Standalone Decoder Function ---
# In src/core/curve.py

# --- Standalone Decoder Function ---
def decode_bonding_curve_account(raw_data: bytes) -> Optional[BondingCurveState]:
    """Decodes raw bonding curve data, handling 48/49 byte cases."""
    expected_size = BONDING_CURVE_LAYOUT.sizeof() # Should be 48
    logger = get_logger(__name__)
    data_to_parse: Optional[bytes] = None

    if len(raw_data) == expected_size:
        data_to_parse = raw_data
    elif len(raw_data) == expected_size + 1:
        logger.debug("Received 49 bytes. Stripping first byte.")
        data_to_parse = raw_data[1:]
    else:
        logger.error(f"Unexpected account data length: {len(raw_data)} (expected {expected_size} or {expected_size + 1}).")
        return None

    if data_to_parse:
        try:
            # Parse the data using the layout
            parsed = BONDING_CURVE_LAYOUT.parse(data_to_parse)

            # --- FIX: Initialize BondingCurveState using direct attribute access ---
            required_fields = ['virtual_token_reserves', 'virtual_sol_reserves', 'real_token_reserves', 'real_sol_reserves', 'token_total_supply', 'complete_raw']
            if all(hasattr(parsed, field) for field in required_fields):
                return BondingCurveState(
                    virtual_token_reserves=parsed.virtual_token_reserves,
                    virtual_sol_reserves=parsed.virtual_sol_reserves,
                    real_token_reserves=parsed.real_token_reserves,
                    real_sol_reserves=parsed.real_sol_reserves,
                    token_total_supply=parsed.token_total_supply,
                    complete_raw=parsed.complete_raw
                )
            else:
                missing = [f for f in required_fields if not hasattr(parsed, f)]
                logger.error(f"Parsed object missing required fields for BondingCurveState: {missing}. Parsed object dir: {dir(parsed)}")
                return None
            # --- End Fix ---
        except ConstructError as e: # Catch specific parsing errors
             logger.error(f"Borsh construct error decoding bonding curve data (length {len(data_to_parse)}): {e}", exc_info=False)
             return None
        except Exception as e: # Catch other potential errors during parsing/init
            logger.error(f"Unexpected error decoding bonding curve data (length {len(data_to_parse)}): {e}", exc_info=True)
            return None
    return None

# --- Keep the rest of curve.py (Dataclass, Manager, Constants) as is ---

# --- BondingCurveManager class and its methods remain unchanged from the previous correct version ---
# (Including the exponential backoff logic in get_curve_state)
class BondingCurveManager:
    """ Manager for the bonding curve. Handles fetching/decoding state and computing PDAs. """
    FETCH_STATE_MAX_RETRIES = 5; FETCH_STATE_INITIAL_DELAY = 1.0
    FETCH_STATE_BACKOFF_FACTOR = 1.8; FETCH_STATE_MAX_DELAY = 12.0

    def __init__(self, client): self.client = client; self.logger = get_logger(__name__)

    async def get_curve_state( self, account_pubkey: Pubkey, commitment: Optional[Commitment] = Confirmed ) -> Optional[BondingCurveState]:
        current_delay = self.FETCH_STATE_INITIAL_DELAY; last_exception: Optional[Exception] = None
        for attempt in range(1, self.FETCH_STATE_MAX_RETRIES + 1):
            self.logger.debug(f"Fetching curve state for {account_pubkey} (Attempt {attempt}/{self.FETCH_STATE_MAX_RETRIES})")
            try:
                resp_value: Optional[Any] = await self.client.get_account_info( account_pubkey, encoding="base64", commitment=commitment )
                if resp_value is None:
                    self.logger.warning(f"Curve state fetch {attempt}: Account {account_pubkey} not found or RPC error occurred.")
                    last_exception = None
                    if attempt < self.FETCH_STATE_MAX_RETRIES:
                         wait_time = min(current_delay, self.FETCH_STATE_MAX_DELAY); jitter = wait_time * 0.2 * (random.random() * 2 - 1)
                         actual_wait = max(0.1, wait_time + jitter); self.logger.info(f"Retrying curve fetch for {account_pubkey} in {actual_wait:.2f}s...")
                         await asyncio.sleep(actual_wait); current_delay *= self.FETCH_STATE_BACKOFF_FACTOR; continue
                    else: self.logger.error(f"Curve state fetch failed for {account_pubkey} after {self.FETCH_STATE_MAX_RETRIES} attempts."); return None
                data_field = getattr(resp_value, 'data', None); raw_data: Optional[bytes] = None
                if isinstance(data_field, bytes): raw_data = data_field
                elif isinstance(data_field, (list, tuple)) and data_field and isinstance(data_field[0], str):
                     try: raw_data = base64.b64decode(data_field[0])
                     except base64.binascii.Error as b64e: self.logger.error(f"Base64 decode error (list) for {account_pubkey}: {b64e}"); return None
                elif isinstance(data_field, str):
                     try: raw_data = base64.b64decode(data_field)
                     except base64.binascii.Error as b64e: self.logger.error(f"Base64 decode error (str) for {account_pubkey}: {b64e}"); return None
                elif data_field is None: self.logger.warning(f"Account {account_pubkey} exists but has no data field."); return None
                else: self.logger.error(f"Unexpected data type in bonding curve account {account_pubkey}: {type(data_field)}"); return None
                if raw_data:
                    state = decode_bonding_curve_account(raw_data)
                    if state is None: self.logger.error(f"Failed decode bonding curve {account_pubkey} (Len: {len(raw_data)})."); return None
                    else: self.logger.debug(f"Successfully fetched/decoded state {account_pubkey}"); return state
                else: self.logger.error(f"Raw data processing failed unexpectedly for {account_pubkey}"); return None
            except asyncio.TimeoutError:
                 self.logger.warning(f"Curve state fetch {attempt} timed out for {account_pubkey}."); last_exception = asyncio.TimeoutError("Fetch timed out")
                 if attempt < self.FETCH_STATE_MAX_RETRIES: wait_time = min(current_delay, self.FETCH_STATE_MAX_DELAY); jitter = wait_time * 0.2 * (random.random() * 2 - 1); actual_wait = max(0.1, wait_time + jitter); self.logger.info(f"Retrying curve fetch after timeout for {account_pubkey} in {actual_wait:.2f}s..."); await asyncio.sleep(actual_wait); current_delay *= self.FETCH_STATE_BACKOFF_FACTOR
                 else: self.logger.error(f"Curve state fetch timed out after {self.FETCH_STATE_MAX_RETRIES} attempts for {account_pubkey}."); return None
            except Exception as e: self.logger.error(f"Unexpected error in get_curve_state for {account_pubkey} (Attempt {attempt}): {e}", exc_info=True); return None
        self.logger.error(f"Exited get_curve_state loop unexpectedly for {account_pubkey}. Last exception: {last_exception}"); return None

    @staticmethod
    def find_bonding_curve_pda(mint_pubkey: Pubkey) -> Tuple[Pubkey, int]:
        seeds = [b"bonding-curve", bytes(mint_pubkey)]; program_id = PUMP_FUN_PROGRAM_ID
        return Pubkey.find_program_address(seeds, program_id)

    @staticmethod
    def find_associated_bonding_curve_pda(mint_pubkey: Pubkey) -> Tuple[Pubkey, int]:
        seeds = [b"token", bytes(mint_pubkey)]; program_id = PUMP_FUN_PROGRAM_ID
        return Pubkey.find_program_address(seeds, program_id)