"""Validação de ofertas, imagens, normalização de URLs e remoção de duplicatas.

Este módulo garante que apenas ofertas íntegras cheguem ao banco de dados e ao
Telegram, barrando preços inválidos, links inseguros e anúncios repetidos, além
de validar se o endereço da imagem (`image_url`) é seguro e utilizável.
"""

from dataclasses import replace
import math
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pesca_bot.offers import Offer

# Parâmetros de rastreamento comuns em links de divulgação que mudam o texto
# do link mas apontam para o mesmo produto.
TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
}


def canonicalize_url(url: str) -> str:
    """Padroniza uma URL para facilitar a identificação de links duplicados."""
    if not isinstance(url, str) or not url.strip():
        return ""

    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    path = parsed.path.rstrip("/")
    if not path and netloc:
        path = ""

    filtered_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in TRACKING_QUERY_PARAMS
    ]
    filtered_query.sort()
    query = urlencode(filtered_query)

    return urlunparse((scheme, netloc, path, "", query, ""))


def is_valid_url(url: str) -> bool:
    """Verifica se a URL usa protocolo seguro (`https://`) e possui domínio válido."""
    if not isinstance(url, str) or not url.strip():
        return False

    cleaned = url.strip()
    if " " in cleaned:
        return False

    parsed = urlparse(cleaned)
    return (
        parsed.scheme.lower() == "https"
        and bool(parsed.netloc.strip())
        and "." in parsed.netloc
    )


def is_valid_image_url(image_url: str | None) -> bool:
    """Verifica se a URL da imagem do produto é válida, segura (HTTPS) e utilizável.

    Regras:
    - Deve ser uma string não vazia começando com `https://`;
    - Deve ter domínio válido e um caminho de arquivo (`path`) não vazio;
    - Não deve conter espaços nem marcadores de template (`[`, `]`).
    """
    if not isinstance(image_url, str) or not image_url.strip():
        return False

    cleaned = image_url.strip()
    if any(ch in cleaned for ch in (" ", "[", "]", "<", ">")):
        return False

    parsed = urlparse(cleaned)
    path = parsed.path.strip("/")
    return (
        parsed.scheme.lower() == "https"
        and bool(parsed.netloc.strip())
        and "." in parsed.netloc
        and bool(path)
    )


def _is_valid_number(value: object) -> bool:
    """Verifica se um valor é numérico real (int/float), finito e não booleano."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def is_valid_offer(offer: Offer) -> bool:
    """Valida todos os dados obrigatórios, preços, link e nicho da oferta.

    Observação sobre `original_price`:
    - Se `original_price` for informado (`> 0`), ele deve ser maior ou igual ao
      preço atual (`price`). Caso `price > original_price`, a oferta é rejeitada
      por ser um falso desconto.
    - Se `original_price` for `0.0` ou `None` (ausência de preço de referência),
      a oferta continua válida desde que `price > 0`, sendo divulgada com
      chamada neutra (sem anunciar desconto).
    """
    if not isinstance(offer, Offer):
        return False

    if not isinstance(offer.title, str) or not offer.title.strip():
        return False

    if not isinstance(offer.source, str) or not offer.source.strip():
        return False

    if not _is_valid_number(offer.price) or offer.price <= 0:
        return False

    if offer.original_price is not None:
        if not _is_valid_number(offer.original_price):
            return False
        if offer.original_price < 0:
            return False
        if offer.original_price > 0 and offer.price > offer.original_price:
            return False

    if not is_valid_url(offer.url):
        return False

    return True


def deduplicate_offers(offers: list[Offer]) -> list[Offer]:
    """Remove ofertas duplicadas mantendo sempre a opção de menor preço."""
    by_url: dict[str, Offer] = {}
    by_title_source: dict[tuple[str, str], str] = {}

    for offer in offers:
        canonical_url = canonicalize_url(offer.url)
        if not canonical_url:
            continue

        title_key = " ".join(offer.title.casefold().split())
        source_key = " ".join(offer.source.casefold().split())
        composite_key = (title_key, source_key)

        existing_url_key = by_title_source.get(composite_key, canonical_url)
        target_key = canonical_url if canonical_url in by_url else existing_url_key

        clean_image = (
            offer.image_url.strip()
            if is_valid_image_url(offer.image_url) and offer.image_url
            else None
        )
        normalized_offer = replace(
            offer,
            url=canonical_url,
            image_url=clean_image,
        )

        if target_key not in by_url:
            by_url[target_key] = normalized_offer
            by_title_source[composite_key] = target_key
        else:
            current_best = by_url[target_key]
            if (
                normalized_offer.price < current_best.price
                or (
                    normalized_offer.price == current_best.price
                    and normalized_offer.discount_percent > current_best.discount_percent
                )
            ):
                # Se a nova oferta não tiver imagem mas a anterior tinha do mesmo item, preserva
                if normalized_offer.image_url is None and current_best.image_url is not None:
                    normalized_offer = replace(
                        normalized_offer,
                        image_url=current_best.image_url,
                    )
                by_url[target_key] = normalized_offer

    return list(by_url.values())
