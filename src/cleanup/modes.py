# src/cleanup/modes.py
import asyncio
from typing import List, Optional  # Added for type hints

import logger
from solders.pubkey import Pubkey  # Added for type hints

# --- Use absolute imports from src ---
from src.cleanup.manager import AccountCleanupManager

# Import specific config variables directly if needed
# This assumes the config is loaded and accessible globally, which is NOT ideal.
# A better pattern is to pass the config values or the trader instance down.
# For now, importing directly to match the original structure, but this needs review.
try:
    # This import style depends on how config is loaded/accessed globally
    # It might be better to load config values within the functions or pass them.
    from src.config import CLEANUP_MODE, CLEANUP_WITH_PRIORITY_FEE
except ImportError:
    # Fallback if direct import fails (e.g., structure changed or not run via standard entrypoint)
    logger.warning(
        "Could not import CLEANUP_MODE, CLEANUP_WITH_PRIORITY_FEE directly from src.config. Cleanup functions might use defaults or fail.")
    CLEANUP_MODE = "never"  # Default fallback
    CLEANUP_WITH_PRIORITY_FEE = False  # Default fallback

from src.utils.logger import get_logger
# --- Need these for function signatures ---
from src.core.client import SolanaClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager

# --- End Imports ---

logger = get_logger(__name__)


# --- Helper Functions to Check Mode ---
# These rely on the globally imported CLEANUP_MODE
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
# These functions now take client, wallet, mint(s), fee_manager explicitly

async def handle_cleanup_after_failure(
        client: SolanaClient,
        wallet: Wallet,
        mint: Pubkey,
        priority_fee_manager: PriorityFeeManager,
        # Pass config values explicitly instead of relying on global import
        cleanup_mode_setting: str,
        use_priority_fee_setting: bool
):
    """Handles cleanup logic if mode is 'on_fail'."""
    if cleanup_mode_setting == "on_fail":
        logger.info(f"[Cleanup] Triggered by failed buy transaction for mint {mint}.")
        manager = AccountCleanupManager(client, wallet, priority_fee_manager)
        await manager.cleanup_ata(mint, use_priority_fee=use_priority_fee_setting)


async def handle_cleanup_after_sell(
        client: SolanaClient,
        wallet: Wallet,
        mint: Pubkey,
        priority_fee_manager: PriorityFeeManager,
        # Pass config values explicitly
        cleanup_mode_setting: str,
        use_priority_fee_setting: bool
):
    """Handles cleanup logic if mode is 'after_sell'."""
    if cleanup_mode_setting == "after_sell":
        logger.info(f"[Cleanup] Triggered after token sell/completion for mint {mint}.")
        manager = AccountCleanupManager(client, wallet, priority_fee_manager)
        await manager.cleanup_ata(mint, use_priority_fee=use_priority_fee_setting)


async def handle_cleanup_post_session(
        client: SolanaClient,
        wallet: Wallet,
        mints: List[Pubkey],  # Takes a list of mints potentially traded
        priority_fee_manager: PriorityFeeManager,
        # Pass config values explicitly
        cleanup_mode_setting: str,
        use_priority_fee_setting: bool
):
    """Handles cleanup logic if mode is 'post_session'."""
    if cleanup_mode_setting == "post_session":
        logger.info(f"[Cleanup] Triggered post trading session for {len(mints)} mint(s).")
        manager = AccountCleanupManager(client, wallet, priority_fee_manager)
        cleanup_tasks = []
        for mint in mints:
            # Create tasks to run cleanup concurrently
            task = asyncio.create_task(
                manager.cleanup_ata(mint, use_priority_fee=use_priority_fee_setting),
                name=f"PostSessionCleanup_{str(mint)[:8]}"
            )
            cleanup_tasks.append(task)

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            logger.info("[Cleanup] Post-session cleanup attempts finished.")