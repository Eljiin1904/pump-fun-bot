# src/core/curve.py

import math
import base64
from dataclasses import dataclass # Keep dataclass import
# 'field' removed
from typing import Optional, Tuple

# --- Borsh/Construct Imports (Direct & Required) ---
# If these fail, the environment/installation is the issue.
from borsh_construct import CStruct, U8, U64, I64, Bool # Specific types
from construct import Bytes, Adapter, ConstructError, Construct # Base types/classes
# --- End Imports ---

# --- Solana/Solders Imports ---
from solders.pubkey import Pubkey
# 'Account' removed as unused
# --- Import AsyncClient directly ---
from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
from .client import SolanaClient # Keep your wrapper for type hints
from solana.exceptions import SolanaRpcException
from ..utils.logger import get_logger

# --- Constants ---
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_TOKEN_DECIMALS = 6
PUMP_FUN_FEE_BPS = 100
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

logger = get_logger(__name__)

# --- Pubkey Adapter (Depends on imported Adapter & Construct) ---
class PubkeyAdapter(Adapter):
    """ Borsh Construct adapter for solders.pubkey.Pubkey """
    def _decode(self, obj: bytes, context, path):
        if len(obj) != 32: raise ConstructError(f"Pubkey 32 bytes", path=path); return Pubkey(obj)
    def _encode(self, obj: Pubkey, context, path):
        if not isinstance(obj, Pubkey): raise ConstructError(f"Value not Pubkey", path=path); return bytes(obj)
# Define the reusable field type using the adapter
SoldersPubkeyField = PubkeyAdapter(Bytes(32))


# --- Verified (Highly Probable) Bonding Curve Structure ---
# This definition now directly uses the imported constructs.
try:
    BONDING_CURVE_LAYOUT = CStruct(
        "discriminator" / Bytes(8),           # Offset 0-7
        "state_flags" / U8,                   # Offset 8
        "bonding_curve_bump" / U8,            # Offset 9
        "creator_vesting_complete" / Bool,    # Offset 10
        "padding1" / Bytes(5),               # Offset 11-15 (Renamed)
        "virtual_sol_reserves" / U64,         # Offset 16-23
        "virtual_token_reserves" / U64,       # Offset 24-31
        "real_sol_reserves" / U64,            # Offset 32-39
        "real_token_reserves" / U64,          # Offset 40-47
        "total_supply" / U64,                 # Offset 48-55
        "complete" / Bool,                    # Offset 56
        "padding2" / Bytes(7),               # Offset 57-63 (Renamed)
        "created_timestamp" / I64,            # Offset 64-71
        "padding3" / Bytes(9),               # Offset 72-80 (Renamed)
        "mint" / SoldersPubkeyField,          # Offset 81-112
        "bonding_curve_sol_vault" / SoldersPubkeyField, # Offset 113-144
        "creator" / SoldersPubkeyField        # Offset 145-176
    )
    logger.info("BONDING_CURVE_LAYOUT defined successfully.")
except Exception as e:
     logger.critical(f"CRITICAL: Failed to define BONDING_CURVE_LAYOUT. Check borsh/construct setup and schema definition. Error: {e}", exc_info=True)
     BONDING_CURVE_LAYOUT = None # Ensure it's None if definition fails


# --- BondingCurveState Dataclass ---
@dataclass
class BondingCurveState:
    # ... (Fields remain the same as previous corrected version) ...
    virtual_sol_reserves: int; virtual_token_reserves: int; complete: bool; real_sol_reserves: int; total_supply: int; creator: Pubkey; mint: Pubkey; bonding_curve_sol_vault: Pubkey; state_flags: Optional[int] = None; curve_pda_bump: Optional[int] = None; real_token_reserves: Optional[int] = None; created_at_timestamp: Optional[int] = None; is_vesting_complete: Optional[bool] = None

    # --- Calculation methods remain the same ---
    def _calculate_slope_m_components(self) -> Optional[Tuple[int, int]]: # ... (implementation) ...
        S = self.virtual_token_reserves; C = self.virtual_sol_reserves;
        if S <= 0: return None;
        if C < 0: logger.warning(f"Slope 'm' calc warning: C < 0 ({C}).");
        m_numerator = 2 * C; m_denominator = S * S;
        if m_denominator == 0: return None;
        return m_numerator, m_denominator;
    def calculate_price(self, decimals: int = DEFAULT_TOKEN_DECIMALS) -> float: # ... (implementation) ...
        S = self.virtual_token_reserves; C = self.virtual_sol_reserves;
        if S <= 0: return 0.0;
        try: price_lamports_per_base_unit = (2 * C) / S; price_sol_per_ui_token = (price_lamports_per_base_unit / LAMPORTS_PER_SOL) * (10**decimals); return price_sol_per_ui_token;
        except ZeroDivisionError: return 0.0;
        except Exception as e: logger.error(f"Error calculating linear curve price: {e}"); return 0.0;
    def calculate_tokens_out_for_sol(self, sol_in_lamports: int) -> int: # ... (implementation) ...
        if sol_in_lamports <= 0: return 0;
        S = self.virtual_token_reserves; C_reserve = self.virtual_sol_reserves; fee_lamports = (sol_in_lamports * PUMP_FUN_FEE_BPS) // 10000; C_in = sol_in_lamports - fee_lamports;
        if C_in <= 0: return 0;
        slope_components = self._calculate_slope_m_components();
        if S == 0 and C_reserve == 0: return 0;
        if slope_components is None: return 0;
        m_num, m_den = slope_components;
        try:
            if m_num == 0: return 0;
            term1_numerator = 2 * C_in * m_den; term1 = term1_numerator // m_num; s_squared = S * S; term_inside_sqrt = term1 + s_squared;
            if term_inside_sqrt < 0: return 0;
            sqrt_val = math.isqrt(term_inside_sqrt); tokens_out = sqrt_val - S; return max(0, tokens_out);
        except (ValueError, ZeroDivisionError, OverflowError) as e: logger.error(f"Error during linear token calc: {e}"); return 0;
        except Exception as e: logger.error(f"Unexpected error calculating linear tokens out: {e}"); return 0;
    def calculate_sol_out_for_tokens(self, tokens_in_base_units: int) -> int: # ... (implementation) ...
        if tokens_in_base_units <= 0: return 0;
        S = self.virtual_token_reserves; dN = tokens_in_base_units;
        if S <= 0: return 0;
        if dN > S: dN = S;
        slope_components = self._calculate_slope_m_components();
        if slope_components is None: return 0;
        m_num, m_den = slope_components;
        try:
            s_squared = S * S; s_minus_dn = S - dN; s_minus_dn_squared = s_minus_dn * s_minus_dn; bracket_term = s_squared - s_minus_dn_squared;
            sol_out_before_fee_num = m_num * bracket_term; sol_out_before_fee_den = 2 * m_den;
            if sol_out_before_fee_den == 0: return 0;
            sol_out_before_fee = sol_out_before_fee_num // sol_out_before_fee_den; fee_lamports = (sol_out_before_fee * PUMP_FUN_FEE_BPS) // 10000;
            sol_out_after_fee = sol_out_before_fee - fee_lamports; return max(0, sol_out_after_fee);
        except (ZeroDivisionError, OverflowError) as e: logger.error(f"Error during linear SOL calc: {e}"); return 0;
        except Exception as e: logger.error(f"Unexpected error calculating linear SOL out: {e}"); return 0;

class BondingCurveManager:
    """Manages interactions with the bonding curve."""
    # --- Corrected __init__ to use imported AsyncClient ---
    def __init__(self, client: SolanaClient):
        self.client = client # Keep reference to the wrapper if needed
        # Get the async client instance directly from the wrapper
        try:
            self.async_client: SolanaPyAsyncClient = self.client.get_async_client()
            if not isinstance(self.async_client, SolanaPyAsyncClient):
                raise TypeError(f"get_async_client returned unexpected type: {type(self.async_client)}")
            logger.debug("BondingCurveManager obtained async client successfully.")
        except AttributeError:
             logger.critical("FATAL: SolanaClient object must have a 'get_async_client' method returning an AsyncClient.")
             raise # Stop initialization if async client cannot be obtained
        except Exception as e:
             logger.critical(f"FATAL: Failed to get async client in BondingCurveManager: {e}", exc_info=True)
             raise
    # --- End Correction ---

    @staticmethod
    def find_bonding_curve_pda(mint_pubkey: Pubkey) -> tuple[Pubkey, int]: # ... (implementation) ...
        seeds = [b"bonding-curve", bytes(mint_pubkey)]; pda, bump = Pubkey.find_program_address(seeds, PUMP_FUN_PROGRAM_ID); return pda, bump

    async def get_bonding_curve_details(self, bonding_curve_pda: Pubkey, commitment: Optional[str] = "confirmed") -> Optional[Tuple[BondingCurveState, Pubkey]]:
        """ Fetches and deserializes the bonding curve state... """
        # --- Implementation uses self.async_client (validated in __init__) ---
        logger.debug(f"Fetching bonding curve state PDA: {bonding_curve_pda}")
        if BONDING_CURVE_LAYOUT is None: logger.critical("Schema not defined."); return None;
        try:
            res = await self.async_client.get_account_info(bonding_curve_pda, encoding="base64", commitment=commitment);
            # ... (Rest of deserialization logic remains the same) ...
            if res.value is None: logger.warning(f"Account info not found {bonding_curve_pda}"); return None;
            account_data = res.value.data;
            if not isinstance(account_data, list) or len(account_data) < 1: logger.error(f"Unexpected data format {bonding_curve_pda}"); return None;
            raw_data = base64.b64decode(account_data[0]); logger.debug(f"Fetched {len(raw_data)} bytes.");
            try: parsed_data = BONDING_CURVE_LAYOUT.parse(raw_data); logger.debug(f"Deserialized successfully.");
            except Exception as e: logger.error(f"FAIL Deserialize {bonding_curve_pda}. Len: {len(raw_data)}. Err: {e}", exc_info=True); return None;
            sol_vault_field_name = "bonding_curve_sol_vault";
            if not hasattr(parsed_data, sol_vault_field_name): logger.critical(f"Schema missing vault field: '{sol_vault_field_name}'"); return None;
            sol_vault_pubkey = getattr(parsed_data, sol_vault_field_name);
            if not isinstance(sol_vault_pubkey, Pubkey): logger.critical(f"Field '{sol_vault_field_name}' not Pubkey."); return None;
            logger.debug(f"Extracted SOL Vault: {sol_vault_pubkey}");
            state = BondingCurveState( # Map fields
                virtual_sol_reserves=getattr(parsed_data, "virtual_sol_reserves", 0), virtual_token_reserves=getattr(parsed_data, "virtual_token_reserves", 0),
                complete=getattr(parsed_data, "complete", False), real_sol_reserves=getattr(parsed_data, "real_sol_reserves", 0),
                total_supply=getattr(parsed_data, "total_supply", 0), creator=getattr(parsed_data, "creator", Pubkey.from_string("11111111111111111111111111111111")),
                mint=getattr(parsed_data, "mint", Pubkey.from_string("11111111111111111111111111111111")), bonding_curve_sol_vault=sol_vault_pubkey,
                state_flags=getattr(parsed_data, "state_flags", 0), curve_pda_bump=getattr(parsed_data, "bonding_curve_bump", 0),
                real_token_reserves=getattr(parsed_data, "real_token_reserves", 0), # Keep original name
                created_at_timestamp=getattr(parsed_data, "created_timestamp", 0),
                is_vesting_complete=getattr(parsed_data, "creator_vesting_complete", False));
            return state, sol_vault_pubkey;
        except SolanaRpcException as e: logger.error(f"RPC Error fetching {bonding_curve_pda}: {e}"); return None;
        except Exception as e: logger.error(f"Unexpected error fetch/parse {bonding_curve_pda}: {e}", exc_info=True); return None;

    async def get_curve_state(self, curve_pubkey: Pubkey, commitment: Optional[str] = "confirmed") -> Optional[BondingCurveState]:
         details = await self.get_bonding_curve_details(curve_pubkey, commitment); return details[0] if details else None;