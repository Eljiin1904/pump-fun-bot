# src/tools/raydium_amm.py

import asyncio
import httpx
import time
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

from solders.pubkey import Pubkey
from solders.instruction import Instruction
from solders.keypair import Keypair  # Import Keypair for type hint

from src.core.client import SolanaClient
from src.core.instruction_builder import InstructionBuilder
from src.core.pubkeys import SolanaProgramAddresses, PumpAddresses  # Need PumpAddresses if used here

# --- CORRECTED IMPORT for raydium_amm_v4 ---
# Assuming these are the necessary components from that file
from src.core.raydium_amm_v4 import make_swap_instruction, SwapAccounts, SwapArgs
# --- END CORRECTION ---

from src.utils.logger import get_logger

logger = get_logger(__name__)
LAMPORTS_PER_SOL = 1_000_000_000
RAYDIUM_LIQUIDITY_ENDPOINT = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"
WSOL_MINT_STR = "So11111111111111111111111111111111111111112"


@dataclass
class RaydiumPoolInfo:
    # ... (Definition from previous response remains unchanged) ...
    id: str
    program_id: str
    base_mint: str
    quote_mint: str
    lp_mint: str
    base_decimals: int
    quote_decimals: int
    lp_decimals: int
    amm_id: Pubkey = field(init=False)
    market_id: str
    base_vault: str
    quote_vault: str
    lp_vault: str
    model_data_account: str
    open_orders: str
    target_orders: str
    withdraw_queue: str
    amm_authority: str
    market_program_id: Optional[str] = None
    market_base_vault: Optional[str] = None
    market_quote_vault: Optional[str] = None
    market_authority: Optional[str] = None
    liquidity_usd: Optional[float] = None
    # --- Add extra_data field to store Serum market accounts ---
    extra_data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.amm_id = Pubkey.from_string(self.id)

    def __str__(self):
        base_sym = self.base_mint[:4] + "..." + self.base_mint[-4:]
        quote_sym = self.quote_mint[:4] + "..." + self.quote_mint[-4:]
        return f"RaydiumPool(Pair={base_sym}/{quote_sym}, ID={self.id[:8]}...)"


class RaydiumAMMInfoFetcher:
    # ... (Implementation from previous response remains unchanged) ...
    def __init__(self, endpoint: str = RAYDIUM_LIQUIDITY_ENDPOINT, request_timeout: int = 10):
        self.endpoint = endpoint
        self.timeout = request_timeout
        self.pools_cache: List[Dict[str, Any]] = []
        self.cache_last_updated: float = 0
        self.cache_ttl_seconds: int = 300

    async def _fetch_all_pools(self) -> Optional[List[Dict[str, Any]]]:
        # ... (fetch logic) ...
        now = time.time()
        if self.pools_cache and (now - self.cache_last_updated < self.cache_ttl_seconds):
            return self.pools_cache
        logger.info(f"Fetching Raydium pool data from {self.endpoint}...")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.endpoint)
                response.raise_for_status()
                data = response.json()
                official_pools = data.get("official", [])
                unofficial_pools = data.get("unOfficial", [])
                all_pools = official_pools + unofficial_pools
                if not all_pools: return None
                self.pools_cache = all_pools
                self.cache_last_updated = now
                logger.info(f"Fetched and cached {len(all_pools)} Raydium pools.")
                return self.pools_cache
        except Exception as e:
            logger.error(f"Error fetching Raydium pools: {e}", exc_info=True)
            return None

    async def get_pool_info_for_token(self, token_mint_str: str) -> List[RaydiumPoolInfo]:
        all_pools_data = await self._fetch_all_pools()
        if not all_pools_data: return []
        found_pools: List[RaydiumPoolInfo] = []
        for pool_data in all_pools_data:
            base_mint = pool_data.get("baseMint")
            quote_mint = pool_data.get("quoteMint")
            if token_mint_str not in [base_mint, quote_mint]: continue
            is_wsol_pair = (base_mint == token_mint_str and quote_mint == WSOL_MINT_STR) or \
                           (quote_mint == token_mint_str and base_mint == WSOL_MINT_STR)
            required_keys = [
                "id", "programId", "baseMint", "quoteMint", "lpMint", "baseDecimals",
                "quoteDecimals", "lpDecimals", "marketId", "baseVault", "quoteVault",
                "lpVault", "modelDataAccount", "openOrders", "targetOrders",
                "withdrawQueue", "authority"
            ]
            if not all(key in pool_data for key in required_keys): continue
            try:
                # --- Store market program/vaults if available ---
                extra_pool_data = {}
                market_program_id = pool_data.get("marketProgramId")
                market_base_vault = pool_data.get("marketBaseVault")
                market_quote_vault = pool_data.get("marketQuoteVault")
                market_authority = pool_data.get("marketAuthority")

                # You might need to fetch serum market details separately if not here
                # Placeholder for serum market accounts needed for swap
                extra_pool_data["serum_bids"] = pool_data.get("marketBids")  # Assuming keys exist in API
                extra_pool_data["serum_asks"] = pool_data.get("marketAsks")  # Assuming keys exist in API
                extra_pool_data["serum_event_queue"] = pool_data.get("marketEventQueue")  # Assuming keys exist in API
                extra_pool_data["serum_vault_signer"] = pool_data.get("vaultSigner")  # Assuming keys exist in API

                pool_info = RaydiumPoolInfo(
                    id=pool_data["id"], program_id=pool_data["programId"], base_mint=pool_data["baseMint"],
                    quote_mint=pool_data["quoteMint"], lp_mint=pool_data["lpMint"],
                    base_decimals=pool_data["baseDecimals"],
                    quote_decimals=pool_data["quoteDecimals"], lp_decimals=pool_data["lpDecimals"],
                    market_id=pool_data["marketId"],
                    base_vault=pool_data["baseVault"], quote_vault=pool_data["quoteVault"],
                    lp_vault=pool_data["lpVault"],
                    model_data_account=pool_data["modelDataAccount"], open_orders=pool_data["openOrders"],
                    target_orders=pool_data["targetOrders"], withdraw_queue=pool_data["withdrawQueue"],
                    amm_authority=pool_data["authority"],
                    market_program_id=market_program_id, market_base_vault=market_base_vault,
                    market_quote_vault=market_quote_vault, market_authority=market_authority,
                    liquidity_usd=pool_data.get("tvl"),
                    extra_data=extra_pool_data  # Store potential serum details
                )
                if is_wsol_pair:
                    found_pools.insert(0, pool_info)
                else:
                    found_pools.append(pool_info)
            except Exception as e:
                logger.error(f"Error creating RaydiumPoolInfo for {pool_data.get('id', 'N/A')}: {e}", exc_info=True)
        # ... (logging and return found_pools) ...
        if not found_pools:
            logger.info(f"No suitable Raydium pool found for token {token_mint_str}")
        elif len(found_pools) > 1:
            logger.info(f"Found {len(found_pools)} pools for {token_mint_str}. Prioritizing: {found_pools[0]}")
        return found_pools


class RaydiumSwapWrapper:
    """Builds instructions for Raydium V4 swaps."""

    def __init__(self, client: SolanaClient):
        self.client = client
        logger.info("RaydiumSwapWrapper initialized.")

    async def _get_serum_market_accounts(self, market_id: Pubkey) -> Optional[Dict[str, Pubkey]]:
        """Fetches and parses Serum market state to find necessary accounts."""
        logger.info(f"Fetching Serum market state for {market_id}...")
        # This requires implementing Serum market state parsing (complex)
        # Or using a library that does it (e.g., Serum-py if available/updated)
        # Placeholder: Return None for now, indicating fetching is needed
        # In a real implementation:
        # market_account_info = await self.client.get_account_info(market_id)
        # if market_account_info and market_account_info.value:
        #     parsed_market_state = parse_serum_market_state(market_account_info.value.data) # Needs parser
        #     if parsed_market_state:
        #         return {
        #             "serum_bids": parsed_market_state.bids,
        #             "serum_asks": parsed_market_state.asks,
        #             "serum_event_queue": parsed_market_state.event_queue,
        #             # serum_base_vault, serum_quote_vault should be available from Raydium API info
        #             # serum_vault_signer needs derivation (PDA: [market_id bytes, vault_signer_nonce bytes])
        #         }
        logger.warning(f"Serum market account fetching for {market_id} is not implemented.")
        return None  # Indicate accounts need fetching / parsing

    async def build_swap_instructions(
            self,
            pool_info: RaydiumPoolInfo,
            owner_keypair: Keypair,  # Use Keypair type hint
            token_in_mint: Pubkey,
            token_out_mint: Pubkey,
            amount_in_lamports: int,
            min_amount_out_lamports: int,
    ) -> List[Instruction]:

        instructions: List[Instruction] = []
        owner_pubkey = owner_keypair.pubkey()

        user_source_ata = InstructionBuilder.get_associated_token_address(owner_pubkey, token_in_mint)
        user_destination_ata = InstructionBuilder.get_associated_token_address(owner_pubkey, token_out_mint)

        dest_ata_info = await self.client.get_account_info(user_destination_ata)
        if dest_ata_info is None or dest_ata_info.value is None:
            logger.info(f"Destination ATA {user_destination_ata} for swap does not exist. Adding create instruction.")
            create_dest_ata_ix = InstructionBuilder.get_create_ata_instruction(
                payer=owner_pubkey, owner=owner_pubkey, mint=token_out_mint, ata_pubkey=user_destination_ata
            )
            instructions.append(create_dest_ata_ix)

        # --- Get Required Serum Market Accounts ---
        serum_market_pk = Pubkey.from_string(pool_info.market_id)
        serum_bids_pk = Pubkey.from_string(pool_info.extra_data.get("serum_bids")) if pool_info.extra_data.get(
            "serum_bids") else None
        serum_asks_pk = Pubkey.from_string(pool_info.extra_data.get("serum_asks")) if pool_info.extra_data.get(
            "serum_asks") else None
        serum_event_queue_pk = Pubkey.from_string(
            pool_info.extra_data.get("serum_event_queue")) if pool_info.extra_data.get("serum_event_queue") else None
        serum_vault_signer_pk = Pubkey.from_string(
            pool_info.extra_data.get("serum_vault_signer")) if pool_info.extra_data.get("serum_vault_signer") else None

        # If essential serum accounts are missing, attempt to fetch/derive them
        # This is a complex step requiring Serum market state parsing.
        if not all([serum_bids_pk, serum_asks_pk, serum_event_queue_pk, serum_vault_signer_pk]):
            logger.warning(
                f"Essential Serum market accounts missing for market {serum_market_pk}. Swap may fail if not fetched/derived.")
            # fetched_serum_accounts = await self._get_serum_market_accounts(serum_market_pk)
            # if fetched_serum_accounts:
            #     serum_bids_pk = fetched_serum_accounts['serum_bids']
            #     # ... etc ...
            # else:
            raise ValueError(
                f"Cannot proceed with swap for pool {pool_info.id}: Missing critical Serum market accounts (bids, asks, event_q, vault_signer). Implement fetching/parsing.")

        # Use optional chaining for market vaults, providing a default invalid Pubkey if None
        # This assumes make_swap_instruction can handle potentially invalid pubkeys if the market program isn't Serum V3
        default_pk = Pubkey.from_string("11111111111111111111111111111111")  # Placeholder
        serum_base_vault_pk = Pubkey.from_string(
            pool_info.market_base_vault) if pool_info.market_base_vault else default_pk
        serum_quote_vault_pk = Pubkey.from_string(
            pool_info.market_quote_vault) if pool_info.market_quote_vault else default_pk

        # Determine pool vaults based on swap direction
        pool_base_vault_pk = Pubkey.from_string(pool_info.base_vault)
        pool_quote_vault_pk = Pubkey.from_string(pool_info.quote_vault)

        pool_vault_in = pool_base_vault_pk if token_in_mint == Pubkey.from_string(
            pool_info.base_mint) else pool_quote_vault_pk
        pool_vault_out = pool_quote_vault_pk if token_out_mint == Pubkey.from_string(
            pool_info.quote_mint) else pool_base_vault_pk

        swap_accounts = SwapAccounts(
            token_program=SolanaProgramAddresses.TOKEN_PROGRAM_ID,
            amm=pool_info.amm_id,
            amm_authority=Pubkey.from_string(pool_info.amm_authority),
            amm_open_orders=Pubkey.from_string(pool_info.open_orders),
            # Raydium V4 often uses the same account for target_orders as open_orders
            amm_target_orders=Pubkey.from_string(pool_info.target_orders),
            pool_coin_token_account=pool_base_vault_pk,  # Use base/quote designation consistently
            pool_pc_token_account=pool_quote_vault_pk,  # Use base/quote designation consistently
            serum_program=Pubkey.from_string(
                pool_info.market_program_id or "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX"),
            serum_market=serum_market_pk,
            serum_bids=serum_bids_pk,
            serum_asks=serum_asks_pk,
            serum_event_queue=serum_event_queue_pk,
            serum_coin_vault_account=serum_base_vault_pk,
            serum_pc_vault_account=serum_quote_vault_pk,
            serum_vault_signer=serum_vault_signer_pk,
            user_source_token_account=user_source_ata,
            user_destination_token_account=user_destination_ata,
            user_source_owner=owner_pubkey,
        )

        swap_args = SwapArgs(
            amount_in=amount_in_lamports,
            min_amount_out=min_amount_out_lamports
        )

        # Call the function imported from src.core.raydium_amm_v4
        swap_ix = make_swap_instruction(
            program_id=Pubkey.from_string(pool_info.program_id),
            accounts=swap_accounts,
            args=swap_args
        )
        instructions.append(swap_ix)

        return instructions