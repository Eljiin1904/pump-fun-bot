# src/monitoring/listener_factory.py

from solders.pubkey import Pubkey
from .logs_listener import LogsListener


class ListenerFactory:
    @staticmethod
    def create_listener(
        listener_type: str,
        client,
        token_queue,
        wss_endpoint: str,
        program_id: Pubkey,
        discriminator: str,
    ):
        """
        Factory for WebSocket-based listeners. Currently only 'logs' is supported.
        """
        if listener_type == "logs":
            return LogsListener(
                client=client,
                token_queue=token_queue,
                wss_endpoint=wss_endpoint,
                program_id=program_id,
                discriminator=discriminator,
            )
        else:
            raise ValueError(f"Unknown listener type: {listener_type}")
