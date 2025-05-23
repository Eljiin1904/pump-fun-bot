# src/monitoring/base_listener.py
"""
Base class for WebSocket token listeners.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Optional

from src.core.client import SolanaClient
from src.trading.base import TokenInfo


class BaseTokenListener(ABC):
    """
    Base abstract class for token listeners.
    Provides a shared SolanaClient and a stop event.
    """

    def __init__(self, client: SolanaClient):
        # Store the Solana RPC wrapper for subclasses
        self.client: SolanaClient = client
        # Event to signal shutdown
        self._stop_event: asyncio.Event = asyncio.Event()

    @abstractmethod
    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: Optional[str] = None,
        creator_address: Optional[str] = None,
    ) -> None:
        """
        Listen for new token creations and invoke the callback.

        Args:
            token_callback: Async function to call when a new token is found.
            match_string: Optional string to match in token name/symbol.
            creator_address: Optional creator address (string) to filter by.
        """
        ...

    async def stop(self) -> None:
        """
        Signal to the listener to stop its loop and clean up.
        """
        if not self._stop_event.is_set():
            self._stop_event.set()
