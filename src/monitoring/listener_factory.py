# src/monitoring/listener_factory.py

import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Callable, Coroutine, Any

from solders.pubkey import Pubkey

from src.core.client import SolanaClient
from src.monitoring.logs_listener import LogsListener
from src.trading.base import TokenInfo
from src.utils.logger import get_logger

logger = get_logger(__name__)


class BaseListener(ABC):
    """Abstract Base Class for token listeners."""

    def __init__(self, client: SolanaClient):
        self.client = client
        self._stop_event = asyncio.Event()
        logger.debug(f"Initialized {self.__class__.__name__}")

    @abstractmethod
    async def listen_for_tokens(
            self,
            token_callback: Callable[[TokenInfo], Coroutine[Any, Any, None]],
            match_string: Optional[str] = None,
            creator_address_filter: Optional[str] = None
    ) -> None:
        pass

    async def stop(self) -> None:
        logger.info(f"Stopping {self.__class__.__name__}...")
        self._stop_event.set()


class ListenerFactory:
    @staticmethod
    def create_listener(
            listener_type: str,
            client: SolanaClient,
            wss_endpoint: str,
            # --- REMOVE processor_client argument from FACTORY METHOD ---
            # logs_event_processor_client: Optional[SolanaClient] = None # No longer needed here
            # --- END REMOVAL ---
    ) -> BaseListener:

        listener_type = listener_type.lower()
        logger.info(f"Creating listener of type: {listener_type}")

        if listener_type == "logs":
            # --- FIX: Call LogsListener with only the arguments it expects ---
            # LogsListener creates its own processor internally using the passed client
            return LogsListener(client=client, wss_endpoint=wss_endpoint)
            # --- END FIX ---
        # --- Add other listener types here ---
        # elif listener_type == "birdeye":
        #     # ... create BirdeyeListener ...
        else:
            raise ValueError(f"Unsupported listener type: {listener_type}")