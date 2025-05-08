# src/core/priority_fee/manager.py (Complete with dataclass import fix)

import asyncio
import time
from typing import Optional, List, Any
from solders.pubkey import Pubkey
from dataclasses import dataclass  # <<< IMPORT ADDED HERE

# --- Corrected Imports ---
from src.utils.logger import get_logger  # Use absolute import
# Import from the __init__.py and specific files in this package
from . import PriorityFeePlugin  # Import base class from __init__
from .fixed_fee import FixedPriorityFee  # Import concrete class
# Import dynamic plugin (which now takes client in init)
from .dynamic_fee import DynamicPriorityFeePlugin
# --- End Corrected Imports ---

from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient

logger = get_logger(__name__)


@dataclass  # Decorator now recognized
class PriorityFeeDetails:  # Define locally for return type consistency
    """Data structure to hold calculated priority fee details."""
    fee: int  # Fee in microlamports


class PriorityFeeManager:
    """
    Manages the calculation of priority fees using different strategies (plugins).
    Uses the plugin structure defined in this package.
    """

    def __init__(self,
                 client: SolanaPyAsyncClient,  # Expects the raw AsyncClient
                 enable_dynamic_fee: bool = True,
                 enable_fixed_fee: bool = False,
                 fixed_fee: int = 10000,  # Microlamports
                 extra_fee: int = 0,  # Microlamports to add on top
                 hard_cap: Optional[int] = None,  # Max fee in Microlamports
                 # Dynamic fee specific parameters (passed to plugin init)
                 dynamic_lookback_slots: int = 20,
                 dynamic_percentile: int = 50,  # Median by default
                 dynamic_adjustment_factor: float = 1.0,
                 dynamic_cache_duration_sec: int = 5
                 ):

        if not isinstance(client, SolanaPyAsyncClient):
            logger.warning(
                f"PriorityFeeManager received client of type {type(client)}, expected SolanaPyAsyncClient. Dynamic fees might fail.")
            # Consider raising TypeError here if strict type checking is desired

        self.client = client
        self.plugins: List[PriorityFeePlugin] = []  # List of base type
        self.dynamic_plugin: Optional[DynamicPriorityFeePlugin] = None  # Keep reference to dynamic plugin

        if enable_fixed_fee:
            self.plugins.append(FixedPriorityFee(fixed_fee))  # Use FixedPriorityFee class
            logger.info(f"Initialized FixedPriorityFee with fee={fixed_fee}")

        if enable_dynamic_fee:
            # Pass client to dynamic plugin's constructor now
            try:
                self.dynamic_plugin = DynamicPriorityFeePlugin(
                    client=self.client,  # Pass raw client here
                    lookback_slots=dynamic_lookback_slots,
                    percentile=dynamic_percentile,
                    adjustment_factor=dynamic_adjustment_factor,
                    cache_duration=dynamic_cache_duration_sec
                )
                self.plugins.append(self.dynamic_plugin)
                logger.info("Initialized DynamicPriorityFeePlugin")
            except TypeError as e_init:  # Catch if client type was wrong
                logger.error(f"Failed to initialize DynamicPriorityFeePlugin: {e_init}")
            except Exception as e_init_dyn:
                logger.error(f"Unexpected error initializing DynamicPriorityFeePlugin: {e_init_dyn}", exc_info=True)

        self.extra_fee = max(0, extra_fee)
        self.hard_cap = hard_cap

        log_plugins = [type(p).__name__ for p in self.plugins]
        logger.info(
            f"PriorityFeeManager initialized. Plugins={log_plugins}, ExtraFee={self.extra_fee}, HardCap={self.hard_cap}")

    async def get_priority_fee(self, accounts_to_check: Optional[List[Pubkey]] = None) -> Optional[PriorityFeeDetails]:
        """
        Calculates the priority fee. Sets accounts for dynamic plugin before calculation.
        Returns details object for consistency.
        """

        # Update dynamic plugin with relevant accounts if it exists and was initialized
        if self.dynamic_plugin and hasattr(self.dynamic_plugin, 'set_accounts_for_check'):
            self.dynamic_plugin.set_accounts_for_check(accounts_to_check)

        highest_plugin_fee = 0
        plugin_fees: List[int] = []

        # Use asyncio.gather to run plugin checks concurrently (optional optimization)
        plugin_tasks = [plugin.get_priority_fee() for plugin in self.plugins]
        results = await asyncio.gather(*plugin_tasks, return_exceptions=True)

        for idx, result in enumerate(results):
            plugin_name = type(self.plugins[idx]).__name__
            if isinstance(result, Exception):
                logger.error(f"Error getting fee from plugin {plugin_name}: {result}")
            elif result is not None and result > 0:
                fee = int(result)  # Ensure it's an int
                plugin_fees.append(fee)
                highest_plugin_fee = max(highest_plugin_fee, fee)
                logger.debug(f"Plugin {plugin_name} suggested fee: {fee}")
            elif result is not None:  # Result is <= 0
                logger.debug(f"Plugin {plugin_name} returned zero or negative fee.")
            # else: plugin returned None

        base_plugin_fee = highest_plugin_fee  # Use highest non-zero fee

        final_fee = base_plugin_fee + self.extra_fee

        if self.hard_cap is not None and final_fee > self.hard_cap:
            logger.info(f"Priority fee ({final_fee}) exceeded hard cap ({self.hard_cap}). Capping fee.")
            final_fee = self.hard_cap

        if final_fee <= 0:
            logger.debug("Final priority fee is zero or negative.")
            return None

        logger.info(f"Final Priority Fee: {final_fee} microlamports (Base={base_plugin_fee}, Extra={self.extra_fee})")
        return PriorityFeeDetails(fee=final_fee)