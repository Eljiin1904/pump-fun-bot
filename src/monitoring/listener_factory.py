# src/monitoring/listener_factory.py (Corrected Version from Response #35)
from typing import Optional

# Core imports
from src.core.client import SolanaClient
from src.utils.logger import get_logger

# Listener imports
from .base_listener import BaseTokenListener
from .logs_listener import LogsListener
from .logs_event_processor import LogsEventProcessor

# Import other potential listener types if they exist

logger = get_logger(__name__)


class ListenerFactory:
    """
    Factory class responsible for creating instances of different token listeners.
    It ensures that listeners are instantiated with their required dependencies.
    """

    @staticmethod
    def create_listener(
            listener_type: str,
            client: SolanaClient,  # The main SolanaClient wrapper instance is now required
            wss_endpoint: str,
    ) -> BaseTokenListener:
        """
        Creates and returns an instance of a specific listener based on type.
        # ... (rest of docstring) ...
        """
        logger.info(f"Creating listener of type: '{listener_type}'")

        if listener_type == "logs":
            if not client:
                raise ValueError(
                    "SolanaClient instance is required to initialize LogsEventProcessor for the 'logs' listener.")

            processor = LogsEventProcessor(client=client)

            # --- THIS IS THE CRITICAL LINE ---
            # It correctly passes 'client=client' to the LogsListener constructor
            logger.debug(
                f"FACTORY DEBUG: Preparing to call LogsListener with wss={wss_endpoint}, proc={type(processor).__name__}, client={type(client).__name__}")  # Added debug from suggestion
            return LogsListener(wss_endpoint=wss_endpoint, processor=processor, client=client)
            # --- END CRITICAL LINE ---

        elif listener_type == "birdeye":
            logger.error("Birdeye listener is not implemented in this factory.")
            raise NotImplementedError("Birdeye listener needs implementation.")

        else:
            raise ValueError(f"Unsupported listener type specified: '{listener_type}'")