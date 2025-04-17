# src/tools/raydium_amm.py

import httpx
import asyncio
import struct
import math # Needed for floor operation
from typing import Optional, Dict, Any, List, Tuple

from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from spl.token.instructions import get_associated_token_address, create_associated_token_account
from solana.rpc.types import TokenAccountOpts # For get_token_account_balance

from ..core.client import SolanaClient # Client is now essential
from ..utils.logger import get_logger
from ..core.pubkeys import SolanaProgramAddresses # Import your program addresses

logger = get_logger(__name__)

# --- Constants ---
RAYDIUM_LP_V4_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
SERUM_DEX_PROGRAM_ID = Pubkey.from_string("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX") # Verify this ID
WSOL_ADDRESS = Pubkey.from_string("So11111111111111111111111111111111111111112")
RAYDIUM_LIQUIDITY_JSON_URL = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"
# Raydium Fee Numerator / Denominator (0.25% fee --> 25 / 10000, but input amount is reduced)
# Input amount retains 99.75% of its value for calculation (9975 / 10000)
FEE_NUMERATOR = 25
FEE_DENOMINATOR = 10000
INPUT_AMOUNT_RETAIN_NUMERATOR = 9975 # 10000 - 25
INPUT_AMOUNT_RETAIN_DENOMINATOR = 10000

# Required keys from API/Pool Info for V4 Swap
# Includes the crucial market accounts
POOL_INFO_REQUIRED_KEYS = [
    "id", # AMM ID
    "authority",
    "baseMint",
    "quoteMint",
    "lpMint",
    "baseVault",
    "quoteVault",
    "marketId",
    "openOrders",
    "marketBaseVault", # Market Keys Start Here
    "marketQuoteVault",
    "marketAuthority", # Vault Signer
    "marketBids",
    "marketAsks",
    "marketEventQueue"
]


class RaydiumAMMInfoFetcher:
    """Fetches and validates Raydium AMM pool information using Raydium's API."""

    _instance = None
    _lock = asyncio.Lock()
    _pool_data: Optional[Dict[str, Any]] = None
    _last_fetch_time: float = 0
    _cache_duration: int = 300 # Cache pool data for 5 minutes

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(RaydiumAMMInfoFetcher, cls).__new__(cls)
        return cls._instance

    def __init__(self, client: SolanaClient):
        # Client is needed if on-chain fallbacks are ever implemented
        self.client = client
        logger.info("RaydiumAMMInfoFetcher initialized (using API).")

    async def _fetch_and_cache_pool_data(self) -> bool:
        current_time = asyncio.get_event_loop().time()
        async with self._lock:
            if self._pool_data is not None and (current_time - self._last_fetch_time) < self._cache_duration:
                logger.debug("Using cached Raydium pool data.")
                return True

            logger.info(f"Fetching updated Raydium pool data from API: {RAYDIUM_LIQUIDITY_JSON_URL}")
            try:
                async with httpx.AsyncClient(timeout=20.0) as http_client:
                    response = await http_client.get(RAYDIUM_LIQUIDITY_JSON_URL)
                    response.raise_for_status()
                    data = response.json()

                if 'official' not in data:
                    logger.error("Raydium API response format unexpected (missing 'official' key).")
                    self._pool_data = None
                    return False

                processed_pools = {}
                pool_count = 0
                for pool in data.get('official', []):
                    pool_id = pool.get('id')
                    base_mint = pool.get('baseMint')
                    quote_mint = pool.get('quoteMint')
                    if not pool_id or not base_mint or not quote_mint: continue

                    # Store the raw pool dict by ID
                    processed_pools[pool_id] = pool
                    pool_count += 1

                    # Index by mints for quick lookup
                    if base_mint not in processed_pools: processed_pools[base_mint] = []
                    if quote_mint not in processed_pools: processed_pools[quote_mint] = []
                    processed_pools[base_mint].append(pool)
                    processed_pools[quote_mint].append(pool)

                # TODO: Optionally process 'unOfficial' pools if needed

                self._pool_data = processed_pools
                self._last_fetch_time = current_time
                logger.info(f"Successfully fetched and cached {pool_count} official Raydium pools.")
                return True

            except httpx.RequestError as e:
                logger.error(f"HTTP error fetching Raydium pool data: {e}")
                self._pool_data = None
                return False
            except Exception as e:
                logger.error(f"Error processing Raydium pool data: {e}", exc_info=True)
                self._pool_data = None
                return False

    async def get_pool_info_for_token(self, token_mint_address: str) -> Optional[Dict[str, Any]]:
        """
        Finds VALIDATED Raydium pool info for a given token, prioritizing WSOL pairs.
        Ensures all required keys for V4 swap are present.
        """
        if not await self._fetch_and_cache_pool_data() or self._pool_data is None:
            logger.error("Cannot get pool info: Failed to fetch or cache Raydium pool data.")
            return None

        target_mint = token_mint_address
        wsol_mint = WSOL_ADDRESS.to_base58()
        potential_pools = self._pool_data.get(target_mint, [])

        if not potential_pools:
            logger.warning(f"No pools found involving token {target_mint} in Raydium API data.")
            return None

        # Find the best pool (WSOL pair) and validate its keys
        best_pool_info = None
        for pool in potential_pools:
            is_wsol_pair = False
            base_mint = pool.get('baseMint')
            quote_mint = pool.get('quoteMint')

            if (base_mint == target_mint and quote_mint == wsol_mint) or \
               (base_mint == wsol_mint and quote_mint == target_mint):
                is_wsol_pair = True

            # --- Key Validation (#2 Implemented Here) ---
            missing_keys = [key for key in POOL_INFO_REQUIRED_KEYS if key not in pool or not pool[key]]
            if missing_keys:
                logger.debug(f"Pool {pool.get('id')} for {target_mint} is missing required keys: {missing_keys}. Skipping.")
                continue # Skip this pool, it's unusable for V4 swap

            # If it's a WSOL pair and valid, prioritize it
            if is_wsol_pair:
                logger.info(f"Found VALID WSOL pair pool for {target_mint}: {pool.get('id')}")
                best_pool_info = pool
                break # Found the best option

            # If no WSOL pair found yet, keep the first valid pool as a fallback
            if best_pool_info is None:
                 logger.debug(f"Found valid non-WSOL pool for {target_mint}: {pool.get('id')}. Keeping as fallback.")
                 best_pool_info = pool
        # --- End Validation ---

        if best_pool_info is None:
            logger.warning(f"Could not find a VALID Raydium pool with all required keys for token {target_mint}.")
            return None

        # Return a structured dictionary using expected keys
        # (Renaming API keys slightly for consistency if desired, but using API keys directly is fine)
        return {
            "amm_id": best_pool_info.get('id'),
            "amm_authority": best_pool_info.get('authority'),
            "base_mint": best_pool_info.get('baseMint'),
            "quote_mint": best_pool_info.get('quoteMint'),
            "lp_mint": best_pool_info.get('lpMint'),
            "base_vault": best_pool_info.get('baseVault'),
            "quote_vault": best_pool_info.get('quoteVault'),
            "market_id": best_pool_info.get('marketId'),
            "open_orders": best_pool_info.get('openOrders'),
            "market_base_vault": best_pool_info.get('marketBaseVault'),
            "market_quote_vault": best_pool_info.get('marketQuoteVault'),
            "market_authority": best_pool_info.get('marketAuthority'),
            "market_bids": best_pool_info.get('marketBids'),
            "market_asks": best_pool_info.get('marketAsks'),
            "market_event_queue": best_pool_info.get('marketEventQueue'),
            "version": best_pool_info.get('version', 4) # Assume V4
        }


class RaydiumSwapWrapper:
    """Builds Raydium AMM V4 swap instructions with amount calculation."""

    def __init__(self):
        logger.info("RaydiumSwapWrapper initialized.")

    async def _get_vault_balances(self, client: SolanaClient, base_vault_pk: Pubkey, quote_vault_pk: Pubkey) -> Optional[Tuple[int, int]]:
        """Fetches token balances for base and quote vaults concurrently."""
        logger.debug(f"Fetching vault balances: Base={base_vault_pk}, Quote={quote_vault_pk}")
        try:
            opts = TokenAccountOpts(encoding="jsonParsed") # Or "base64" and decode manually
            # Fetch concurrently
            results = await asyncio.gather(
                client.get_async_client().get_token_account_balance(base_vault_pk, commitment="confirmed"),
                client.get_async_client().get_token_account_balance(quote_vault_pk, commitment="confirmed"),
                return_exceptions=True # Allow one to fail without stopping the other
            )

            base_resp, quote_resp = results

            if isinstance(base_resp, Exception) or not hasattr(base_resp, 'value') or base_resp.value is None:
                logger.error(f"Failed to get base vault balance ({base_vault_pk}): {base_resp}")
                return None
            if isinstance(quote_resp, Exception) or not hasattr(quote_resp, 'value') or quote_resp.value is None:
                logger.error(f"Failed to get quote vault balance ({quote_vault_pk}): {quote_resp}")
                return None

            base_balance = int(base_resp.value.amount)
            quote_balance = int(quote_resp.value.amount)
            logger.debug(f"Vault balances fetched: Base={base_balance}, Quote={quote_balance}")
            return base_balance, quote_balance

        except Exception as e:
            logger.error(f"Error fetching vault balances: {e}", exc_info=True)
            return None

    def _calculate_amount_out(self, reserve_in: int, reserve_out: int, amount_in: int) -> int:
        """Calculates the expected amount out based on reserves and input, accounting for Raydium fee."""
        if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
            return 0

        # Apply fee to input amount (Raydium takes 0.25%)
        amount_in_post_fee = (amount_in * INPUT_AMOUNT_RETAIN_NUMERATOR) // INPUT_AMOUNT_RETAIN_DENOMINATOR

        # Calculate amount out using constant product formula on post-fee input
        numerator = reserve_out * amount_in_post_fee
        denominator = reserve_in + amount_in_post_fee

        if denominator == 0: return 0 # Avoid division by zero

        amount_out = numerator // denominator
        return int(amount_out) # Ensure integer result (lamports)


    async def build_swap_instructions(
        self,
        client: SolanaClient, # Pass the client object
        pool_info: Dict[str, Any],
        input_mint: Pubkey,
        output_mint: Pubkey, # Typically WSOL when selling
        amount_in: int,
        slippage_bps: int,
        owner_pubkey: Pubkey
    ) -> Optional[List[Instruction]]:
        """
        Builds the Raydium AMM V4 swap instruction, including min_amount_out calculation.
        """
        logger.debug(f"Building Raydium swap: {amount_in} lamports of {input_mint} for {output_mint} in pool {pool_info.get('amm_id')}")

        # --- Pool Info Validation (Done by Fetcher, but double-check is cheap) ---
        missing_keys = [key for key in POOL_INFO_REQUIRED_KEYS if key not in pool_info or not pool_info[key]]
        if missing_keys:
            logger.error(f"Cannot build swap: Missing required pool_info keys: {missing_keys}. Pool data from fetcher was invalid.")
            return None

        # --- Determine Base/Quote relative to Input/Output & Get Vault Pks ---
        base_vault_pk = Pubkey.from_string(pool_info["base_vault"])
        quote_vault_pk = Pubkey.from_string(pool_info["quote_vault"])
        pool_base_mint_str = pool_info["base_mint"]
        pool_quote_mint_str = pool_info["quote_mint"]
        input_mint_str = str(input_mint)

        if input_mint_str == pool_base_mint_str:
            reserve_in_vault = base_vault_pk
            reserve_out_vault = quote_vault_pk
            logger.debug("Input is BASE token")
        elif input_mint_str == pool_quote_mint_str:
            reserve_in_vault = quote_vault_pk
            reserve_out_vault = base_vault_pk
            logger.debug("Input is QUOTE token")
        else:
            logger.error(f"Input mint {input_mint} does not match pool's base ({pool_base_mint_str}) or quote ({pool_quote_mint_str}) mint.")
            return None

        # --- Fetch Live Vault Balances (#1 - Fetching Reserves) ---
        balances = await self._get_vault_balances(client, reserve_in_vault, reserve_out_vault)
        if balances is None:
            logger.error("Failed to fetch vault balances, cannot calculate amount out.")
            return None
        reserve_in_balance, reserve_out_balance = balances

        # --- Calculate Expected Amount Out (#1 - Calculation) ---
        expected_amount_out = self._calculate_amount_out(reserve_in_balance, reserve_out_balance, amount_in)
        if expected_amount_out <= 0:
            logger.error(f"Calculated expected_amount_out is zero or negative ({expected_amount_out}) based on reserves {reserve_in_balance}/{reserve_out_balance} and input {amount_in}. Check calculation or reserves.")
            # Allow proceeding with min_amount_out = 0? Or fail? Failing is safer.
            return None
        logger.debug(f"Calculated expected amount out (post-fee): {expected_amount_out}")

        # --- Apply Slippage (#1 - Slippage) ---
        # slippage_bps is in basis points (e.g., 50 = 0.5%)
        min_amount_out = math.floor(expected_amount_out * (10000 - slippage_bps) / 10000)

        if min_amount_out < 0: min_amount_out = 0 # Should not happen if expected > 0
        logger.info(f"Calculated min_amount_out (slippage {slippage_bps/100}%): {min_amount_out}")

        if amount_in <= 0:
            logger.error("Amount in must be positive.")
            return None

        # --- Get User ATAs ---
        user_source_ata = get_associated_token_address(owner_pubkey, input_mint)
        user_destination_ata = get_associated_token_address(owner_pubkey, output_mint)

        # --- Instruction Data ---
        SWAP_DISCRIMINATOR = 9
        instruction_data = struct.pack("<BQQ", SWAP_DISCRIMINATOR, amount_in, min_amount_out) # Use calculated min_amount_out

        # --- Account Metas (Order verified as standard for V4) ---
        accounts = [
            AccountMeta(pubkey=SolanaProgramAddresses.TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["amm_id"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["amm_authority"]), is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["open_orders"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["amm_id"]), is_signer=False, is_writable=True), # Target Orders = AMM ID (Standard)
            AccountMeta(pubkey=Pubkey.from_string(pool_info["base_vault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["quote_vault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SERUM_DEX_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["market_id"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["market_bids"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["market_asks"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["market_event_queue"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["market_base_vault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["market_quote_vault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_info["market_authority"]), is_signer=False, is_writable=False),
            AccountMeta(pubkey=user_source_ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_destination_ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner_pubkey, is_signer=True, is_writable=False),
        ]

        swap_instruction = Instruction(
            program_id=RAYDIUM_LP_V4_PROGRAM_ID,
            data=instruction_data,
            accounts=accounts
        )

        instructions = []

        # --- Check if Destination ATA needs creation ---
        if output_mint != WSOL_ADDRESS: # Don't need to create WSOL ATA
            try:
                logger.debug(f"Checking if destination ATA {user_destination_ata} exists for mint {output_mint}")
                # Use the passed client instance
                acc_info_resp = await client.get_async_client().get_account_info(user_destination_ata, commitment="confirmed")
                if acc_info_resp.value is None:
                    logger.info(f"Destination ATA {user_destination_ata} does not exist. Adding create instruction.")
                    create_dest_ata_ix = create_associated_token_account(
                        payer=owner_pubkey,
                        owner=owner_pubkey,
                        mint=output_mint
                    )
                    instructions.append(create_dest_ata_ix)
            except Exception as e:
                 logger.error(f"Failed to check destination ATA {user_destination_ata} existence: {e}. Tx might fail if it doesn't exist.")
                 # Decide whether to fail the build or proceed cautiously
                 # return None # Safer to fail if check fails

        instructions.append(swap_instruction)

        # --- SOL/WSOL Handling ---
        # Since we are selling TO WSOL, we primarily need the WSOL ATA to exist.
        # If we were selling FROM SOL, wrapping instructions would be needed here.
        # If we wanted native SOL output, unwrapping instructions would be needed AFTER the swap.

        logger.info(f"Successfully built Raydium swap instruction(s) for pool {pool_info.get('amm_id')}")
        return instructions