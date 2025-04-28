# src/monitoring/listener_factory.py (Correct args for LogsListener init)

import asyncio
from typing import Optional, Type

# Use absolute imports
try:
    from .logs_listener import LogsListener
    from .base_listener import BaseTokenListener
    from ..core.client import SolanaClient
    from ..utils.logger import get_logger
except ImportError as e:
    print(f"ERROR importing in listener_factory: {e}")
    SolanaClient = object; LogsListener = object; BaseTokenListener = object; get_logger = lambda x: type('dummy', (object,), {'info': print, 'error': print, 'warning': print})()

logger = get_logger(__name__)

class ListenerFactory:
    @staticmethod
    def create_listener(
        listener_type: str,
        client: SolanaClient, # Expects the SolanaClient wrapper
        wss_endpoint: Optional[str] = None # Needed by LogsListener
        ) -> BaseTokenListener:
        """Creates a listener instance based on type."""
        logger.info(f"Creating {listener_type} listener instance...")

        if listener_type.lower() == 'logs':
            if not wss_endpoint:
                 logger.error("WebSocket endpoint (wss_endpoint) is required for 'logs' listener.")
                 raise ValueError("WebSocket endpoint (wss_endpoint) is required for 'logs' listener.")
            logger.info(f"Instantiating LogsListener for endpoint: {wss_endpoint}")
            # --- Pass ONLY client and wss_endpoint to LogsListener.__init__ ---
            return LogsListener(client=client, wss_endpoint=wss_endpoint)

        # Add elif for other listener types if needed
        # elif listener_type.lower() == 'block':
        #     logger.info("Instantiating BlockListener...")
        #     return BlockListener(client=client)

        else:
            logger.error(f"Attempted to create unsupported listener type: {listener_type}")
            raise ValueError(f"Unsupported listener type: {listener_type}")

# --- END OF FILE ---