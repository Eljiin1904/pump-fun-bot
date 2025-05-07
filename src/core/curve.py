# src/core/curve.py

import asyncio
import base64
import time
import random  # Import random for jitter
from dataclasses import dataclass
from typing import Optional, Tuple, Any

# --- Solana/Borsh Imports ---
from borsh_construct import CStruct, U64
# --- FIX: Add ConstructError import ---
from construct import ConstructError  # Import the exception class
# --- End Fix ---
from solders.pubkey import Pubkey
from solana.rpc.commitment import Commitment, Confirmed
from solana.exceptions import SolanaRpcException

# --- Project Imports (Corrected) ---
# --- FIX: Import the class, not the non-existent constant ---
from src.core.pubkeys import PumpAddresses  # Import the class containing program IDs
# --- END FIX ---
from src.utils.logger import get_logger

# --- Standard constants ---
LAMPORTS_PER_SOL = 1000000000
DEFAULT_TOKEN_DECIMALS = 6

# --- Bonding Curve Layout ---
# This layout defines the 48-byte core structure of the bonding curve state.
BONDING_CURVE_LAYOUT = CStruct(
    "virtual_token_reserves" / U64,
    "virtual_sol_reserves" / U64,
    "real_token_reserves" / U64,
    "real_sol_reserves" / U64,
    "token_total_supply" / U64,
    "complete_raw" / U64  # Using _raw suffix to avoid conflict with property
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
    def complete(self) -> bool:
        """Returns True if the bonding curve process is complete."""
        return self.complete_raw != 0

    def calculate_price(self, decimals: int = DEFAULT_TOKEN_DECIMALS) -> float:
        """Calculates the current instantaneous price in SOL per UI token."""
        if self.virtual_token_reserves == 0 or self.virtual_sol_reserves == 0:
            return 0.0
        try:
            # Price per token lamport = virtual_sol_reserves / virtual_token_reserves
            price_lamports_per_token_lamport = self.virtual_sol_reserves / self.virtual_token_reserves
            # Convert to price per whole UI token in SOL
            price_sol_per_ui_token = (price_lamports_per_token_lamport / LAMPORTS_PER_SOL) * (10 ** decimals)
            return price_sol_per_ui_token
        except ZeroDivisionError:
            return 0.0

    def calculate_tokens_out_for_sol(self, sol_in_lamports: int) -> int:
        """
        Calculates token amount received for a given SOL input.
        NOTE: This is a placeholder. The actual pump.fun buy formula is complex and
        depends on the specific curve implementation. The transaction itself uses the
        on-chain program for the true calculation. This method is for off-chain estimation.
        """
        logger = get_logger(__name__)  # Get logger instance for this method
        if sol_in_lamports <= 0: return 0
        if self.virtual_sol_reserves == 0:  # Cannot buy if no virtual SOL reserves
            logger.warning("Attempted to calculate tokens out with zero virtual SOL reserves.")
            return 0

        # The pump.fun buy quote is often:
        # tokens_out = floor(virtual_token_reserves * sol_in_lamports / virtual_sol_reserves)
        # This is an approximation and might not be accurate for all curve states or amounts.
        # For a more precise estimation, one might need to replicate the on-chain math.
        # For now, returning 0 as per your provided code to indicate it needs a proper formula.
        logger.warning(
            "calculate_tokens_out_for_sol in BondingCurveState needs the precise pump.fun buy formula or use TokenBuyer's estimation.")
        return 0

    def calculate_sol_out_for_tokens(self, tokens_in: int) -> int:
        """Calculates SOL received for selling tokens using pump.fun bonding curve formula."""
        if tokens_in <= 0: return 0
        if self.virtual_token_reserves == 0 and self.real_token_reserves == 0:  # Cannot sell if no tokens in curve
            return 0
        if self.virtual_token_reserves + tokens_in == 0:  # Avoid division by zero
            return 0

        # Pump.fun actual sell quote: sol_out = floor(virtual_sol_reserves * tokens_in / (virtual_token_reserves + tokens_in))
        try:
            # Ensure all values are integers for integer arithmetic as done on-chain
            sol_out = int((self.virtual_sol_reserves * tokens_in) // (self.virtual_token_reserves + tokens_in))
            return sol_out
        except ZeroDivisionError:
            return 0


# --- Standalone Decoder Function ---
def decode_bonding_curve_account(raw_data: bytes) -> Optional[BondingCurveState]:
    """Decodes raw bonding curve data, handling potential discriminator/padding."""
    expected_layout_size = BONDING_CURVE_LAYOUT.sizeof()  # Should be 48 for 6*U64
    logger = get_logger(__name__)
    data_to_parse: Optional[bytes] = None

    data_len = len(raw_data)

    if data_len == expected_layout_size:  # e.g., 48 bytes
        data_to_parse = raw_data
        logger.debug("Decoding bonding curve data with exact expected length %d.", expected_layout_size)
    elif data_len == expected_layout_size + 1:  # e.g., 49 bytes (extra leading/padding byte)
        logger.warning("Received %d bytes (expected %d). Assuming extra leading byte, stripping.", data_len,
                       expected_layout_size)
        data_to_parse = raw_data[1:]  # Try stripping first byte
        if len(data_to_parse) != expected_layout_size:  # If still not matching, try stripping last
            data_to_parse = raw_data[:-1]
            if len(data_to_parse) != expected_layout_size:  # If still not matching, problematic
                logger.error(f"Stripping 1 byte from {data_len} did not result in {expected_layout_size} bytes.")
                data_to_parse = None  # Reset
    elif data_len == expected_layout_size + 8:  # e.g., 56 bytes (48 + 8-byte discriminator)
        logger.debug("Received %d bytes. Assuming 8-byte discriminator, stripping.", data_len)
        data_to_parse = raw_data[8:]
    elif data_len == expected_layout_size + 9:  # e.g., 57 bytes (48 + 8-byte discriminator + 1 padding byte)
        logger.warning("Received %d bytes. Assuming 8-byte discriminator and 1 padding byte, stripping.", data_len)
        data_to_parse = raw_data[9:]  # Strip 8-byte disc + 1-byte padding
    else:
        # This path will be taken if raw_data is 256 bytes and expected_layout_size is 48.
        logger.error(
            f"Unexpected bonding curve account data length: {data_len} (expected layout size {expected_layout_size}, possibly with discriminator/padding).")
        return None

    if data_to_parse and len(data_to_parse) == expected_layout_size:
        try:
            parsed = BONDING_CURVE_LAYOUT.parse(data_to_parse)
            required_fields = ['virtual_token_reserves', 'virtual_sol_reserves', 'real_token_reserves',
                               'real_sol_reserves', 'token_total_supply', 'complete_raw']
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
                logger.error(f"Parsed object missing required fields for BondingCurveState: {missing}.")
                return None
        except ConstructError as e:
            logger.error(
                f"Borsh construct error decoding bonding curve data (length {len(data_to_parse)} after stripping): {e}",
                exc_info=False)
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error decoding bonding curve data (length {len(data_to_parse)} after stripping): {e}",
                exc_info=True)
            return None
    elif data_to_parse:  # data_to_parse exists but length is not expected_layout_size after attempted stripping
        logger.error(
            f"Data to parse has unexpected length {len(data_to_parse)} after stripping, expected {expected_layout_size}.")
        return None

    return None


# --- BondingCurveManager class ---
class BondingCurveManager:
    FETCH_STATE_MAX_RETRIES = 5
    FETCH_STATE_INITIAL_DELAY = 0.5
    FETCH_STATE_BACKOFF_FACTOR = 1.5
    FETCH_STATE_MAX_DELAY = 5.0

    def __init__(self, client):
        self.client = client  # Expects SolanaClient (your wrapper)
        self.logger = get_logger(__name__)

    async def get_curve_state(self, account_pubkey: Pubkey, commitment: Optional[Commitment] = Confirmed) -> Optional[
        BondingCurveState]:
        current_delay = self.FETCH_STATE_INITIAL_DELAY
        last_exception: Optional[Exception] = None
        for attempt in range(1, self.FETCH_STATE_MAX_RETRIES + 1):
            self.logger.debug(
                f"Fetching curve state for {account_pubkey} (Attempt {attempt}/{self.FETCH_STATE_MAX_RETRIES})")
            try:
                resp = await self.client.get_account_info_json_parsed(
                    # Using json_parsed for simplicity if schema is known
                    account_pubkey,
                    commitment=commitment
                )
                # If using get_account_info_json_parsed, resp.value.data will be a dict or list.
                # For raw bytes with base64, use:
                # resp = await self.client.get_account_info(
                # account_pubkey,
                # encoding="base64", # Or "base64+zstd" if applicable
                # commitment=commitment
                # )

                if resp is None or resp.value is None:
                    self.logger.warning(
                        f"Curve state fetch {attempt}: Account {account_pubkey} not found or RPC error (no value). Response: {resp}")
                    last_exception = ValueError("Account not found or no value in response")
                elif not hasattr(resp.value, 'data'):
                    self.logger.warning(
                        f"Curve state fetch {attempt}: Response for {account_pubkey} has no 'data' attribute. Response value: {resp.value}")
                    last_exception = ValueError("Response value has no 'data' attribute")
                else:
                    account_data_field = resp.value.data
                    raw_data: Optional[bytes] = None

                    if isinstance(account_data_field, (list, tuple)) and len(account_data_field) > 0 and isinstance(
                            account_data_field[0], str):
                        # This is typical for "encoding": "base64"
                        try:
                            raw_data = base64.b64decode(account_data_field[0])
                        except (base64.binascii.Error, TypeError) as b64e:
                            self.logger.error(f"Base64 decode error (list/tuple) for {account_pubkey}: {b64e}")
                            return None  # Fatal error for this attempt
                    elif isinstance(account_data_field, str):  # Direct base64 string
                        try:
                            raw_data = base64.b64decode(account_data_field)
                        except (base64.binascii.Error, TypeError) as b64e:
                            self.logger.error(f"Base64 decode error (str) for {account_pubkey}: {b64e}")
                            return None  # Fatal error for this attempt
                    elif isinstance(account_data_field, bytes):  # Already bytes
                        raw_data = account_data_field
                    else:
                        self.logger.error(
                            f"Unexpected data type in bonding curve account {account_pubkey}.data: {type(account_data_field)}. Full response value: {resp.value}")
                        return None  # Fatal error for this attempt

                    if raw_data:
                        state = decode_bonding_curve_account(raw_data)
                        if state is None:
                            self.logger.error(
                                f"Failed to decode bonding curve data for {account_pubkey} (Raw Len: {len(raw_data)}). Stopping retries for this account.")
                            return None  # Don't retry on decode failure, data is likely malformed for current layout.
                        else:
                            self.logger.debug(f"Successfully fetched/decoded state for {account_pubkey}: {state}")
                            return state
                    else:
                        self.logger.warning(f"Account {account_pubkey} has an empty or non-decodable data field.")
                        last_exception = ValueError("Account data field is empty or non-decodable")

            except SolanaRpcException as e:
                self.logger.warning(f"RPC Exception fetching curve state {account_pubkey} (Attempt {attempt}): {e}")
                last_exception = e
            except asyncio.TimeoutError:
                self.logger.warning(f"Curve state fetch {attempt} timed out for {account_pubkey}.")
                last_exception = asyncio.TimeoutError("Fetch timed out")
            except Exception as e:
                self.logger.error(f"Unexpected error in get_curve_state for {account_pubkey} (Attempt {attempt}): {e}",
                                  exc_info=True)
                last_exception = e
                return None  # Don't retry on wholly unexpected errors

            if attempt < self.FETCH_STATE_MAX_RETRIES:
                wait_time = min(current_delay, self.FETCH_STATE_MAX_DELAY)
                jitter = wait_time * 0.1 * (random.random() * 2 - 1)
                actual_wait = max(0.1, wait_time + jitter)
                self.logger.info(
                    f"Retrying curve state fetch for {account_pubkey} in {actual_wait:.2f}s... (Last error: {type(last_exception).__name__})")
                await asyncio.sleep(actual_wait)
                current_delay *= self.FETCH_STATE_BACKOFF_FACTOR
            else:
                self.logger.error(
                    f"Curve state fetch failed for {account_pubkey} after {self.FETCH_STATE_MAX_RETRIES} attempts. Last error: {last_exception}")
                return None

        self.logger.critical(
            f"Exited get_curve_state retry loop unexpectedly for {account_pubkey}. This should not happen.")
        return None

    @staticmethod
    def find_bonding_curve_pda(mint_pubkey: Pubkey) -> Tuple[Pubkey, int]:
        seeds = [b"bonding-curve", bytes(mint_pubkey)]
        program_id = PumpAddresses.PROGRAM  # Corrected to use PROGRAM
        return Pubkey.find_program_address(seeds, program_id)

    @staticmethod
    def find_associated_bonding_curve_pda(mint_pubkey: Pubkey) -> Tuple[Pubkey, int]:
        seeds = [b"token", bytes(mint_pubkey)]
        program_id = PumpAddresses.PROGRAM  # Corrected to use PROGRAM
        return Pubkey.find_program_address(seeds, program_id)

# --- END OF FILE ---