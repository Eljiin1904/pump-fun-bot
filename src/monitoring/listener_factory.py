# src/monitoring/listener_factory.py
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Callable, Coroutine, Any

from solders.pubkey import Pubkey

# Assuming SolanaClient, LogsEventProcessor etc. are imported if needed by concrete listeners
from src.core.client import SolanaClient
from src.monitoring.logs_listener import LogsListener  # Example concrete listener
# Import other listener types if they exist (e.g., BirdeyeListener)
from src.trading.base import TokenInfo  # Need TokenInfo definition
from src.utils.logger import get_logger

logger = get_logger(__name__)


# --- NEW BaseListener Class ---
class BaseListener(ABC):
    """Abstract Base Class for token listeners."""

    def __init__(self, client: SolanaClient):
        self.client = client  # Listeners might need the client
        self._stop_event = asyncio.Event()
        logger.debug(f"Initialized {self.__class__.__name__}")

    @abstractmethod
    async def listen_for_tokens(
            self,
            token_callback: Callable[[TokenInfo], Coroutine[Any, Any, None]],
            match_string: Optional[str] = None,
            creator_address_filter: Optional[str] = None
    ) -> None:
        """Start listening for new tokens and invoke the callback."""
        pass  # Concrete implementations will override this

    async def stop(self) -> None:
        """Signal the listener to stop."""
        logger.info(f"Stopping {self.__class__.__name__}...")
        self._stop_event.set()
        # Concrete implementations might need additional cleanup in their stop methods


# --- END NEW BaseListener Class ---


class ListenerFactory:
    @staticmethod
    def create_listener(
            listener_type: str,
            client: SolanaClient,  # Pass the client wrapper
            wss_endpoint: str,
            logs_event_processor_client: Optional[SolanaClient] = None  # Pass client for processor if needed
    ) -> BaseListener:  # Return type is the BaseListener interface

        listener_type = listener_type.lower()
        logger.info(f"Creating listener of type: {listener_type}")

        if listener_type == "logs":
            # Ensure LogsListener gets the client it needs for its processor
            if not logs_event_processor_client:
                logger.warning(
                    "Logs listener selected, but no client provided for its processor. Processor might fail.")
                # Or pass the main client if appropriate: logs_event_processor_client = client
            return LogsListener(client=client, wss_endpoint=wss_endpoint, processor_client=logs_event_processor_client)
        # --- Add other listener types here ---
        # elif listener_type == "birdeye":
        #     from .birdeye_listener import BirdeyeListener # Example
        #     return BirdeyeListener(client=client, api_key="YOUR_BIRDEYE_API_KEY") # Get API key from config
        else:
            raise ValueError(f"Unsupported listener type: {listener_type}")