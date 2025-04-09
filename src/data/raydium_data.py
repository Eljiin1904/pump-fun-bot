# src/data/raydium_data.py

import os
import httpx
from typing import Optional, Dict, Any
from dotenv import load_dotenv # Keep load_dotenv here for potential direct use/testing
from ..utils.logger import get_logger

logger = get_logger(__name__)

# Load environment variables WHEN THE MODULE IS LOADED - may still be too early
# It's better practice to load centrally in the main entry point (cli.py)
# load_dotenv() # REMOVE or comment out load_dotenv() here

# Birdeye API Endpoint
BIRDEYE_OVERVIEW_ENDPOINT = "https://public-api.birdeye.so/public/token_overview"
REQUEST_TIMEOUT = 10.0

async def get_raydium_pool_data(mint_address: str) -> Optional[Dict[str, Any]]:
    """ Fetches token market data from Birdeye API. """

    # --- Get API Key *inside* the function ---
    # This ensures load_dotenv() from cli.py has likely run
    # Run load_dotenv() again just in case, doesn't hurt if called multiple times
    load_dotenv()
    birdeye_api_key = os.getenv("BIRDEYE_API_KEY")
    if not birdeye_api_key:
        # Log error only if key is truly needed and absent
        logger.error("BIRDEYE_API_KEY environment variable not found. Cannot fetch Birdeye data.")
        return None
    # --- End Get API Key ---

    headers = {"X-API-KEY": birdeye_api_key}
    params = {"address": mint_address}

    logger.debug(f"Fetching Birdeye token overview for {mint_address}...")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(BIRDEYE_OVERVIEW_ENDPOINT, params=params, headers=headers)
            # Check for 401 Unauthorized specifically
            if response.status_code == 401:
                 logger.error(f"Birdeye API key is invalid or unauthorized (HTTP 401). Please check .env file.")
                 return None
            response.raise_for_status()
            data = response.json()
            # ... (rest of parsing logic as before) ...
            if data.get("success") and isinstance(data.get("data"), dict):
                token_data = data["data"]; price = token_data.get("price")
                if price is None or not isinstance(price, (int, float)): return None # Basic validation
                liquidity = token_data.get("liquidity"); volume_24h = token_data.get("v24hUSD"); market_cap = token_data.get("mc")
                extracted_data = { "price": float(price), "liquidity": float(liquidity) if liquidity else None, "volume_24h": float(volume_24h) if volume_24h else None, "market_cap": float(market_cap) if market_cap else None }
                logger.info(f"Birdeye data: Px={extracted_data['price']:.9f}, Liq=${extracted_data['liquidity']:,.2f}, Vol24h=${extracted_data['volume_24h']:,.2f}")
                return extracted_data
            else: logger.warning(f"Birdeye API failed {mint_address}: {data.get('message', 'Unknown')}"); return None
    # ... (rest of exception handling as before) ...
    except httpx.TimeoutException: logger.error(f"Timeout Birdeye {mint_address}"); return None
    except httpx.HTTPStatusError as e: logger.error(f"HTTP error Birdeye {mint_address}: {e.response.status_code} - {e.response.text}"); return None
    except httpx.RequestError as e: logger.error(f"Network error Birdeye {mint_address}: {e}"); return None
    except Exception as e: logger.error(f"Error Birdeye {mint_address}: {e}", exc_info=True); return None