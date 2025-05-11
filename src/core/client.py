# src/core/client.py

import asyncio
import logging
from typing import Any, List, Optional, Tuple, Union

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.hash import Hash
from solders.signature import Signature
from solders.solders import Instruction
from solders.transaction import VersionedTransaction
from solders.message import MessageV0

from solders.rpc.config import (
    RpcAccountInfoConfig,
    RpcProgramAccountsConfig,
    RpcSendTransactionConfig,
)
from solders.account_decoder import UiAccountEncoding
from solders.rpc.filter import Memcmp
from solders.rpc.responses import GetTransactionResp, SimulateTransactionResp, SendTransactionResp
from solders.transaction_status import UiTransactionEncoding, EncodedTransactionWithStatusMeta
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment, Confirmed
from solana.rpc.core import RPCException
from solana.rpc.types import TxOpts
from solana.transaction import Transaction as LegacyTransaction

from spl.token.instructions import get_associated_token_address

logger = logging.getLogger(__name__)

# Constants
MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 2


class SolanaClient:
    def __init__(
        self,
        rpc_endpoint: str,
        keypair: Optional[Keypair] = None,
        commitment: Commitment = Confirmed,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        skip_preflight: bool = False,
    ) -> None:
        self.async_client = AsyncClient(
            rpc_endpoint,
            commitment=commitment,
            timeout=timeout_seconds,
        )
        self.keypair = keypair
        self.commitment = commitment
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay_seconds
        self.skip_preflight = skip_preflight
        logger.info(f"SolanaClient initialized with RPC: {rpc_endpoint}")

    async def __aenter__(self) -> "SolanaClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def close(self) -> None:
        """Gracefully close the underlying HTTP session."""
        try:
            await self.async_client.close()
            logger.info("SolanaClient connection closed.")
        except Exception as e:
            logger.warning(f"Error closing SolanaClient: {e}")

    async def get_latest_blockhash(self) -> Optional[Hash]:
        try:
            resp = await self.async_client.get_latest_blockhash(self.commitment)
            return resp.value.blockhash if resp.value else None
        except Exception as e:
            logger.error(f"Error get_latest_blockhash: {e}", exc_info=True)
            return None

    async def get_account_info(
        self,
        pubkey: Pubkey,
        commitment: Optional[Commitment] = None,
        encoding_str: str = "base64",
        data_slice: Any = None,
    ) -> Any:
        """
        Returns whatever AsyncClient.get_account_info returns.
        We'll treat it as 'Any' since RpcResponse isn't a public import.
        """
        enc_map = {
            "base64": UiAccountEncoding.Base64,
            "base64+zstd": UiAccountEncoding.Base64Zstd,
            "jsonparsed": UiAccountEncoding.JsonParsed,
        }
        enum_enc = enc_map.get(encoding_str.lower(), UiAccountEncoding.Base64)
        config = RpcAccountInfoConfig(
            encoding=enum_enc,
            data_slice=data_slice,
            commitment=commitment or self.commitment,
            min_context_slot=None,
        )
        try:
            return await self.async_client.get_account_info(pubkey, config)
        except Exception as e:
            logger.error(f"Error get_account_info for {pubkey}: {e}", exc_info=True)
            return None

    async def get_mint_info(self, mint_pubkey: Pubkey) -> Optional[int]:
        info = await self.get_account_info(mint_pubkey, encoding_str="jsonparsed")
        if info and info.value and info.value.data:
            parsed = info.value.data.parsed
            return int(parsed["info"].get("decimals", 0))
        return None

    async def get_program_accounts(
        self,
        program_id: Pubkey,
        commitment: Optional[Commitment] = None,
        encoding_str: str = "base64",
        filters: Optional[List[dict]] = None,
        data_slice: Any = None,
    ) -> Any:
        enc_map = {
            "base64": UiAccountEncoding.Base64,
            "base64+zstd": UiAccountEncoding.Base64Zstd,
            "jsonparsed": UiAccountEncoding.JsonParsed,
        }
        enum_enc = enc_map.get(encoding_str.lower(), UiAccountEncoding.Base64)
        acct_cfg = RpcAccountInfoConfig(
            encoding=enum_enc,
            data_slice=data_slice,
            min_context_slot=None,
        )

        solders_filters = []
        if filters:
            for f in filters:
                if "memcmp" in f and isinstance(f["memcmp"], dict):
                    solders_filters.append(
                        Memcmp(offset=f["memcmp"]["offset"], bytes=f["memcmp"]["bytes"])
                    )
                # Note: DataSize is not available in solders.rpc.filter

        config = RpcProgramAccountsConfig(
            filters=solders_filters or None,
            account_config=acct_cfg,
        )
        try:
            return await self.async_client.get_program_accounts(
                program_id,
                config=config,
                commitment=commitment or self.commitment,
            )
        except Exception as e:
            logger.error(f"Error get_program_accounts for {program_id}: {e}", exc_info=True)
            return None

    async def get_balance_lamports(self, pubkey: Pubkey) -> Optional[int]:
        try:
            resp = await self.async_client.get_balance(pubkey, self.commitment)
            return resp.value
        except Exception as e:
            logger.error(f"Error get_balance_lamports for {pubkey}: {e}", exc_info=True)
            return None

    async def get_token_account_balance_lamports(self, token_account: Pubkey) -> int:
        try:
            resp = await self.async_client.get_token_account_balance(
                token_account, self.commitment
            )
            return int(resp.value.amount) if resp.value else 0
        except RPCException:
            return 0
        except Exception as e:
            logger.error(f"Error get_token_account_balance: {e}", exc_info=True)
            return 0

    async def get_token_balance_lamports(
        self, owner_pubkey: Pubkey, mint_pubkey: Pubkey
    ) -> Optional[int]:
        ata = get_associated_token_address(owner_pubkey, mint_pubkey)
        return await self.get_token_account_balance_lamports(ata)

    async def get_token_supply(self, mint_pubkey: Pubkey) -> Optional[int]:
        try:
            resp = await self.async_client.get_token_supply(mint_pubkey, self.commitment)
            return int(resp.value.amount) if resp.value else None
        except Exception as e:
            logger.error(f"Error get_token_supply for {mint_pubkey}: {e}", exc_info=True)
            return None

    async def get_transaction(
        self,
        tx_sig_str: str,
        encoding: UiTransactionEncoding = UiTransactionEncoding.JsonParsed,
        max_supported_transaction_version: Optional[int] = None,
    ) -> Optional[EncodedTransactionWithStatusMeta]:
        try:
            sig = Signature.from_string(tx_sig_str)
            params = {"encoding": encoding, "commitment": self.commitment}
            if max_supported_transaction_version is not None:
                params["max_supported_transaction_version"] = max_supported_transaction_version
            resp: GetTransactionResp = await self.async_client.get_transaction(sig, **params)  # type: ignore
            return resp.value
        except Exception as e:
            logger.error(f"Error get_transaction for {tx_sig_str}: {e}", exc_info=True)
            return None

    async def send_transaction(
        self,
        transaction: Union[LegacyTransaction, VersionedTransaction],
        signers: Optional[List[Keypair]] = None,
        opts: Optional[TxOpts] = None,
        max_retries: Optional[int] = None,
        retry_delay_seconds: Optional[int] = None,
    ) -> Optional[Signature]:
        _signers = signers or ([self.keypair] if self.keypair else [])
        if not _signers:
            raise ValueError("Transaction needs signers.")
        _opts = opts or TxOpts(skip_preflight=self.skip_preflight, preflight_commitment=self.commitment)
        retries = max_retries if max_retries is not None else self.max_retries
        delay = retry_delay_seconds if retry_delay_seconds is not None else self.retry_delay

        for attempt in range(retries + 1):
            try:
                if isinstance(transaction, LegacyTransaction) and not transaction.recent_blockhash:
                    bh = await self.get_latest_blockhash()
                    if not bh:
                        raise RPCException("Failed to get blockhash.")
                    transaction.recent_blockhash = bh

                signer_args = (
                    transaction.signers
                    if isinstance(transaction, VersionedTransaction)
                    else _signers
                )
                resp: SendTransactionResp = await self.async_client.send_transaction(
                    transaction, *signer_args, opts=_opts  # type: ignore
                )
                logger.info(f"Transaction sent: {resp.value} (attempt {attempt + 1})")
                return resp.value

            except Exception as err:
                logger.warning(f"Send tx error (attempt {attempt + 1}): {err}")
                if "blockhash" in str(err).lower() and isinstance(transaction, LegacyTransaction):
                    transaction.recent_blockhash = None
            if attempt < retries:
                await asyncio.sleep(delay)

        logger.error(f"Failed to send transaction after {retries + 1} attempts.")
        return None

    async def send_legacy_transaction(
        self,
        instructions: List[Any],
        signers: Optional[List[Keypair]] = None,
        recent_blockhash_str: Optional[str] = None,
        add_memo: Optional[str] = None,
        **kwargs,
    ) -> Optional[Signature]:
        _signers = signers or ([self.keypair] if self.keypair else [])
        if not _signers:
            raise ValueError("Legacy transaction needs signers.")
        payer = _signers[0].pubkey()
        bh = (
            Hash.from_string(recent_blockhash_str)
            if recent_blockhash_str
            else await self.get_latest_blockhash()
        )
        if not bh:
            raise RPCException("Failed to get blockhash.")
        tx = LegacyTransaction(recent_blockhash=bh, fee_payer=payer)
        for instr in instructions:
            tx.add(instr)
        if add_memo:
            tx.add(Instruction(MEMO_PROGRAM_ID, add_memo.encode(), []))
        return await self.send_transaction(tx, _signers, **kwargs)

    async def send_versioned_transaction(
        self,
        instructions: List[Any],
        payer: Keypair,
        add_memo: Optional[str] = None,
        **kwargs,
    ) -> Optional[Signature]:
        tx_ins = list(instructions)
        if add_memo:
            tx_ins.append(Instruction(MEMO_PROGRAM_ID, add_memo.encode(), []))

        bh = await self.get_latest_blockhash()
        if not bh:
            raise RPCException("Failed to get blockhash.")

        msg = MessageV0.compile(
            instructions=tx_ins,
            payer=payer.pubkey(),
            recent_blockhash=bh,
            address_table_lookups=None,
        )
        vtx = VersionedTransaction(msg, [payer])
        return await self.send_transaction(vtx, [payer], **kwargs)

    async def simulate_transaction(
        self,
        transaction: Union[LegacyTransaction, VersionedTransaction],
        signers: Optional[List[Keypair]] = None,
        commitment: Optional[Commitment] = None,
        replace_recent_blockhash: bool = True,
        sim_config: Optional[RpcSendTransactionConfig] = None,
    ) -> Optional[SimulateTransactionResp]:
        _cfg = sim_config or RpcSendTransactionConfig(
            skip_preflight=False,
            preflight_commitment=commitment or self.commitment,
            encoding=UiTransactionEncoding.Base64,
            sig_verify=False,
        )
        if isinstance(transaction, LegacyTransaction):
            _signers = signers or ([self.keypair] if self.keypair else [])
            if not _signers:
                raise ValueError("Legacy tx simulation needs signers.")
            if replace_recent_blockhash or not transaction.recent_blockhash:
                bh = await self.get_latest_blockhash()
                if not bh:
                    raise RPCException("Failed to get blockhash.")
                transaction.recent_blockhash = bh
            return await self.async_client.simulate_transaction(transaction, *_signers, config=_cfg)  # type: ignore
        return await self.async_client.simulate_transaction(transaction, config=_cfg)  # type: ignore

    async def confirm_transaction(
        self,
        tx_sig: Signature,
        commitment: Optional[Commitment] = None,
        sleep_seconds: float = 2.0,
        timeout_seconds: Optional[int] = None,
    ) -> Optional[EncodedTransactionWithStatusMeta]:
        _commit = commitment or self.commitment
        _timeout = timeout_seconds or self.timeout_seconds
        elapsed = 0.0
        while elapsed < _timeout:
            try:
                status_resp = await self.async_client.get_signature_statuses([tx_sig])
                if status_resp.value and status_resp.value[0]:
                    st = status_resp.value[0]
                    if st.err:
                        logger.error(f"Tx {tx_sig} failed: {st.err}")
                        return None
                    if st.confirmation_status in ("confirmed", "finalized"):
                        return await self.get_transaction(str(tx_sig))
            except Exception:
                pass
            await asyncio.sleep(sleep_seconds)
            elapsed += sleep_seconds
        logger.warning(f"Timeout confirming {tx_sig} after {_timeout}s")
        return None

    async def build_and_send_transaction(
        self,
        instructions: List[Any],
        signers: Optional[List[Keypair]] = None,
        add_memo: Optional[str] = None,
        use_versioned_tx: bool = False,
        **kwargs,
    ) -> Tuple[Optional[str], Optional[EncodedTransactionWithStatusMeta]]:
        _signers = signers or ([self.keypair] if self.keypair else [])
        if not _signers:
            raise ValueError("Transaction needs signers.")
        payer = _signers[0]
        sig = None

        if use_versioned_tx:
            sig = await self.send_versioned_transaction(instructions, payer, add_memo=add_memo, **kwargs)
        else:
            sig = await self.send_legacy_transaction(instructions, _signers, add_memo=add_memo, **kwargs)

        if not sig:
            return None, None
        sig_str = str(sig)
        if kwargs.get("confirm_transaction_flag", True):
            details = await self.confirm_transaction(sig, timeout_seconds=kwargs.get("confirmation_timeout_seconds"))
            return sig_str, details
        return sig_str, None
