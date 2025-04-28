# src/tools/raydium_amm.py (Corrected Imports and Usage)

import asyncio
import logging
from typing import List, Optional, Dict, Any

# Use absolute imports
try:
    # --- FIX: Import PumpAddresses ---
    from ..core.pubkeys import PumpAddresses
    # REMOVED: from ..core.pubkeys import SolanaProgramAddresses
    # --- END FIX ---
    from ..core.client import SolanaClient # Client is now essential
    from ..utils.logger import get_logger
    # Import necessary Solana types
    from solders.pubkey import Pubkey
except ImportError as e:
     print(f"ERROR importing in raydium_amm: {e}")
     # Dummies
     SolanaClient=object; get_logger=lambda x: type('dummy', (object,), {'info':print, 'error':print, 'warning':print})(); PumpAddresses=object; Pubkey=object;

# logger = get_logger(__name__)
logger = logging.getLogger(__name__) # Use standard logging setup

# Constants (Example - Load actual pools from JSON/API)
# Example: Load from a file provided previously
# from ..data.raydium_data import load_raydium_pools # Assuming helper exists
# RAYDIUM_POOLS_LIST = load_raydium_pools() # Load pool list

# Dummy pool data for demonstration
RAYDIUM_POOLS_LIST = [
    {
        "id": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2", # SOL-USDC main pool
        "baseMint": "So11111111111111111111111111111111111111112",
        "quoteMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "lpMint": "APdnoFFBNcsCqVb3tPHyYjRBc1PEJ3nBwGdZDNcJsCHM",
        "baseDecimals": 9,
        "quoteDecimals": 6,
        "lpDecimals": 9,
        "version": 4,
        "programId": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        "authority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
        "openOrders": "J8uEngVD5MGDkSgAsBHuXgWhRvGnNKNuUJKSg9urPFqw",
        "targetOrders": "GNqdCaFvxV1iNtDmweRdyXDNHAkJ53wQ1pEk54pLhb7e",
        "baseVault": "5h6hJGxNwA4DRr9mAwkL2KsvP3Q7DzzD1HMMDPjxBEQG",
        "quoteVault": "9aCUMxNZ83Z6nLpb7nvTszL4zRzEdQ1tGTDXdeU7KFkF",
        "withdrawQueue": "8z41P5d8o4HzCAFv1NqAmLiF7cfKrMDrfN5YQrECcBP",
        "lpVault": "FWEyQ9N58Whvg1tF1kNmYNFpHS2EqGPYuS2L2gxPLIED",
        "marketVersion": 3,
        "marketProgramId": "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",
        "marketId": "9wFFyRfZBsuAha4YcuxcXLKwMxJR43S7fPfQLusDBgy8",
        "marketAuthority": "FaxAbEVVtU2SfQykAouPckTUQJ4gp5Xe7kZgtAvYUNZm",
        "marketBaseVault": "GDmqUGVFMC1NCsH8sQcN1gV3C7j9WgL6K85V5RNVKVG",
        "marketQuoteVault": "AbviYmMp2fHg4HCgYY51sRBbXFbKvxnNuPWfh3MwyAG8",
        "marketBids": "CXMRrGEme4j1rkUGzEpGJ5n4atciie3XzYJc6MYWkgQb",
        "marketAsks": "27Z1oJ71D8Z79KESNvMvJ4u1sXW6rvZLFvoNaEm6Mndz",
        "marketEventQueue": "8BnEgHoWFysVcuFFX7QztDmzuHc9bnGA9iZChA9BGpb6",
        "lookupTableAccount": "D1XZWaCSaMyyWtMR78rDnHwyRNJ9CUuVPCr3rLwWDBPt"
    },
    # Add more pools...
]

# Pre-process pools for faster lookup by mint
POOLS_BY_MINT: Dict[str, List[Dict]] = {}
for pool in RAYDIUM_POOLS_LIST:
    base = pool.get("baseMint")
    quote = pool.get("quoteMint")
    if base: POOLS_BY_MINT.setdefault(base, []).append(pool)
    if quote: POOLS_BY_MINT.setdefault(quote, []).append(pool)

logger.info(f"Processed {len(RAYDIUM_POOLS_LIST)} Raydium pools into lookup map.")


class RaydiumAMMInfoFetcher:
    """ Fetches AMM info for a given token mint from Raydium. """

    def __init__(self, client: SolanaClient, pool_data: Optional[List[Dict]] = None):
        self.client = client
        # Use provided pool data or default internal list
        self.pool_data = pool_data if pool_data is not None else RAYDIUM_POOLS_LIST
        # Build lookup map for faster searching
        self.pools_by_mint: Dict[str, List[Dict]] = {}
        for pool in self.pool_data:
             base = pool.get("baseMint")
             quote = pool.get("quoteMint")
             if base: self.pools_by_mint.setdefault(base, []).append(pool)
             if quote: self.pools_by_mint.setdefault(quote, []).append(pool)
        logger.info(f"RaydiumAMMInfoFetcher initialized with {len(self.pool_data)} pools.")


    async def find_pools_for_token(self, token_mint_address: str) -> List[Dict]:
        """ Finds potential Raydium liquidity pools involving the given token mint. """
        potential_pools = self.pools_by_mint.get(token_mint_address, [])
        logger.debug(f"Found {len(potential_pools)} potential pools for mint {token_mint_address} in static data.")
        return potential_pools

    async def get_pool_info_with_market_data(self, token_mint_address: str) -> Optional[Dict]:
        """
        Finds the most relevant pool (e.g., vs SOL or USDC) and fetches market data.
        Returns pool info dictionary augmented with market details if found.
        """
        token_mint = Pubkey.from_string(token_mint_address)
        potential_pools = await self.find_pools_for_token(token_mint_address)

        if not potential_pools:
            logger.info(f"No potential Raydium pools found for {token_mint_address} in configured list.")
            return None

        # Prioritize pools against SOL or USDC
        target_pool = None
        # --- FIX: Use constants from PumpAddresses ---
        sol_mint_str = str(PumpAddresses.SOL_MINT)
        usdc_mint_str = str(PumpAddresses.USDC_MINT)
        # --- END FIX ---

        for pool in potential_pools:
            is_base = pool.get("baseMint") == token_mint_address
            other_mint = pool.get("quoteMint") if is_base else pool.get("baseMint")
            if other_mint == sol_mint_str:
                target_pool = pool
                logger.info(f"Found SOL pool for {token_mint_address}: {pool.get('id')}")
                break # Found SOL pool, prioritize this
            elif other_mint == usdc_mint_str:
                target_pool = pool # Found USDC pool, keep searching for SOL
                logger.info(f"Found USDC pool for {token_mint_address}: {pool.get('id')}")


        if not target_pool:
             target_pool = potential_pools[0] # Fallback to the first found pool
             logger.warning(f"No SOL/USDC pool found for {token_mint_address}, using first found pool: {target_pool.get('id')}")


        # Optional: Fetch additional on-chain data for the pool if needed
        # e.g., get reserves from baseVault and quoteVault
        # pool_id = target_pool.get("id")
        # base_vault_addr = target_pool.get("baseVault")
        # quote_vault_addr = target_pool.get("quoteVault")
        # if pool_id and base_vault_addr and quote_vault_addr:
        #     try:
        #         # Fetch vault balances (careful with rate limits)
        #         # base_balance = await self.client.get_token_account_balance(Pubkey.from_string(base_vault_addr))
        #         # quote_balance = await self.client.get_token_account_balance(Pubkey.from_string(quote_vault_addr))
        #         # target_pool['baseReserves'] = base_balance.amount if base_balance else '0'
        #         # target_pool['quoteReserves'] = quote_balance.amount if quote_balance else '0'
        #         pass # Placeholder for actual fetching
        #     except Exception as e:
        #          logger.error(f"Failed to fetch vault balances for pool {pool_id}: {e}")


        # For now, just return the pool info found in the static list
        return target_pool


# --- END OF FILE ---