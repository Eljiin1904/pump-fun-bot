# src/cleanup/modes.py
import asyncio
from typing import List, Optional

import logger
from solders.pubkey import Pubkey

# --- Use absolute imports from src ---
from src.cleanup.manager import AccountCleanupManager

# --- REMOVE incorrect logger import ---
# import logger # REMOVE THIS LINE
# --- Use correct logger import ---
from src.utils.logger import get_logger

# --- Config Import (Keep try/except for now, but direct passing is better) ---
try:
    from src.config import CLEANUP_MODE, CLEANUP_WITH_PRIORITY_FEE
except ImportError:
    logger.warning("Could not import CLEANUP_MODE, CLEANUP_WITH_PRIORITY_FEE directly from src.config.")
    CLEANUP_MODE = "never"
    CLEANUP_WITH_PRIORITY_FEE = False
# --- End Config Import ---

# --- Need these for function signatures ---
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager
# --- End Imports ---

# --- Initialize logger correctly ---
logger = get_logger(__name__) # Initialize logger instance


# --- Helper Functions to Check Mode ---
# These still rely on the potentially problematic global config import above.
# Refactoring to pass config values into these checkers would be more robust.
def should_cleanup_after_failure() -> bool:
    """Checks if cleanup mode is set to 'on_fail'."""
    return CLEANUP_MODE == "on_fail"

def should_cleanup_after_sell() -> bool:
    """Checks if cleanup mode is set to 'after_sell'."""
    return CLEANUP_MODE == "after_sell"

def should_cleanup_post_session() -> bool:
    """Checks if cleanup mode is set to 'post_session'."""
    return CLEANUP_MODE == "post_session"


# --- Cleanup Handler Functions ---
# These correctly take settings as arguments now.

async def handle_cleanup_after_failure(
        client: SolanaClient,
        wallet: Wallet,
        mint: Pubkey,
        priority_fee_manager: PriorityFeeManager,
        cleanup_mode_setting: str,
        use_priority_fee_setting: bool
):
    """Handles cleanup logic if mode is 'on_fail'."""
    # Use the passed setting, not the global check function
    if cleanup_mode_setting == "on_fail":
        logger.info(f"[Cleanup] Triggered by failed buy transaction for mint {mint}.")
        manager = AccountCleanupManager(client, wallet, priority_fee_manager)
        await manager.cleanup_ata(mint, use_priority_fee=use_priority_fee_setting)

async def handle_cleanup_after_sell(
        client: SolanaClient,
        wallet: Wallet,
        mint: Pubkey,
        priority_fee_manager: PriorityFeeManager,
        cleanup_mode_setting: str,
        use_priority_fee_setting: bool
):
    """Handles cleanup logic if mode is 'after_sell'."""
    # Use the passed setting
    if cleanup_mode_setting == "after_sell":
        logger.info(f"[Cleanup] Triggered after token sell/completion for mint {mint}.")
        manager = AccountCleanupManager(client, wallet, priority_fee_manager)
        await manager.cleanup_ata(mint, use_priority_fee=use_priority_fee_setting)

async def handle_cleanup_post_session(
        client: SolanaClient,
        wallet: Wallet,
        mints: List[Pubkey],
        priority_fee_manager: PriorityFeeManager,
        cleanup_mode_setting: str,
        use_priority_fee_setting: bool
):
    """Handles cleanup logic if mode is 'post_session'."""
     # Use the passed setting
    if cleanup_mode_setting == "post_session":
        logger.info(f"[Cleanup] Triggered post trading session for {len(mints)} mint(s).")
        manager = AccountCleanupManager(client, wallet, priority_fee_manager)
        cleanup_tasks = []
        for mint in mints:
            task = asyncio.create_task(
                manager.cleanup_ata(mint, use_priority_fee=use_priority_fee_setting),
                name=f"PostSessionCleanup_{str(mint)[:8]}"
            )
            cleanup_tasks.append(task)

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            logger.info("[Cleanup] Post-session cleanup attempts finished.")