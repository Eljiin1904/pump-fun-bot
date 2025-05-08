# src/core/client.py
# (Showing relevant parts - apply changes to your existing file)

import asyncio
import base64
from typing import Optional, List, Union, overload, Dict, Any  # Added Dict, Any

from solders.pubkey import Pubkey
from solders.rpc.config import RpcAccountInfoConfig, RpcProgramAccountsConfig
# --- REMOVE UiAccountEncoding import ---
# from solders.transaction_status import UiAccountEncoding # Not needed if using strings
# --- REMOVE RpcFilter import ---
# from solders.rpc.filter import RpcFilter # Not needed, use Dict for filters
from solders.transaction_status import UiTransactionEncoding  # KEEP this one for get_transaction default

from solders.rpc.responses import RpcResponseContext, GetAccountInfoResp, GetProgramAccountsResp, \
    GetTransactionResp  # Add GetTransactionResp
from solders.account import Account
from solders.commitment_config import CommitmentLevel, CommitmentConfig
from solders.signature import Signature  # For get_transaction type hint

from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
from solana.exceptions import SolanaRpcException

from src.utils.logger import get_logger  # Use absolute import

logger = get_logger(__name__)


# Helper to convert string commitment to CommitmentLevel enum
def _to_commitment_level(commitment_str: Optional[str]) -> CommitmentLevel:
    # ... (implementation remains the same) ...
    if not commitment_str: return CommitmentLevel.Confirmed
    try:
        return CommitmentLevel[commitment_str.capitalize()]
    except KeyError:
        logger.warning(
            f"Invalid commitment string '{commitment_str}'. Defaulting to Confirmed."); return CommitmentLevel.Confirmed


class SolanaClient:
    # ... ( __init__ method remains the same) ...
    def __init__(self, rpc_endpoint: str):
        self.rpc_endpoint = rpc_endpoint
        self._async_client = SolanaPyAsyncClient(rpc_endpoint)
        logger.info(f"SolanaClient initialized with RPC: {rpc_endpoint}")
        self.default_commitment = CommitmentLevel.Confirmed

    async def get_account_info(
            self,
            pubkey: Pubkey,
            commitment: Optional[Union[str, CommitmentLevel]] = None,
            encoding: str = "base64",  # Use string literal directly
            data_slice: Optional[slice] = None,
            min_context_slot: Optional[int] = None,
    ) -> Optional[GetAccountInfoResp]:
        # ... (implementation remains the same, using string 'encoding') ...
        try:
            commit_level = self.default_commitment
            if isinstance(commitment, CommitmentLevel):
                commit_level = commitment
            elif isinstance(commitment, str):
                commit_level = _to_commitment_level(commitment)
            config = RpcAccountInfoConfig(
                encoding=encoding,  # Pass string directly
                data_slice=data_slice,
                commitment=CommitmentConfig(commit_level),
                min_context_slot=min_context_slot
            )
            resp = await self._async_client.get_account_info(pubkey=pubkey, config=config)
            if resp and resp.value is not None:
                return resp
            else:
                return None
        except SolanaRpcException as e:
            logger.error(f"RPC Exception get_account_info for {pubkey}: {e}"); return None
        except Exception as e:
            logger.error(f"Unexpected Error get_account_info for {pubkey}: {e}", exc_info=True); return None

    @overload
    async def get_account_info_json_parsed(
            self, pubkey: Pubkey, commitment: Optional[Union[str, CommitmentLevel]] = None,
            min_context_slot: Optional[int] = None,
    ) -> Optional[GetAccountInfoResp]:
        ...  # Still use base resp type, parsing is internal

    async def get_account_info_json_parsed(
            self, pubkey: Pubkey, commitment: Optional[Union[str, CommitmentLevel]] = None,
            min_context_slot: Optional[int] = None,
    ) -> Optional[GetAccountInfoResp]:
        """Fetches JSON parsed account info."""
        return await self.get_account_info(
            pubkey=pubkey, commitment=commitment, encoding="jsonParsed", min_context_slot=min_context_slot
        )

    async def get_program_accounts(
            self,
            program_id: Pubkey,
            commitment: Optional[Union[str, CommitmentLevel]] = None,
            encoding: str = "base64",  # Use string literal
            data_slice: Optional[slice] = None,
            data_size: Optional[int] = None,
            # --- Use List[Dict] for filters ---
            memcmp_opts: Optional[List[Dict[str, Any]]] = None,
            # --- End Filter Change ---
            min_context_slot: Optional[int] = None,
    ) -> Optional[GetProgramAccountsResp]:
        # ... (implementation remains the same, using string 'encoding' and Dict filters) ...
        try:
            commit_level = self.default_commitment
            if isinstance(commitment, CommitmentLevel):
                commit_level = commitment
            elif isinstance(commitment, str):
                commit_level = _to_commitment_level(commitment)

            # --- Build filters as list of dicts ---
            filters_list: List[Dict[str, Any]] = []
            if data_size is not None:
                filters_list.append({"dataSize": data_size})
            if memcmp_opts:
                # Assuming memcmp_opts is already list of [{"memcmp": {"offset": X, "bytes": "Y"}}]
                filters_list.extend(memcmp_opts)
                # --- End Filter Building ---

            config = RpcProgramAccountsConfig(
                filters=filters_list if filters_list else None,  # Pass list of dicts
                account_config=RpcAccountInfoConfig(
                    encoding=encoding,  # Pass string directly
                    data_slice=data_slice,
                    commitment=CommitmentConfig(commit_level),
                    min_context_slot=min_context_slot
                ),
                with_context=False
            )
            resp = await self._async_client.get_program_accounts(pubkey=program_id, config=config)
            return resp
        except SolanaRpcException as e:
            logger.error(f"RPC Exception get_program_accounts for {program_id}: {e}"); return None
        except Exception as e:
            logger.error(f"Unexpected Error get_program_accounts for {program_id}: {e}", exc_info=True); return None

    # --- Update get_transaction ---
    async def get_transaction(
            self,
            signature_str: str,
            # --- Use string literal for encoding default ---
            encoding: str = "json",  # Default to json, can be "jsonParsed", "base64"
            # --- End Encoding Change ---
            commitment: Optional[Union[str, CommitmentLevel]] = None,
            max_supported_transaction_version: Optional[int] = 0
    ) -> Optional[GetTransactionResp]:  # Use solders response type
        """Fetches transaction details."""
        logger.debug(f"Fetching tx details for {signature_str} with encoding '{encoding}'")
        try:
            commit_level = self.default_commitment
            if isinstance(commitment, CommitmentLevel):
                commit_level = commitment
            elif isinstance(commitment, str):
                commit_level = _to_commitment_level(commitment)

            sig = Signature.from_string(signature_str)  # Convert str to Signature for the call

            resp = await self._async_client.get_transaction(
                signature=sig,  # Pass Signature object
                encoding=encoding,  # Pass string directly
                commitment=CommitmentConfig(commit_level),
                max_supported_transaction_version=max_supported_transaction_version
            )
            # solders returns RpcResponseContext(RpcContext, Optional[EncodedTransactionWithStatusMeta])
            # Or similar based on encoding. We return the whole object.
            if resp and resp.value:
                return resp
            else:
                logger.warning(f"Transaction {signature_str} not found or error fetching.")
                return None
        except SolanaRpcException as e:
            logger.error(f"RPC Exception get_transaction for {signature_str}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected Error get_transaction for {signature_str}: {e}", exc_info=True)
            return None

    # ... (Other methods: get_balance, get_token_account_balance, get_latest_blockhash,
    #      send_transaction_logic, confirm_transaction_logic, close etc.) ...
    # Ensure these methods also use string literals for encoding where applicable

    async def close(self):
        # ... (implementation remains the same) ...
        logger.info("Closing SolanaClient HTTP session...")
        await self._async_client.close()
        logger.info("SolanaClient session closed.")