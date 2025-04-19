# src/data/raydium_data.py
"""
Raydium Pool Data Fetching via Birdeye API.
"""
import os
import httpx
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from ..utils.logger import get_logger

logger = get_logger(__name__)

BIRDEYE_OVERVIEW_ENDPOINT = "https://public-api.birdeye.so/public/token_overview"
REQUEST_TIMEOUT = 10.0  # seconds

async def get_raydium_pool_data(mint_address: str) -> Optional[Dict[str, Any]]:
    """
    Fetch Raydium token market data (price, liquidity, volume, market cap) from Birdeye.
    Returns a dict with 'price', 'liquidity', 'volume_24h', 'market_cap', or None if not available.
    """
    # Load API key from environment
    load_dotenv()
    api_key = os.getenv("BIRDEYE_API_KEY")
    if not api_key:
        logger.error("BIRDEYE_API_KEY is not set; cannot fetch Raydium data.")
        return None
    headers = {"X-API-KEY": api_key}
    params = {"address": mint_address}
    logger.debug(f"Fetching Birdeye data for token mint: {mint_address}")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(BIRDEYE_OVERVIEW_ENDPOINT, params=params, headers=headers)
            if response.status_code == 401:
                logger.error("Birdeye API key unauthorized (HTTP 401). Check your API key.")
                return None
            response.raise_for_status()
            data = response.json()
            if data.get("success") and isinstance(data.get("data"), dict):
                token_data = data["data"]
                price = token_data.get("price")
                if price is None or not isinstance(price, (int, float)):
                    logger.warning(f"Birdeye response missing/invalid price for {mint_address}: {price}")
                    return None
                liquidity = token_data.get("liquidity")
                volume_24h = token_data.get("v24hUSD")
                market_cap = token_data.get("mc")
                return {
                    "price": float(price),
                    "liquidity": float(liquidity) if liquidity is not None else None,
                    "volume_24h": float(volume_24h) if volume_24h is not None else None,
                    "market_cap": float(market_cap) if market_cap is not None else None
                }
            else:
                logger.warning(f"No Birdeye data for mint: {mint_address}")
                return None
    except Exception as e:
        logger.error(f"Error fetching Birdeye data for {mint_address}: {e}")
        return None
