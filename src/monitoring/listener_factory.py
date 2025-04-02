# src/monitoring/listener_factory.py

from .base_listener import BaseTokenListener
from .block_listener import BlockListener  # Assuming this exists
from .logs_listener import LogsListener
from ..core.client import SolanaClient  # Import SolanaClient for type hint
from ..core.pubkeys import PumpAddresses  # Import PumpAddresses
from ..utils.logger import get_logger

logger = get_logger(__name__)

class ListenerFactory:
    """Factory class to create token listener instances."""

    def create_listener(
        self,
        listener_type: str,
        wss_endpoint: str,
        solana_client: SolanaClient # Pass the client wrapper
        # Add other necessary parameters if listeners need them (e.g., program_id)
    ) -> BaseTokenListener:
        """
        Creates a token listener instance based on the specified type.

        Args:
            listener_type: The type of listener ('logs' or 'block').
            wss_endpoint: The WebSocket endpoint URL.
            solana_client: The SolanaClient wrapper instance.
            # program_id: The program ID to monitor (e.g., PumpAddresses.PROGRAM)

        Returns:
            An instance of a class derived from BaseTokenListener.

        Raises:
            ValueError: If an unsupported listener type is provided.
        """
        program_id_to_monitor = PumpAddresses.PROGRAM # Define the program to monitor

        if listener_type.lower() == "logs":
            logger.info(f"Creating LogsListener for endpoint: {wss_endpoint}")
            # Pass necessary arguments to LogsListener constructor
            return LogsListener(wss_endpoint, program_id_to_monitor)
        elif listener_type.lower() == "block":
            logger.info(f"Creating BlockListener for endpoint: {wss_endpoint}")
            # Pass necessary arguments to BlockListener constructor
            # Assuming BlockListener constructor takes wss_endpoint and program_id
            return BlockListener(wss_endpoint, program_id_to_monitor) # Adjust constructor if needed
        else:
            error_msg = f"Unsupported listener type: '{listener_type}'. Choose 'logs' or 'block'."
            logger.error(error_msg)
            raise ValueError(error_msg)