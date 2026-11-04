"""Pacote de fontes de coleta de ofertas (sources)."""

from pesca_bot.sources.base import (
    OfferSource,
    RateLimiter,
    SourceAPIError,
    SourceConfigurationError,
    extract_primary_image_url,
    parse_price,
    standardize_offer,
)
from pesca_bot.sources.demo import get_demo_offers
from pesca_bot.sources.mercadolivre import MercadoLivreClient
from pesca_bot.sources.shopee import ShopeeAffiliateClient, build_shopee_signature

__all__ = [
    "MercadoLivreClient",
    "OfferSource",
    "RateLimiter",
    "ShopeeAffiliateClient",
    "SourceAPIError",
    "SourceConfigurationError",
    "build_shopee_signature",
    "extract_primary_image_url",
    "get_demo_offers",
    "parse_price",
    "standardize_offer",
]
