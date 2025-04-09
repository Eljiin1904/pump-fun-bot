# src/monitoring/listener_factory.py

from typing import Optional

# Use absolute imports assuming factory is in src/monitoring
try:
    from .logs_listener import LogsListener
    # from .block_listener import BlockListener # Uncomment if you implement BlockListener
    from .base_listener import BaseTokenListener # Import base class for type hint
    from ..core.client import SolanaClient # Import SolanaClient for type hint
    from ..core.pubkeys import PumpAddresses # Import for default program ID (though listener handles it)
    from ..utils.logger import get_logger
except ImportError as e:
    print(f"ERROR importing in listener_factory: {e}")
    # Define dummies if necessary for basic parsing, though functionality breaks
    SolanaClient = object; LogsListener = object; BlockListener = object; BaseTokenListener = object; PumpAddresses = object; get_logger = lambda x: type('dummy', (object,), {'info': print, 'error': print, 'warning': print})()

logger = get_logger(__name__)

class ListenerFactory:
    def create_listener(
        self,
        listener_type: str,
        wss_endpoint: str,
        client: SolanaClient # Expect the SolanaClient wrapper instance
        # Optional: program_id_to_monitor could be added later if needed
        ) -> BaseTokenListener: # Return type hint using base class
        """Creates a listener instance based on type."""
        logger.info(f"Creating {listener_type} listener for endpoint: {wss_endpoint}")

        if listener_type.lower() == 'logs':
            # --- Ensure the CORRECT 'client' object is passed ---
            # It receives 'client' as an argument and passes it to LogsListener.
            # LogsListener internally knows which program ID to monitor (PumpAddresses.PROGRAM).
            return LogsListener(wss_endpoint, client)
            # --- End Correction ---

        # elif listener_type.lower() == 'block':
        #     # Example if BlockListener exists
        #     if not hasattr(client, 'rpc_endpoint'):
        #          raise ValueError("BlockListener requires SolanaClient with rpc_endpoint")
        #     return BlockListener(client.rpc_endpoint, client) # Pass RPC and client

        else:
            # Log error before raising for clarity
            logger.error(f"Attempted to create unsupported listener type: {listener_type}")
            raise ValueError(f"Unsupported listener type: {listener_type}")