# src/monitoring/logs_event_processor.py

from base58 import b58decode
from solders.pubkey import Pubkey
from ..trading.base import TokenInfo


class LogsEventProcessor:
    """
    Parses Anchor-style 'Program log:' entries to extract newly
    created token mints and their bonding-curve addresses.
    """

    def __init__(self, discriminator: str, program_id: Pubkey):
        # e.g. "1b72a94ddeeb6376"
        self.discriminator = discriminator
        self.program_id = program_id

    def extract_new_tokens(self, log_result, match_string, creator_address):
        tokens = []
        for entry in log_result.get("logs", []):
            # event payloads usually begin with the discriminator
            if not entry.startswith(self.discriminator):
                continue

            parts = entry.split()
            if len(parts) < 3:
                continue

            mint_b58, curve_b58 = parts[1], parts[2]
            try:
                mint_bytes = b58decode(mint_b58)
                curve_bytes = b58decode(curve_b58)
                # construct Pubkey directly from raw bytes
                mint_pk = Pubkey(mint_bytes)
                curve_pk = Pubkey(curve_bytes)
            except Exception:
                # catch invalid-base58 or other decode errors
                continue

            ti = TokenInfo(
                mint=mint_pk,
                bonding_curve_address=curve_pk,
                mint_str=mint_b58,
            )

            # filter by match_string (substring) if provided
            if match_string and match_string.lower() not in ti.mint_str.lower():
                continue
            # filter by creator_address if provided
            if creator_address and creator_address != ti.creator_str:
                continue

            tokens.append(ti)

        return tokens
