"""Modelos de ofertas e fachada principal do motor de ofertas.

Este módulo integra todo o motor de ofertas:
- Modelo padronizado `Offer` (com suporte opcional a `image_url`, estoque e vendas);
- Cálculo seguro de descontos (`calculate_discount_percent`) e economia (`calculate_savings_amount`);
- Reexportação da validação (`validator.py`) e copywriting (`copywriting.py`).
"""

from dataclasses import dataclass, replace
import math
from typing import Any


def calculate_discount_percent(price: Any, original_price: Any) -> float:
    """Calcula o percentual de desconto tratando valores inválidos com segurança."""
    if isinstance(price, bool) or isinstance(original_price, bool):
        return 0.0

    try:
        current = float(price)
        original = float(original_price)
    except (TypeError, ValueError):
        return 0.0

    if not (math.isfinite(current) and math.isfinite(original)):
        return 0.0

    if original <= 0 or current <= 0 or current >= original:
        return 0.0

    return round((1 - current / original) * 100, 2)


def calculate_savings_amount(price: Any, original_price: Any) -> float:
    """Calcula o valor economizado em reais (`original_price - price`) com segurança."""
    if isinstance(price, bool) or isinstance(original_price, bool):
        return 0.0

    try:
        current = float(price)
        original = float(original_price)
    except (TypeError, ValueError):
        return 0.0

    if not (math.isfinite(current) and math.isfinite(original)):
        return 0.0

    if original <= 0 or current <= 0 or current >= original:
        return 0.0

    return round(original - current, 2)


@dataclass
class Offer:
    """Representa uma oferta padronizada de artigo de pesca.

    Atributos:
        title: Nome ou título real do anúncio do produto.
        price: Preço atual de venda (R$).
        original_price: Preço original de referência antes do desconto (R$).
                        Use `0.0` quando a fonte não informar preço anterior confiável.
        url: Link direto (HTTPS) para a página do produto ou link de afiliado.
        source: Nome da loja ou marketplace de origem.
        category: Categoria identificada automaticamente (ex.: 'Varas de pesca').
        image_url: URL opcional (HTTPS) da imagem principal do produto.
        available_quantity: Estoque disponível informado pela fonte (opcional).
        sales_count: Volume de vendas informado pela fonte (opcional).
        has_verified_urgency: `True` apenas quando houver promoção com prazo confirmado.
        product_key: Identificador canônico do produto independente de vendedor
                     ou link de afiliado (ex.: `mlb-product:MLB123`, `shopee:998877`).
    """

    title: str
    price: float
    original_price: float = 0.0
    url: str = ""
    source: str = ""
    category: str | None = None
    image_url: str | None = None
    available_quantity: int | None = None
    sales_count: int | None = None
    has_verified_urgency: bool = False
    product_key: str | None = None

    @property
    def discount_percent(self) -> float:
        """Percentual de desconto da oferta (de 0.0 a 100.0)."""
        return calculate_discount_percent(self.price, self.original_price)

    @property
    def savings_amount(self) -> float:
        """Valor economizado em reais (R$). Retorna 0.0 se não houver desconto real."""
        return calculate_savings_amount(self.price, self.original_price)

    @property
    def has_verified_discount(self) -> bool:
        """Indica se há um preço original confiável maior que o preço atual."""
        return self.discount_percent > 0.0 and self.savings_amount > 0.0


# Importa após a definição de Offer para evitar importação circular
from pesca_bot.copywriting import (  # noqa: E402
    CopywritingConfig,
    build_offer_caption,
    format_brl,
)
from pesca_bot.validator import (  # noqa: E402
    canonicalize_url,
    deduplicate_offers,
    detect_offer_platform,
    extract_product_key,
    interleave_offers_by_source,
    is_fishing_niche,
    is_valid_image_url,
    is_valid_offer,
    is_valid_url,
)


def format_offer(
    offer: Offer,
    config: CopywritingConfig | None = None,
) -> str:
    """Formata a mensagem/legenda persuasiva da oferta para envio no Telegram."""
    return build_offer_caption(offer, config=config)


def filter_offers(
    offers: list[Offer],
    min_discount_percent: float = 0.0,
    require_fishing_niche: bool = False,
) -> list[Offer]:
    """Valida, remove duplicatas e ordena do maior ao menor desconto.

    Args:
        offers: Ofertas candidatas.
        min_discount_percent: Desconto real mínimo exigido (%). Quando `> 0`,
            ofertas sem preço original confiável ou com desconto menor são
            descartadas. O padrão `0.0` mantém o comportamento original.
        require_fishing_niche: Quando `True`, descarta produtos cujo título não
            pertence ao nicho de pesca (ver `is_fishing_niche`).
    """
    min_discount = max(0.0, float(min_discount_percent or 0.0))

    valid = [
        replace(
            offer,
            image_url=(
                offer.image_url.strip()
                if is_valid_image_url(offer.image_url) and offer.image_url
                else None
            ),
        )
        for offer in offers
        if is_valid_offer(offer)
        and (min_discount <= 0 or offer.discount_percent >= min_discount)
        and (not require_fishing_niche or is_fishing_niche(offer.title))
    ]

    unique_offers = deduplicate_offers(valid)

    return sorted(
        unique_offers,
        key=lambda offer: (offer.discount_percent, offer.savings_amount),
        reverse=True,
    )


__all__ = [
    "CopywritingConfig",
    "Offer",
    "build_offer_caption",
    "calculate_discount_percent",
    "calculate_savings_amount",
    "canonicalize_url",
    "deduplicate_offers",
    "detect_offer_platform",
    "extract_product_key",
    "filter_offers",
    "format_brl",
    "format_offer",
    "interleave_offers_by_source",
    "is_fishing_niche",
    "is_valid_image_url",
    "is_valid_offer",
    "is_valid_url",
]
