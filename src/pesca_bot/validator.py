"""Validação de ofertas, imagens, normalização de URLs e remoção de duplicatas.

Este módulo garante que apenas ofertas íntegras cheguem ao banco de dados e ao
Telegram, barrando preços inválidos, links inseguros e anúncios repetidos, além
de validar se o endereço da imagem (`image_url`) é seguro e utilizável.
"""

from dataclasses import replace
import math
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pesca_bot.offers import Offer

# Parâmetros de rastreamento comuns em links de divulgação que mudam o texto
# do link mas apontam para o mesmo produto.
#
# `matt_word` e `matt_tool` (afiliado do Mercado Livre) são ignorados APENAS na
# chave canônica de deduplicação. O link publicado continua com esses
# parâmetros, preservando a comissão do afiliado.
TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
    "matt_word",
    "matt_tool",
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


# --- Identificação canônica do produto -------------------------------------

_ML_CATALOG_RE = re.compile(r"/p/(MLB\d+)", re.IGNORECASE)
_ML_ITEM_RE = re.compile(r"\bMLB-?(\d+)", re.IGNORECASE)
_SHOPEE_ITEM_RE = re.compile(r"[.-]i\.(\d+)\.(\d+)", re.IGNORECASE)
_SHOPEE_PRODUCT_RE = re.compile(r"/product/(\d+)/(\d+)", re.IGNORECASE)


def extract_product_key(url: str, source: str = "") -> str:
    """Extrai um identificador estável do produto a partir da URL.

    Exemplos:
    - `https://www.mercadolivre.com.br/p/MLB123` -> `mlb-product:MLB123`
    - `https://produto.mercadolivre.com.br/MLB-456-vara` -> `mlb-item:MLB456`
    - `https://shopee.com.br/vara-i.10.20` -> `shopee:20`

    Retorna string vazia quando não for possível identificar o produto
    (ex.: links curtos `https://s.shopee.com.br/...`).
    """
    if not isinstance(url, str) or not url.strip():
        return ""

    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if "mercadolivre" in host or "mercadolibre" in host:
        catalog = _ML_CATALOG_RE.search(path)
        if catalog:
            return f"mlb-product:{catalog.group(1).upper()}"
        item = _ML_ITEM_RE.search(path)
        if item:
            return f"mlb-item:MLB{item.group(1)}"
        return ""

    if "shopee" in host:
        match = _SHOPEE_ITEM_RE.search(path) or _SHOPEE_PRODUCT_RE.search(path)
        if match:
            return f"shopee:{match.group(2)}"
        return ""

    return ""


# --- Filtro de nicho de pesca ----------------------------------------------

# Termos que indicam claramente um artigo de pesca.
FISHING_POSITIVE_TERMS = (
    "pesca",
    "pescaria",
    "pescador",
    "pesqueiro",
    "fishing",
    "molinete",
    "carretilha",
    "isca artificial",
    "iscas artificiais",
    "isca de pesca",
    "anzol",
    "anzois",
    "chumbada",
    "girador",
    "snap pesca",
    "passagua",
    "puca",
    "multifilamento",
    "fluorcarbono",
    "jig head",
    "jumping jig",
    "vara de pesca",
    "vara para molinete",
    "vara para carretilha",
    "vara molinete",
    "vara carretilha",
    "alicate de contencao",
    "alicate pega peixe",
    "boga grip",
    "fish grip",
    "garateia",
    "cevadeira",
    "canico",
    "vara telescopica",
    "estojo de isca",
    "caixa de isca",
    "porta varas",
    "tucunare",
    "tilapia",
    "tambaqui",
    "tambacu",
    "traira",
    "dourado pesca",
    "shimano",
    "daiwa",
    "marine sports",
    "maruri",
    "albatroz",
    "saint plus",
    "lumis",
    "sumax",
    "ygk",
    "vexter",
    "abu garcia",
    "okuma",
    "rapala",
    "monster 3x",
    "deconto",
    "nelson nakamura",
)

# Termos que indicam produtos fora do nicho, mesmo quando o título cita pesca.
FISHING_NEGATIVE_TERMS = (
    "maquiagem",
    "manicure",
    "pedicure",
    "unha",
    "esmalte",
    "batom",
    "cosmetico",
    "bijuteria",
    "bijuterias",
    "artesanato",
    "brinco",
    "bolsa feminina",
    "maternidade",
    "fralda",
    "brinquedo",
    "brinquedo pega peixe",
    "jogo de pescaria",
    "fantasia",
    "aquario",
    "racao",
    "adesivo",
    "quadro decorativo",
    "caneca",
    "chaveiro",
    "rocadeira",
    "eletricista",
    "automotivo",
    "capinha",
    "pelicula",
)


def _normalize_text(text: str) -> str:
    """Remove acentos, normaliza caixa e espaços para comparação de termos."""
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", no_accents.casefold())
    return f" {' '.join(cleaned.split())} "


def _contains_term(normalized_text: str, term: str) -> bool:
    """Verifica se o termo aparece como palavra(s) inteira(s) no texto."""
    normalized_term = _normalize_text(term)
    return normalized_term in normalized_text


def is_fishing_niche(title: str) -> bool:
    """Indica se o título pertence ao nicho de artigos de pesca.

    Regras:
    1. Qualquer termo da lista negativa reprova o produto.
    2. Pelo menos um termo da lista positiva é obrigatório.
    """
    if not isinstance(title, str) or not title.strip():
        return False

    normalized = _normalize_text(title)

    if any(_contains_term(normalized, term) for term in FISHING_NEGATIVE_TERMS):
        return False

    return any(_contains_term(normalized, term) for term in FISHING_POSITIVE_TERMS)


def detect_offer_platform(offer_or_source: Offer | str, url: str = "") -> str:
    """Identifica a plataforma/marketplace canônico da oferta.

    Unifica variações de vendedores dentro do mesmo marketplace (por exemplo,
    `"Shopee (Loja A)"` e `"Shopee (Loja B)"` tornam-se `"shopee"`), evitando
    que múltiplas lojas de um mesmo marketplace monopolizem a fila de postagens.
    """
    if isinstance(offer_or_source, Offer):
        raw_source = offer_or_source.source or ""
        raw_url = url or offer_or_source.url or ""
    else:
        raw_source = offer_or_source or ""
        raw_url = url or ""

    source_norm = " ".join(raw_source.casefold().split())
    url_norm = raw_url.casefold()

    if (
        "mercado livre" in source_norm
        or "mercadolivre" in source_norm
        or "mercadolibre" in source_norm
        or "mercadolivre" in url_norm
        or "mercadolibre" in url_norm
        or "meli.la" in url_norm
    ):
        return "mercadolivre"

    if "shopee" in source_norm or "shopee" in url_norm:
        return "shopee"

    return source_norm or "desconhecido"


def interleave_offers_by_source(
    offers: list[Offer],
    last_platform: str | None = None,
) -> list[Offer]:
    """Intercala ofertas em round-robin entre plataformas (ex.: Mercado Livre e Shopee).

    Mantém a ordem interna de qualidade/desconto de cada plataforma, mas alterna
    1 oferta de cada fonte por rodada. Quando `last_platform` é informado,
    rotaciona a ordem inicial para que a próxima publicação priorize uma
    plataforma diferente da última enviada ao canal.
    """
    if len(offers) <= 1:
        return list(offers)

    buckets: dict[str, list[Offer]] = {}
    for offer in offers:
        platform = detect_offer_platform(offer)
        buckets.setdefault(platform, []).append(offer)

    if len(buckets) <= 1:
        return list(offers)

    platforms = list(buckets.keys())
    if last_platform:
        normalized_last = detect_offer_platform(last_platform)
        if normalized_last in platforms:
            idx = platforms.index(normalized_last)
            platforms = platforms[idx + 1 :] + platforms[: idx + 1]

    interleaved: list[Offer] = []
    round_idx = 0
    while True:
        added_in_round = False
        for platform in platforms:
            bucket = buckets[platform]
            if round_idx < len(bucket):
                interleaved.append(bucket[round_idx])
                added_in_round = True
        if not added_in_round:
            break
        round_idx += 1

    return interleaved


def deduplicate_offers(offers: list[Offer]) -> list[Offer]:
    """Remove ofertas duplicadas mantendo sempre a opção de menor preço.

    Duas ofertas são consideradas o mesmo produto quando compartilham qualquer
    uma destas chaves: URL canônica, identificador do produto (`product_key`)
    ou o par (título, plataforma). O link original da oferta (incluindo parâmetros
    de afiliado) é preservado no resultado.
    """
    best_by_group: dict[str, Offer] = {}
    group_by_key: dict[str, str] = {}

    for offer in offers:
        canonical_url = canonicalize_url(offer.url)
        if not canonical_url:
            continue

        title_key = " ".join(offer.title.casefold().split())
        source_key = detect_offer_platform(offer)
        product_key = (offer.product_key or "").strip() or extract_product_key(
            offer.url, offer.source
        )

        identity_keys = [f"url:{canonical_url}"]
        if product_key:
            identity_keys.append(f"product:{product_key}")
        identity_keys.append(f"title:{title_key}|{source_key}")

        group = next(
            (group_by_key[key] for key in identity_keys if key in group_by_key),
            identity_keys[0],
        )
        for key in identity_keys:
            group_by_key.setdefault(key, group)

        clean_image = (
            offer.image_url.strip()
            if is_valid_image_url(offer.image_url) and offer.image_url
            else None
        )
        normalized_offer = replace(
            offer,
            url=offer.url.strip(),
            image_url=clean_image,
            product_key=product_key or None,
        )

        current_best = best_by_group.get(group)
        if current_best is None:
            best_by_group[group] = normalized_offer
            continue

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
            best_by_group[group] = normalized_offer

    return list(best_by_group.values())
