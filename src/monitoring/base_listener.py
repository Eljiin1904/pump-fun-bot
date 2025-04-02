# src/monitoring/base_listener.py
"""
Base class for WebSocket token listeners.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Optional  # Import Optional

# Import TokenInfo relative to this file's location
from ..trading.base import TokenInfo


class BaseTokenListener(ABC):
    """Base abstract class for token listeners."""

    @abstractmethod
    async def listen_for_tokens(
        self,
        # Callback signature: Takes TokenInfo, returns nothing (Awaitable None)
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: Optional[str] = None,
        creator_address: Optional[str] = None,
        # Add max_token_age if the base listener should handle it (optional)
        # max_token_age: Optional[float] = None
    ) -> None:
        """
        Listen for new token creations and invoke the callback.

        Args:
            token_callback: Async function to call when a new token is found.
            match_string: Optional string to match in token name/symbol.
            creator_address: Optional creator address (string) to filter by.
            # max_token_age: Optional maximum age in seconds for a token to be considered.
        """
        pass

    # Optional: Add a common stop method if all listeners should have one
    # @abstractmethod
    # async def stop(self) -> None:
    #     """Stop the listener."""
    #     pass