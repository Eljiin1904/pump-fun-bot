# src/data/raydium_data.py

import os
import httpx
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from ..utils.logger import get_logger

logger = get_logger(__name__)

# It is recommended to load environment variables centrally (in cli.py),
# but we load here as a fallback.
load_dotenv()

BIRDEYE_OVERVIEW_ENDPOINT = "https://public-api.birdeye.so/public/token_overview"
REQUEST_TIMEOUT = 10.0

async def get_raydium_pool_data(mint_address: str) -> Optional[Dict[str, Any]]:
    """Fetches token market data from Birdeye API."""
    birdeye_api_key = os.getenv("BIRDEYE_API_KEY")
    if not birdeye_api_key:
        logger.error("BIRDEYE_API_KEY not found in environment. Cannot fetch data from Birdeye.")
        return None
    headers = {"X-API-KEY": birdeye_api_key}
    params = {"address": mint_address}
    logger.debug(f"Fetching Birdeye token overview for {mint_address}...")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(BIRDEYE_OVERVIEW_ENDPOINT, params=params, headers=headers)
            if response.status_code == 401:
                logger.error("Birdeye API key unauthorized (HTTP 401). Check your .env file.")
                return None
            response.raise_for_status()
            data = response.json()
            if data.get("success") and isinstance(data.get("data"), dict):
                token_data = data["data"]
                price = token_data.get("price")
                if price is None or not isinstance(price, (int, float)):
                    return None
                liquidity = token_data.get("liquidity")
                volume_24h = token_data.get("v24hUSD")
                market_cap = token_data.get("mc")
                extracted_data = {
                    "price": float(price),
                    "liquidity": float(liquidity) if liquidity else None,
                    "volume_24h": float(volume_24h) if volume_24h else None,
                    "market_cap": float(market_cap) if market_cap else None
                }
                logger.info(f"Birdeye data: Price={extracted_data['price']:.9f} SOL, Liquidity=${extracted_data['liquidity']:,.2f}, 24h Volume=${extracted_data['volume_24h']:,.2f}")
                return extracted_data
            else:
                logger.warning(f"Birdeye API failed for {mint_address}: {data.get('message', 'Unknown error')}")
                return None
    except httpx.TimeoutException:
        logger.error(f"Timeout occurred while fetching data from Birdeye for {mint_address}")
        return None
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching Birdeye data for {mint_address}: {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        logger.error(f"Network error fetching Birdeye data for {mint_address}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching Birdeye data for {mint_address}: {e}", exc_info=True)
        return None
