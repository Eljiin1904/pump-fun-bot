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
# --- FIX: Import the class, not the non-existent constant ---
from src.core.pubkeys import PumpAddresses # Import the class containing program IDs
# --- END FIX ---
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
    "complete_raw" / U64 # Using _raw suffix to avoid conflict with property
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
        # Price formula for linear curve: dSOL/dToken = k / (token_supply + token_base_supply)^2
        # Simpler for instantaneous buy: SOL price = virtual_sol_reserves / virtual_token_reserves (average price)
        # Or for marginal price: Use derivative if needed, but pump.fun uses specific formula based on reserves.
        # Let's use the approximate formula based on reserves for now:
        # Price per token lamport = virtual_sol_reserves / virtual_token_reserves (less accurate for buys)
        # A common approximation for small buys: Price ≈ virtual_sol_reserves / virtual_token_reserves
        # Or using the pump.fun contract's sell quote logic: sol_out = (tokens_in * virtual_sol_reserves) // (virtual_token_reserves + tokens_in)
        # Let's provide an approximate spot price per whole token in SOL
        try:
            price_lamports_per_token_lamport = self.virtual_sol_reserves / self.virtual_token_reserves
            price_sol_per_ui_token = (price_lamports_per_token_lamport / LAMPORTS_PER_SOL) * (10 ** decimals)
            return price_sol_per_ui_token
        except ZeroDivisionError:
            return 0.0

    def calculate_tokens_out_for_sol(self, sol_in_lamports: int) -> int:
        """Calculates token amount received for a given SOL input using bonding curve formula."""
        if sol_in_lamports <= 0: return 0
        # Formula: tokens_out = virtual_token_reserves * ((1 + sol_in / virtual_sol_reserves)^0.5 - 1)
        # Or simplified as pump.fun uses: Buy quote = floor(tokens_supply * (sqrt(1 + sol_amount / sol_balance) - 1))
        # Let's use the inverse of the sell quote for approximation if exact buy formula isn't available:
        # Approximation: tokens_out = (sol_in_lamports * virtual_token_reserves) / virtual_sol_reserves
        # It's better to get the exact quote from an SDK or simulate the transaction if precision is critical.
        # Using the formula derived from sell quote (less accurate for buys):
        try:
            # This formula seems related to constant product, not pump.fun's specific curve.
            # Need to verify the correct pump.fun buy formula.
            # Placeholder - Returning 0 until correct formula is confirmed.
            # tokens_out = int((sol_in_lamports * self.virtual_token_reserves) // (self.virtual_sol_reserves)) # Incorrect approximation
            # Correct Buy Quote simulation is complex. Return 0 for now.
            logger = get_logger(__name__)
            logger.warning("calculate_tokens_out_for_sol needs the precise pump.fun buy formula.")
            return 0 # Return 0 as placeholder
        except ZeroDivisionError:
            return 0

    def calculate_sol_out_for_tokens(self, tokens_in: int) -> int:
        """Calculates SOL received for selling tokens using bonding curve formula."""
        if tokens_in <= 0: return 0
        if self.virtual_token_reserves == 0: return 0 # Cannot sell if no virtual tokens
        # Formula: sol_out = virtual_sol_reserves * (1 - (virtual_token_reserves / (virtual_token_reserves + tokens_in)))
        # Simplified pump.fun: sol_out = floor(sol_balance * (1 - (token_supply / (token_supply + token_amount))^2)) -> This seems more like sqrt price curve
        # Pump.fun actual sell quote: sol_out = floor(sol_balance * tokens_in / (token_supply + tokens_in))
        try:
            sol_out = int((self.virtual_sol_reserves * tokens_in) // (self.virtual_token_reserves + tokens_in))
            return sol_out
        except ZeroDivisionError: # Should be caught by virtual_token_reserves check, but defensive
             return 0

# --- Standalone Decoder Function ---
def decode_bonding_curve_account(raw_data: bytes) -> Optional[BondingCurveState]:
    """Decodes raw bonding curve data, handling 48/49 byte cases."""
    expected_size = BONDING_CURVE_LAYOUT.sizeof() # Should be 48
    logger = get_logger(__name__) # Get logger instance
    data_to_parse: Optional[bytes] = None

    # Pump.fun bonding curve account data starts with 'bonding_curve' discriminator (8 bytes)
    # followed by the layout. Total expected size might include discriminator.
    # Let's assume the layout size (48 bytes) is what we expect *after* potential discriminator/padding.
    # Common observation: Sometimes accounts have an extra byte (e.g., from account type enum).
    # Adjust logic based on observed data length from getAccountInfo for the bonding curve PDA.

    if len(raw_data) == expected_size:
        data_to_parse = raw_data
        logger.debug("Decoding bonding curve data with expected length %d.", expected_size)
    elif len(raw_data) == expected_size + 1:
        logger.warning("Received %d bytes (expected %d). Assuming extra leading byte, stripping.", len(raw_data), expected_size)
        data_to_parse = raw_data[1:]
    elif len(raw_data) == expected_size + 8: # Check for 8-byte discriminator
         logger.debug("Received %d bytes. Assuming 8-byte discriminator, stripping.", len(raw_data))
         data_to_parse = raw_data[8:]
    elif len(raw_data) == expected_size + 9: # Check for 8-byte discriminator + 1 padding byte
         logger.warning("Received %d bytes. Assuming 8-byte discriminator and 1 padding byte, stripping.", len(raw_data))
         data_to_parse = raw_data[9:]
    else:
        logger.error(f"Unexpected bonding curve account data length: {len(raw_data)} (expected layout size {expected_size}, possibly with discriminator/padding).")
        return None

    if data_to_parse:
        try:
            # Parse the potentially stripped data using the layout
            parsed = BONDING_CURVE_LAYOUT.parse(data_to_parse)

            # Initialize BondingCurveState using direct attribute access from parsed object
            required_fields = ['virtual_token_reserves', 'virtual_sol_reserves', 'real_token_reserves', 'real_sol_reserves', 'token_total_supply', 'complete_raw']
            if all(hasattr(parsed, field) for field in required_fields):
                return BondingCurveState(
                    virtual_token_reserves=parsed.virtual_token_reserves,
                    virtual_sol_reserves=parsed.virtual_sol_reserves,
                    real_token_reserves=parsed.real_token_reserves,
                    real_sol_reserves=parsed.real_sol_reserves,
                    token_total_supply=parsed.token_total_supply,
                    complete_raw=parsed.complete_raw # Pass the raw value
                )
            else:
                missing = [f for f in required_fields if not hasattr(parsed, f)]
                logger.error(f"Parsed object missing required fields for BondingCurveState: {missing}. Parsed object type: {type(parsed)}, dir: {dir(parsed)}")
                return None
        except ConstructError as e: # Catch specific parsing errors
             logger.error(f"Borsh construct error decoding bonding curve data (length {len(data_to_parse)}): {e}", exc_info=False)
             return None
        except Exception as e: # Catch other potential errors during parsing/init
            logger.error(f"Unexpected error decoding bonding curve data (length {len(data_to_parse)}): {e}", exc_info=True)
            return None
    return None


# --- BondingCurveManager class ---
class BondingCurveManager:
    """ Manager for the bonding curve. Handles fetching/decoding state and computing PDAs. """
    FETCH_STATE_MAX_RETRIES = 5
    FETCH_STATE_INITIAL_DELAY = 0.5 # Reduced initial delay
    FETCH_STATE_BACKOFF_FACTOR = 1.5 # Reduced backoff factor
    FETCH_STATE_MAX_DELAY = 5.0    # Reduced max delay

    def __init__(self, client): # Expects SolanaClient wrapper instance
        self.client = client
        self.logger = get_logger(__name__)

    async def get_curve_state( self, account_pubkey: Pubkey, commitment: Optional[Commitment] = Confirmed ) -> Optional[BondingCurveState]:
        """Fetches and decodes the bonding curve state with exponential backoff."""
        current_delay = self.FETCH_STATE_INITIAL_DELAY
        last_exception: Optional[Exception] = None
        for attempt in range(1, self.FETCH_STATE_MAX_RETRIES + 1):
            self.logger.debug(f"Fetching curve state for {account_pubkey} (Attempt {attempt}/{self.FETCH_STATE_MAX_RETRIES})")
            try:
                # Use the SolanaClient wrapper's method
                resp_value = await self.client.get_account_info(
                    account_pubkey,
                    encoding="base64",
                    commitment=commitment
                )

                # Check response structure from SolanaClient wrapper
                if resp_value is None or not hasattr(resp_value, 'data'):
                    self.logger.warning(f"Curve state fetch {attempt}: Account {account_pubkey} not found or RPC error (no data). Response: {resp_value}")
                    last_exception = ValueError("Account not found or no data in response")
                else:
                    # Access data assuming resp_value has .data attribute based on SolanaClient structure
                    raw_data: Optional[bytes] = None
                    account_data = resp_value.data
                    if isinstance(account_data, bytes):
                        raw_data = account_data
                    elif isinstance(account_data, (list, tuple)) and account_data and isinstance(account_data[0], str):
                        try: raw_data = base64.b64decode(account_data[0])
                        except (base64.binascii.Error, TypeError) as b64e: self.logger.error(f"Base64 decode error (list/tuple) for {account_pubkey}: {b64e}"); return None
                    elif isinstance(account_data, str): # Handle direct base64 string
                         try: raw_data = base64.b64decode(account_data)
                         except (base64.binascii.Error, TypeError) as b64e: self.logger.error(f"Base64 decode error (str) for {account_pubkey}: {b64e}"); return None
                    else:
                        self.logger.error(f"Unexpected data type in bonding curve account {account_pubkey}: {type(account_data)}"); return None

                    if raw_data:
                        state = decode_bonding_curve_account(raw_data)
                        if state is None:
                            # Error logged within decode function
                            self.logger.error(f"Failed to decode bonding curve data for {account_pubkey} (Len: {len(raw_data)}).")
                            # Don't retry on decode failure, likely permanent data issue
                            return None
                        else:
                            self.logger.debug(f"Successfully fetched/decoded state for {account_pubkey}")
                            return state # Success
                    else:
                        self.logger.warning(f"Account {account_pubkey} has empty data field.")
                        # Treat as account not found essentially, allow retry
                        last_exception = ValueError("Account data field is empty")

            except SolanaRpcException as e:
                self.logger.warning(f"RPC Exception fetching curve state {account_pubkey} (Attempt {attempt}): {e}")
                last_exception = e
            except asyncio.TimeoutError:
                 self.logger.warning(f"Curve state fetch {attempt} timed out for {account_pubkey}.")
                 last_exception = asyncio.TimeoutError("Fetch timed out")
            except Exception as e:
                 # Catch unexpected errors during the fetch/decode process
                 self.logger.error(f"Unexpected error in get_curve_state for {account_pubkey} (Attempt {attempt}): {e}", exc_info=True)
                 last_exception = e
                 # Don't retry on unexpected errors
                 return None

            # If we reach here, an error occurred (account not found, timeout, RPC error)
            if attempt < self.FETCH_STATE_MAX_RETRIES:
                wait_time = min(current_delay, self.FETCH_STATE_MAX_DELAY)
                # Add jitter: random adjustment up to +/- 10% of wait_time
                jitter = wait_time * 0.1 * (random.random() * 2 - 1)
                actual_wait = max(0.1, wait_time + jitter) # Ensure minimum wait
                self.logger.info(f"Retrying curve state fetch for {account_pubkey} in {actual_wait:.2f}s... (Last error: {type(last_exception).__name__})")
                await asyncio.sleep(actual_wait)
                current_delay *= self.FETCH_STATE_BACKOFF_FACTOR # Increase delay for next retry
            else:
                self.logger.error(f"Curve state fetch failed for {account_pubkey} after {self.FETCH_STATE_MAX_RETRIES} attempts. Last error: {last_exception}")
                return None

        # Should not be reached if loop logic is correct
        self.logger.error(f"Exited get_curve_state retry loop unexpectedly for {account_pubkey}.")
        return None

    @staticmethod
    def find_bonding_curve_pda(mint_pubkey: Pubkey) -> Tuple[Pubkey, int]:
        """Finds the PDA for the bonding curve account."""
        seeds = [b"bonding-curve", bytes(mint_pubkey)]
        # --- FIX: Use the correct class attribute ---
        program_id = PumpAddresses.PROGRAM
        # --- END FIX ---
        return Pubkey.find_program_address(seeds, program_id)

    @staticmethod
    def find_associated_bonding_curve_pda(mint_pubkey: Pubkey) -> Tuple[Pubkey, int]:
        """Finds the PDA for the bonding curve's associated token account."""
        seeds = [b"token", bytes(mint_pubkey)]
         # --- FIX: Use the correct class attribute ---
        program_id = PumpAddresses.PROGRAM
        # --- END FIX ---
        return Pubkey.find_program_address(seeds, program_id)

# --- END OF FILE ---