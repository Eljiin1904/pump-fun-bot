# src/core/client.py

import asyncio
import logging
import base64
from typing import Any, List, Optional, Tuple, Union, Dict

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.hash import Hash
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.instruction import Instruction as SoldersInstruction

from solders.rpc.config import (
    RpcAccountInfoConfig,
    RpcProgramAccountsConfig,
    RpcSendTransactionConfig,
)
from solders.account_decoder import UiAccountEncoding, UiTokenAmount
from solders.rpc.filter import Memcmp
from solders.rpc.responses import (
    GetLatestBlockhashResp,
    GetAccountInfoResp,
    GetProgramAccountsResp,
    GetBalanceResp,
    GetTokenAccountBalanceResp,
    GetTokenSupplyResp,
    GetTokenLargestAccountsResp,
    GetTransactionResp,
    SendTransactionResp,
    SimulateTransactionResp,
)
from solders.transaction_status import UiTransactionEncoding, EncodedTransactionWithStatusMeta
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment, Confirmed
from solana.rpc.core import RPCException
from solana.rpc.types import TxOpts as SolanaPyTxOpts
from solana.transaction import Transaction as SolanaPyLegacyTransaction

from spl.token.instructions import get_associated_token_address

logger = logging.getLogger(__name__)

MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 2


class TokenSupplyInfo:
    def __init__(self, amount: int, decimals: int, ui_amount_string: Optional[str]):
        self.amount = amount
        self.decimals = decimals
        self.ui_amount_string = ui_amount_string


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
        self.rpc_endpoint = rpc_endpoint
        self.async_client = AsyncClient(
            rpc_endpoint, commitment=commitment, timeout=timeout_seconds
        )
        self.keypair = keypair
        self.commitment = commitment
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay_seconds
        self.skip_preflight = skip_preflight
        self.tx_opts = SolanaPyTxOpts(
            skip_preflight=self.skip_preflight, preflight_commitment=self.commitment
        )

        commit_val = (
            self.commitment.value
            if hasattr(self.commitment, "value")
            else str(self.commitment)
        )
        logger.info(f"SolanaClient initialized: {rpc_endpoint} @ {commit_val}")

    async def __aenter__(self) -> "SolanaClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self.async_client.close()
            logger.info("SolanaClient connection closed.")
        except Exception as e:
            logger.warning(f"Error closing SolanaClient: {e}")

    async def get_latest_blockhash(self) -> Optional[Hash]:
        try:
            resp: GetLatestBlockhashResp = await self.async_client.get_latest_blockhash(
                self.commitment
            )
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
    ) -> Optional[GetAccountInfoResp]:
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
            resp: GetAccountInfoResp = await self.async_client.get_account_info(
                pubkey, config
            )
            if resp.error:
                logger.error(
                    f"RPC error get_account_info {pubkey}: {resp.error}"
                )
                return None
            return resp
        except Exception as e:
            logger.error(f"Error get_account_info {pubkey}: {e}", exc_info=True)
            return None

    async def get_program_accounts(
        self,
        program_id: Pubkey,
        commitment: Optional[Commitment] = None,
        encoding_str: str = "base64",
        filters: Optional[List[Dict[str, Any]]] = None,
        data_slice: Any = None,
    ) -> Optional[GetProgramAccountsResp]:
        enc_map = {
            "base64": UiAccountEncoding.Base64,
            "base64+zstd": UiAccountEncoding.Base64Zstd,
            "jsonparsed": UiAccountEncoding.JsonParsed,
        }
        enum_enc = enc_map.get(encoding_str.lower(), UiAccountEncoding.Base64)
        acct_cfg = RpcAccountInfoConfig(encoding=enum_enc, data_slice=data_slice)
        solders_filters: List[Union[int, Memcmp]] = []
        if filters:
            for f in filters:
                if "memcmp" in f and isinstance(f["memcmp"], dict):
                    args = f["memcmp"]
                    b = args["bytes"]
                    if isinstance(b, str):
                        try:
                            b = Pubkey.from_string(b).to_bytes()
                        except ValueError:
                            b = b.encode()
                    solders_filters.append(
                        Memcmp(offset=args["offset"], bytes=b)
                    )
                elif "dataSize" in f:
                    solders_filters.append(int(f["dataSize"]))
        config = RpcProgramAccountsConfig(
            filters=solders_filters or None, account_config=acct_cfg
        )
        try:
            return await self.async_client.get_program_accounts(
                program_id,
                config=config,
                commitment=commitment or self.commitment,
            )
        except Exception as e:
            logger.error(f"Error get_program_accounts {program_id}: {e}", exc_info=True)
            return None

    async def get_balance_lamports(self, pubkey: Pubkey) -> Optional[int]:
        try:
            resp: GetBalanceResp = await self.async_client.get_balance(
                pubkey, self.commitment
            )
            return resp.value
        except Exception as e:
            logger.error(f"Error get_balance {pubkey}: {e}", exc_info=True)
            return None

    async def get_token_account_balance_atoms(
        self, token_account_pk: Pubkey
    ) -> Optional[int]:
        try:
            resp: GetTokenAccountBalanceResp = (
                await self.async_client.get_token_account_balance(
                    token_account_pk, self.commitment
                )
            )
            if resp.value:
                return int(resp.value.amount)
            if resp.error:
                logger.error(f"RPC error get_token_account_balance {token_account_pk}: {resp.error}")
            return None
        except RPCException:
            logger.warning(f"Account not found {token_account_pk}")
            return 0
        except Exception as e:
            logger.error(f"Error get_token_account_balance {token_account_pk}: {e}", exc_info=True)
            return None

    async def get_token_balance_lamports(
        self, owner: Pubkey, mint: Pubkey
    ) -> Optional[int]:
        ata = get_associated_token_address(owner, mint)
        return await self.get_token_account_balance_atoms(ata)

    async def get_mint_decimals(self, mint_pubkey: Pubkey) -> Optional[int]:
        resp = await self.get_account_info(mint_pubkey, encoding_str="jsonparsed")
        if resp and resp.value and isinstance(resp.value.data, dict):
            parsed = resp.value.data.get("parsed", {})
            info = parsed.get("info", {})
            dec = info.get("decimals")
            if isinstance(dec, int):
                return dec
        return None

    async def get_token_supply(
        self, mint_pubkey: Pubkey
    ) -> Optional[TokenSupplyInfo]:
        try:
            resp: GetTokenSupplyResp = await self.async_client.get_token_supply(
                mint_pubkey, self.commitment
            )
            if resp.value:
                ua = resp.value
                return TokenSupplyInfo(
                    amount=int(ua.amount),
                    decimals=ua.decimals,
                    ui_amount_string=ua.ui_amount_string,
                )
            if resp.error:
                logger.error(f"RPC error get_token_supply {mint_pubkey}: {resp.error}")
            return None
        except Exception as e:
            logger.error(f"Error get_token_supply {mint_pubkey}: {e}", exc_info=True)
            return None

    async def get_token_largest_accounts(
        self, mint_pubkey: Pubkey
    ) -> Optional[List[UiTokenAmount]]:
        try:
            resp: GetTokenLargestAccountsResp = (
                await self.async_client.get_token_largest_accounts(
                    mint_pubkey, self.commitment
                )
            )
            if resp.value:
                return resp.value  # List[UiTokenAmount]
            if resp.error:
                logger.error(f"RPC error get_token_largest_accounts {mint_pubkey}: {resp.error}")
            return None
        except Exception as e:
            logger.error(f"Error get_token_largest_accounts {mint_pubkey}: {e}", exc_info=True)
            return None

    async def get_transaction_details(
        self,
        tx_sig_str: str,
        encoding: UiTransactionEncoding = UiTransactionEncoding.JsonParsed,
        max_supported_transaction_version: Optional[int] = 0,
    ) -> Optional[EncodedTransactionWithStatusMeta]:
        try:
            sig = Signature.from_string(tx_sig_str)
            resp: GetTransactionResp = await self.async_client.get_transaction(
                sig,
                encoding=encoding,
                commitment=self.commitment,
                max_supported_transaction_version=max_supported_transaction_version,
            )
            return resp.value
        except Exception as e:
            logger.error(f"Error get_transaction_details {tx_sig_str}: {e}", exc_info=True)
            return None

    async def send_transaction(
        self,
        transaction: Union[SolanaPyLegacyTransaction, VersionedTransaction],
        signers: Optional[List[Keypair]] = None,
        opts: Optional[SolanaPyTxOpts] = None,
        max_retries: Optional[int] = None,
        retry_delay_seconds: Optional[int] = None,
    ) -> Optional[Signature]:
        _signers = signers or ([self.keypair] if self.keypair else [])
        _opts = opts or self.tx_opts
        _retries = max_retries if max_retries is not None else self.max_retries
        _delay = retry_delay_seconds if retry_delay_seconds is not None else self.retry_delay

        for attempt in range(_retries + 1):
            try:
                if isinstance(transaction, SolanaPyLegacyTransaction):
                    if not transaction.recent_blockhash:
                        bh = await self.get_latest_blockhash()
                        if not bh:
                            raise RPCException("Failed to get blockhash")
                        transaction.recent_blockhash = bh
                    resp: SendTransactionResp = await self.async_client.send_transaction(
                        transaction, *_signers, opts=_opts
                    )
                else:
                    resp: SendTransactionResp = await self.async_client.send_transaction(
                        transaction, opts=_opts
                    )
                logger.info(f"Tx sent: {resp.value} (attempt {attempt+1})")
                return resp.value
            except RPCException as err:
                logger.warning(f"RPC send_transaction error (attempt {attempt+1}): {err}")
            except Exception as err:
                logger.warning(f"send_transaction error (attempt {attempt+1}): {err}", exc_info=True)

            if attempt < _retries:
                await asyncio.sleep(_delay)
        logger.error("send_transaction failed after retries")
        return None

    async def confirm_transaction(
        self,
        tx_sig: Signature,
        commitment_override: Optional[Commitment] = None,
        sleep_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        timeout_s_override: Optional[int] = None,
    ) -> Optional[EncodedTransactionWithStatusMeta]:
        _commit = commitment_override or self.commitment
        _timeout = timeout_s_override or self.timeout_seconds
        logger.info(f"Confirming {tx_sig} @ {_commit.value} timeout={_timeout}")
        try:
            result = await asyncio.wait_for(
                self.async_client.confirm_transaction(tx_sig, _commit, sleep_secs=sleep_seconds),
                timeout=_timeout,
            )
            if result.value and result.value[0] and not result.value[0].err:
                return await self.get_transaction_details(str(tx_sig))
        except Exception as e:
            logger.error(f"confirm_transaction error for {tx_sig}: {e}", exc_info=True)
        return None

    async def build_and_send_transaction(
        self,
        instructions: List[SoldersInstruction],
        signers: Optional[List[Keypair]] = None,
        add_memo: Optional[str] = None,
        use_versioned_tx: bool = False,
        **kwargs,
    ) -> Tuple[Optional[str], Optional[EncodedTransactionWithStatusMeta]]:
        _signers = signers or ([self.keypair] if self.keypair else [])
        if not _signers:
            raise ValueError("Transaction needs signers")
        payer = _signers[0]

        tx_instructions = []
        fee_limit = kwargs.get("cu_limit")
        fee_price = kwargs.get("priority_fee_microlamports")
        if fee_price:
            tx_instructions.append(set_compute_unit_price(fee_price))
        if fee_limit:
            tx_instructions.append(set_compute_unit_limit(fee_limit))
        tx_instructions.extend(instructions)
        if add_memo:
            tx_instructions.append(
                SoldersInstruction(
                    MEMO_PROGRAM_ID, add_memo.encode(), []
                )
            )

        bh = await self.get_latest_blockhash()
        if not bh:
            return None, None

        if use_versioned_tx:
            msg = MessageV0.compile(
                instructions=tx_instructions,
                payer=payer.pubkey(),
                recent_blockhash=bh,
                address_table_lookups=None,
            )
            tx = VersionedTransaction(msg, _signers)
            sig = await self.send_transaction(tx, opts=kwargs.get("opts"))
        else:
            legacy = SolanaPyLegacyTransaction(
                recent_blockhash=bh, fee_payer=payer.pubkey()
            )
            for ix in tx_instructions:
                legacy.add(ix)
            sig = await self.send_transaction(legacy, _signers, opts=kwargs.get("opts"))

        if not sig:
            return None, None
        sig_str = str(sig)
        details = None
        if kwargs.get("confirm_transaction_flag", True):
            details = await self.confirm_transaction(
                sig, timeout_s_override=kwargs.get("confirmation_timeout_seconds")
            )
        return sig_str, details
