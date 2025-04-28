# src/monitoring/base_listener.py (Ensuring start method exists)

import asyncio
from abc import ABC, abstractmethod
from typing import Optional

# Use absolute imports
try:
    from ..core.client import SolanaClient
    from ..utils.logger import get_logger
except ImportError as e:
    print(f"ERROR importing in base_listener: {e}")
    # Dummy definitions
    SolanaClient = object; get_logger = lambda x: type('dummy', (object,), {'info': print, 'error': print, 'warning': print, 'debug': print})()

logger = get_logger(__name__)

class BaseTokenListener(ABC):
    def __init__(self, client: SolanaClient):
        self.client = client
        self._stop_event = asyncio.Event() # Internal stop signal

    @abstractmethod
    async def _listen(self, token_queue: asyncio.Queue,
                      match_string: Optional[str] = None,
                      creator_address: Optional[str] = None):
        """Abstract method for the core listening logic."""
        raise NotImplementedError

    async def start(self, token_queue: asyncio.Queue,
                    match_string: Optional[str] = None,
                    creator_address: Optional[str] = None):
        """Starts the listener loop, handles errors and restarts."""
        listener_name = self.__class__.__name__
        logger.info(f"{listener_name} starting...")
        self._stop_event.clear() # Ensure stop event is clear on start
        while not self._stop_event.is_set(): # Loop until stop is called
            try:
                await self._listen(token_queue, match_string, creator_address)
                # If _listen exits normally, it's unexpected unless stop was called during it
                if not self._stop_event.is_set():
                     logger.warning(f"{listener_name} _listen method exited unexpectedly. Restarting...")
                else:
                     logger.info(f"{listener_name} _listen exited after stop signal.")
                     break # Exit while loop if stopped

            except asyncio.CancelledError:
                logger.info(f"{listener_name} received cancellation signal. Stopping.")
                self._stop_event.set() # Ensure loop terminates
                break

            except Exception as e:
                logger.error(f"Error in {listener_name} listener: {e}", exc_info=True)

            # Wait before restarting after an error or unexpected exit, only if not stopped
            if not self._stop_event.is_set():
                 wait_time = 10 # Increased wait on error
                 logger.info(f"Restarting {listener_name} after error/exit (delay {wait_time}s)...")
                 try:
                      await asyncio.wait_for(asyncio.sleep(wait_time), timeout=wait_time + 1)
                 except asyncio.CancelledError:
                      logger.info(f"{listener_name} restart wait cancelled.")
                      self._stop_event.set() # Ensure loop terminates
                      break

        logger.info(f"{listener_name} stopped.")

    async def stop(self):
        """Signals the listener to stop."""
        logger.info(f"Requesting stop for {self.__class__.__name__}...")
        self._stop_event.set()

# --- END OF FILE ---