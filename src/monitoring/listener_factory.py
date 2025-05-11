# src/monitoring/listener_factory.py

from typing import Optional

# Core imports
from src.core.client import SolanaClient
from src.utils.logger import get_logger

# Listener imports
from .base_listener import BaseTokenListener
from .logs_listener import LogsListener
from .logs_event_processor import LogsEventProcessor

logger = get_logger(__name__)


class ListenerFactory:
    """
    Factory class responsible for creating instances of different token listeners.
    It ensures that listeners are instantiated with their required dependencies.
    """

    @staticmethod
    def create_listener(
        listener_type: str,
        client: SolanaClient,           # required for RPC calls
        wss_endpoint: str,              # WebSocket URL
        create_event_discriminator: str # 8-byte hex prefix for your on-chain event
    ) -> BaseTokenListener:
        """
        Creates and returns an instance of a specific listener based on type.
        """
        logger.info(f"Creating listener of type: '{listener_type}'")

        if listener_type == "logs":
            if not client:
                raise ValueError(
                    "SolanaClient instance is required to initialize LogsEventProcessor for the 'logs' listener."
                )

            # Pass ONLY the discriminator into the processor
            processor = LogsEventProcessor(create_event_discriminator)

            logger.debug(
                f"FACTORY DEBUG: Preparing to call LogsListener "
                f"with wss={wss_endpoint}, proc={type(processor).__name__}, client={type(client).__name__}"
            )
            return LogsListener(
                wss_endpoint=wss_endpoint,
                processor=processor,
                client=client
            )

        elif listener_type == "birdeye":
            logger.error("Birdeye listener is not implemented in this factory.")
            raise NotImplementedError("Birdeye listener needs implementation.")

        else:
            raise ValueError(f"Unsupported listener type specified: '{listener_type}'")
