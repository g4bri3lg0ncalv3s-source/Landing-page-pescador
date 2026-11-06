"""Padronização, seleção de imagem principal, controle de taxa e contrato base.

Este módulo fornece:
1. `parse_price`, `extract_primary_image_url` e `standardize_offer`: conversão
   de dados brutos (incluindo seleção da imagem principal) para `Offer`.
2. `RateLimiter`: limitador de requisições para respeitar os limites das APIs
   oficiais (Shopee e Mercado Livre) sem risco de bloqueio.
3. `OfferSource`: interface base (contrato) para qualquer integração de loja.
"""

import math
import re
import threading
import time
from typing import Any, Callable, Protocol

from pesca_bot.offers import Offer, is_valid_image_url


class OfferSource(Protocol):
    """Contrato (interface) que toda fonte de ofertas deve seguir."""

    source_name: str

    def fetch_offers(self, keyword: str, limit: int = 20) -> list[Offer]:
        """Busca ofertas na fonte externa a partir de uma palavra-chave."""


class SourceConfigurationError(RuntimeError):
    """Erro lançado quando faltam credenciais obrigatórias da fonte no `.env`."""


class SourceAPIError(RuntimeError):
    """Erro lançado quando a API externa retorna falha HTTP, GraphQL ou de rede."""


class RateLimiter:
    """Controla o intervalo mínimo entre requisições para respeitar limites da API.

    É seguro para uso concorrente entre threads (ex.: coletas disparadas por
    `asyncio.to_thread` a partir do loop periódico e de comandos manuais).
    """

    def __init__(
        self,
        min_interval_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._sleeper = sleeper
        self._last_call_time: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Aguarda o tempo necessário antes de permitir a próxima requisição."""
        if self.min_interval_seconds <= 0:
            return

        with self._lock:
            now = self._clock()
            if self._last_call_time is not None:
                elapsed = now - self._last_call_time
                remaining = self.min_interval_seconds - elapsed
                if remaining > 0:
                    self._sleeper(remaining)
                    now = self._clock()

            self._last_call_time = now


def parse_price(raw_value: Any) -> float:
    """Converte um valor bruto (número ou texto) em `float` de forma segura."""
    if raw_value is None or isinstance(raw_value, bool):
        return 0.0

    if isinstance(raw_value, (int, float)):
        value = float(raw_value)
        return value if math.isfinite(value) else 0.0

    if isinstance(raw_value, str):
        cleaned = re.sub(r"[^\d,.-]", "", raw_value.strip())
        if not cleaned:
            return 0.0

        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")

        try:
            value = float(cleaned)
            return value if math.isfinite(value) else 0.0
        except ValueError:
            return 0.0

    return 0.0


def extract_primary_image_url(raw_data: dict[str, Any]) -> str | None:
    """Seleciona a imagem principal válida (`https://`) fornecida pela fonte.

    Ordem de prioridade:
    1. Campos diretos de imagem principal (`image_url`, `imageUrl`, `image`, `imagem`);
    2. Lista de imagens (`pictures` ou `images`), escolhendo a primeira URL HTTPS válida;
    3. Miniatura (`secure_thumbnail` ou `thumbnail`), convertendo `http://` do
       domínio `mlstatic.com` apenas quando houver equivalente `secure_thumbnail`
       ou URL `https://` válida.
    """
    if not isinstance(raw_data, dict):
        return None

    for key in ("image_url", "imageUrl", "image", "imagem", "secure_thumbnail"):
        candidate = raw_data.get(key)
        if isinstance(candidate, str) and is_valid_image_url(candidate):
            return candidate.strip()

    for list_key in ("pictures", "images"):
        pictures = raw_data.get(list_key)
        if isinstance(pictures, (list, tuple)):
            for pic in pictures:
                if isinstance(pic, dict):
                    url = pic.get("secure_url") or pic.get("url") or pic.get("src")
                    if isinstance(url, str) and is_valid_image_url(url):
                        return url.strip()
                elif isinstance(pic, str) and is_valid_image_url(pic):
                    return pic.strip()

    thumbnail = raw_data.get("thumbnail")
    if isinstance(thumbnail, str) and is_valid_image_url(thumbnail):
        return thumbnail.strip()

    return None


def standardize_offer(
    raw_data: dict[str, Any],
    default_source: str = "Fonte desconhecida",
) -> Offer:
    """Converte um dicionário bruto de qualquer fonte no modelo único `Offer`."""
    if not isinstance(raw_data, dict):
        raise TypeError("Os dados brutos da oferta devem ser um dicionário (dict).")

    raw_title = (
        raw_data.get("title")
        or raw_data.get("titulo")
        or raw_data.get("name")
        or raw_data.get("nome")
        or ""
    )
    title = " ".join(str(raw_title).split())

    raw_price = (
        raw_data.get("price")
        if "price" in raw_data
        else raw_data.get("preco", raw_data.get("current_price"))
    )
    price = parse_price(raw_price)

    raw_original = (
        raw_data.get("original_price")
        if "original_price" in raw_data
        else raw_data.get("preco_original", raw_data.get("old_price"))
    )
    original_price = parse_price(raw_original) if raw_original is not None else 0.0

    raw_url = raw_data.get("url") or raw_data.get("link") or ""
    url = str(raw_url).strip()

    raw_source = (
        raw_data.get("source")
        or raw_data.get("loja")
        or raw_data.get("store")
        or default_source
    )
    source = " ".join(str(raw_source).split())

    image_url = extract_primary_image_url(raw_data)

    raw_qty = raw_data.get("available_quantity")
    available_quantity: int | None = None
    if isinstance(raw_qty, int) and not isinstance(raw_qty, bool) and raw_qty >= 0:
        available_quantity = raw_qty

    return Offer(
        title=title,
        price=price,
        original_price=original_price,
        url=url,
        source=source,
        image_url=image_url,
        available_quantity=available_quantity,
    )
