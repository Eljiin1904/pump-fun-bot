# src/core/priority_fee/manager.py (Corrected Syntax & Dynamic Re-enabled)

import logging
from typing import Optional, List, Any
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient

try:
    # Import DynamicPriorityFee
    from .dynamic_fee import DynamicPriorityFee
    # Adjust relative path if logger is elsewhere
    from ...utils.logger import get_logger
except ImportError as e:
    print(f"ERROR importing in priority_fee.manager: {e}")
    # Dummies
    DynamicPriorityFee=object; get_logger=lambda x: type('dummy', (object,), {'info':print, 'error':print, 'warning':print})(); SolanaPyAsyncClient=object; Instruction=object; set_compute_unit_limit=None; set_compute_unit_price=None; Pubkey=object

logger = get_logger(__name__)

DEFAULT_COMPUTE_UNITS = 200_000
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

class PriorityFeeManager:
    """ Manages fetching and applying priority fees. """
    def __init__(
        self,
        client: SolanaPyAsyncClient, # Expects the raw async client
        # Removed rpc_endpoint
        enable_dynamic_fee: bool = True, # Use the flag passed from config/cli
        enable_fixed_fee: bool = False,
        fixed_fee: int = 10_000, # micro-lamports per Million CU
        extra_fee: int = 0,
        hard_cap: int = 1_000_000,
        default_compute_units: int = DEFAULT_COMPUTE_UNITS
        ):
        self.client = client
        # --- Use the passed enable_dynamic_fee flag ---
        self.enable_dynamic_fee = enable_dynamic_fee
        # --- End ---
        self.enable_fixed_fee = enable_fixed_fee
        self.fixed_fee_per_million_cu = fixed_fee
        self.extra_fee_microlamports = extra_fee
        self.hard_cap_microlamports = hard_cap
        self.default_compute_units = default_compute_units
        self.dynamic_fee_plugin: Optional[DynamicPriorityFee] = None

        # --- Instantiate DynamicPriorityFee if enabled ---
        if self.enable_dynamic_fee:
            try:
                # Pass only the client
                self.dynamic_fee_plugin = DynamicPriorityFee(self.client)
                logger.info("Dynamic priority fee plugin enabled and initialized.")
            except Exception as e:
                 logger.error(f"Failed to initialize DynamicPriorityFee plugin: {e}", exc_info=True)
                 self.dynamic_fee_plugin = None
                 self.enable_dynamic_fee = False # Fallback to disabled if init fails
        # --- End ---

        if self.enable_fixed_fee:
             logger.info(f"Fixed priority fee plugin enabled (Rate: {self.fixed_fee_per_million_cu} microLamports/1M CU).")
             if self.enable_dynamic_fee: # Check if dynamic is *still* enabled after potential init failure
                  logger.warning("Both dynamic and fixed fees enabled, prioritizing FIXED fee.")
                  self.enable_dynamic_fee = False
                  self.dynamic_fee_plugin = None # Ensure plugin is None if fixed takes over

    async def get_fee(self, instructions: Optional[List[Instruction]] = None, accounts: Optional[List[Pubkey]] = None) -> int:
        """ Calculates the total priority fee in micro-lamports. """
        base_fee = 0
        compute_units = self._get_compute_units(instructions) or self.default_compute_units

        if self.enable_fixed_fee:
            base_fee = (compute_units * self.fixed_fee_per_million_cu) // 1_000_000
            logger.debug(f"Using fixed fee rate: {self.fixed_fee_per_million_cu}/1M_CU for {compute_units} CU = {base_fee} microLamports")
        # --- Call dynamic fee plugin if enabled ---
        elif self.enable_dynamic_fee and self.dynamic_fee_plugin:
            logger.debug("Attempting to get dynamic fee from plugin...")
            base_fee = await self.dynamic_fee_plugin.get_priority_fee(accounts)
            logger.debug(f"Dynamic fee plugin returned target fee: {base_fee} microLamports")
        # --- End ---
        else:
             if self.enable_dynamic_fee and not self.dynamic_fee_plugin:
                  logger.warning("Dynamic fee enabled but plugin failed to initialize. Using 0 base fee.")
             else:
                  logger.debug("No priority fee strategy enabled (Fixed=False, Dynamic=False/Plugin Missing). Using 0 base fee.")
             base_fee = 0

        total_fee = base_fee + self.extra_fee_microlamports
        final_fee = min(total_fee, self.hard_cap_microlamports) if self.hard_cap_microlamports > 0 else total_fee
        final_fee = max(0, final_fee) # Ensure non-negative

        if final_fee > 0:
            logger.info(f"Calculated total priority fee: {final_fee} microLamports")
        else:
            logger.debug("Calculated total priority fee 0.")
        return final_fee

    def _get_compute_units(self, instructions: Optional[List[Instruction]]) -> Optional[int]:
        """ Extracts compute unit limit from instructions if set. """
        if not instructions:
            return None
        for ix in instructions:
             try:
                 if ix.program_id == COMPUTE_BUDGET_PROGRAM_ID and len(ix.data) >= 5 and ix.data[0] == 2:
                      units = int.from_bytes(ix.data[1:5], 'little')
                      logger.debug(f"Found SetComputeUnitLimit: {units}")
                      return units
             except Exception:
                 # Ignore errors parsing unrelated instructions
                 pass
        return None # No limit found

    def create_priority_fee_instructions(self, priority_fee_total_microlamports: int, compute_units: Optional[int] = None) -> List[Instruction]:
        """ Creates SetComputeUnitPrice and SetComputeUnitLimit instructions based on total fee. """
        ixs = []
        target_compute_units = compute_units if compute_units is not None else self.default_compute_units

        # Ensure solders functions are available (handle potential import issues)
        if set_compute_unit_limit is None or set_compute_unit_price is None:
            logger.error("solders.compute_budget functions (set_compute_unit_limit/price) not available.")
            return ixs

        # Add SetComputeUnitLimit instruction
        ixs.append(set_compute_unit_limit(units=target_compute_units))
        logger.debug(f"Adding SetComputeUnitLimit instruction: {target_compute_units} units")

        # Add SetComputeUnitPrice instruction if fee > 0
        if priority_fee_total_microlamports > 0:
            if target_compute_units > 0:
                 # Calculate micro-lamports per 1M compute units for the instruction
                 micro_lamports_per_million_cu = (priority_fee_total_microlamports * 1_000_000) // target_compute_units
                 if micro_lamports_per_million_cu > 0:
                     # Pass fee per 1M CU to set_compute_unit_price
                     ixs.append(set_compute_unit_price(micro_lamports=micro_lamports_per_million_cu))
                     logger.debug(f"Adding SetComputeUnitPrice instruction: {micro_lamports_per_million_cu} microLamports/1M_CU")
                 else:
                     logger.debug("Calculated fee per CU is 0 or less, skipping SetComputeUnitPrice.")
            else:
                logger.warning("Target compute units is 0, cannot calculate fee per CU.")
        else:
            logger.debug("Total priority fee is 0, skipping SetComputeUnitPrice.")

        return ixs

# --- END OF FILE ---