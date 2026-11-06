"""Integração oficial com a Shopee Affiliate Open API (GraphQL).

Documentação oficial:
- Endpoint Brasil: `https://open-api.affiliate.shopee.com.br/graphql`
- Método HTTP: `POST` (JSON UTF-8)
- Autenticação: Cabeçalho `Authorization` assinado com SHA-256:
  `SHA256 Credential={AppId},Timestamp={Timestamp},Signature={Signature}`
  Onde `Signature = SHA256(AppId + Timestamp + PayloadJSON + Secret)`.
"""

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable

import httpx

from pesca_bot.config import load_env
from pesca_bot.offers import Offer, extract_product_key
from pesca_bot.sources.base import (
    RateLimiter,
    SourceAPIError,
    SourceConfigurationError,
    extract_primary_image_url,
    parse_price,
)

logger = logging.getLogger(__name__)

load_env()

SHOPEE_GRAPHQL_ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"


def build_shopee_signature(
    app_id: str,
    app_secret: str,
    timestamp: int,
    payload_json: str,
) -> str:
    """Calcula a assinatura SHA-256 exigida pela API de Afiliados da Shopee."""
    raw = f"{app_id}{timestamp}{payload_json}{app_secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ShopeeAffiliateClient:
    """Cliente oficial para busca de produtos e geração de links na Shopee.

    Nunca expõe `app_id` ou `app_secret` em logs ou mensagens de erro.
    """

    source_name = "Shopee"

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        endpoint: str = SHOPEE_GRAPHQL_ENDPOINT,
        http_client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        time_provider: Callable[[], int] | None = None,
        only_discounted: bool = True,
    ) -> None:
        self._app_id = (
            app_id if app_id is not None else os.getenv("SHOPEE_APP_ID", "")
        ).strip()
        self._app_secret = (
            app_secret if app_secret is not None else os.getenv("SHOPEE_APP_SECRET", "")
        ).strip()
        self.endpoint = endpoint
        self._http_client = http_client
        self._owns_http_client = False
        self._client_lock = threading.Lock()
        self._rate_limiter = rate_limiter or RateLimiter(min_interval_seconds=1.0)
        self._time_provider = time_provider or (lambda: int(time.time()))
        self.only_discounted = only_discounted

    def _get_client(self) -> httpx.Client:
        """Retorna um `httpx.Client` reutilizável (connection pooling)."""
        if self._http_client is None:
            with self._client_lock:
                if self._http_client is None:
                    self._http_client = httpx.Client(timeout=15.0)
                    self._owns_http_client = True
        return self._http_client

    def close(self) -> None:
        """Fecha o cliente HTTP interno (quando criado por esta instância)."""
        with self._client_lock:
            if self._owns_http_client and self._http_client is not None:
                self._http_client.close()
                self._http_client = None
                self._owns_http_client = False

    def _validate_credentials(self) -> None:
        """Garante que as credenciais da Shopee foram configuradas antes da chamada."""
        if not self._app_id or not self._app_secret:
            raise SourceConfigurationError(
                "Credenciais da Shopee ausentes. Configure SHOPEE_APP_ID e "
                "SHOPEE_APP_SECRET no arquivo .env após aprovação no painel "
                "Shopee Open API."
            )

    def _post_graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Envia uma requisição GraphQL assinada com SHA-256 para a Shopee."""
        self._validate_credentials()
        self._rate_limiter.wait()

        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables

        payload_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        timestamp = self._time_provider()
        signature = build_shopee_signature(
            app_id=self._app_id,
            app_secret=self._app_secret,
            timestamp=timestamp,
            payload_json=payload_json,
        )

        headers = {
            "Content-Type": "application/json",
            "Authorization": (
                f"SHA256 Credential={self._app_id},"
                f"Timestamp={timestamp},"
                f"Signature={signature}"
            ),
        }

        try:
            response = self._get_client().post(
                self.endpoint,
                content=payload_json.encode("utf-8"),
                headers=headers,
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            logger.error("Falha de rede ao consultar API da Shopee: %s", type(exc).__name__)
            raise SourceAPIError("Erro de conexão ao consultar a API da Shopee.") from exc

        if response.status_code == 429:
            logger.warning("Limite de requisições (HTTP 429) atingido na API da Shopee.")
            raise SourceAPIError("Limite de requisições excedido na API da Shopee (HTTP 429).")

        if response.status_code >= 400:
            logger.error("API da Shopee retornou status HTTP %s.", response.status_code)
            raise SourceAPIError(f"Erro HTTP {response.status_code} na API da Shopee.")

        try:
            data = response.json()
        except ValueError as exc:
            raise SourceAPIError("Resposta inválida (não-JSON) da API da Shopee.") from exc

        if "errors" in data and data["errors"]:
            first_error = data["errors"][0]
            message = first_error.get("message", "Erro GraphQL desconhecido")
            logger.error("Erro GraphQL retornado pela Shopee: %s", message)
            raise SourceAPIError(f"Erro GraphQL da Shopee: {message}")

        return data.get("data") or {}

    def fetch_offers(self, keyword: str, limit: int = 20, page: int = 1) -> list[Offer]:
        """Busca ofertas de produtos por palavra-chave usando `productOfferV2`."""
        clean_keyword = keyword.strip()
        if not clean_keyword:
            return []

        query = """
        query SearchFishingOffers($keyword: String!, $page: Int!, $limit: Int!) {
          productOfferV2(keyword: $keyword, listType: 0, sortType: 2, page: $page, limit: $limit) {
            nodes {
              itemId
              productName
              imageUrl
              priceMin
              priceMax
              priceDiscountRate
              productLink
              offerLink
              shopName
              commissionRate
              sales
            }
          }
        }
        """
        variables = {
            "keyword": clean_keyword,
            "page": max(1, int(page)),
            "limit": max(1, min(int(limit), 50)),
        }

        logger.info("Buscando ofertas na Shopee para o termo '%s' (página %s).", clean_keyword, page)
        data = self._post_graphql(query=query, variables=variables)
        nodes = (data.get("productOfferV2") or {}).get("nodes") or []

        offers: list[Offer] = []
        for node in nodes:
            offer = self._parse_node_to_offer(
                node,
                only_discounted=self.only_discounted,
            )
            if offer is None:
                continue

            if not str(node.get("offerLink") or "").strip():
                product_link = str(node.get("productLink") or "").strip()
                if product_link:
                    try:
                        short_url = self.generate_short_link(product_link)
                        offer.url = short_url
                    except SourceAPIError:
                        logger.warning(
                            "Falha ao gerar shortLink da Shopee; mantendo productLink."
                        )

            offers.append(offer)

        logger.info("Shopee retornou %s ofertas para o termo '%s'.", len(offers), clean_keyword)
        return offers

    def generate_short_link(
        self,
        origin_url: str,
        sub_ids: list[str] | None = None,
    ) -> str:
        """Gera um link curto rastreável (`https://s.shopee.com.br/...`) via `generateShortLink`."""
        clean_url = origin_url.strip()
        if not clean_url:
            raise ValueError("A URL de origem (origin_url) não pode estar vazia.")

        mutation = """
        mutation CreateShortLink($originUrl: String!, $subIds: [String!]) {
          generateShortLink(input: { originUrl: $originUrl, subIds: $subIds }) {
            shortLink
          }
        }
        """
        variables: dict[str, Any] = {"originUrl": clean_url}
        if sub_ids:
            variables["subIds"] = [str(s).strip() for s in sub_ids[:5] if str(s).strip()]

        data = self._post_graphql(query=mutation, variables=variables)
        short_link = ((data.get("generateShortLink") or {}).get("shortLink") or "").strip()
        if not short_link:
            raise SourceAPIError("A Shopee não retornou o shortLink na resposta.")

        return short_link

    @staticmethod
    def _parse_node_to_offer(
        node: dict[str, Any],
        only_discounted: bool = True,
    ) -> Offer | None:
        """Converte um item do `productOfferV2` no modelo padronizado `Offer`.

        Importante: `priceMax` na Shopee representa o maior preço entre variações
        (SKUs diferentes) do mesmo anúncio e NUNCA deve ser usado como preço
        original de promoção. O desconto real vem exclusivamente de
        `priceDiscountRate`.
        """
        title = " ".join(str(node.get("productName") or "").split())
        price = parse_price(node.get("priceMin"))
        discount_rate = parse_price(node.get("priceDiscountRate"))

        if price <= 0 or not title:
            return None

        if 0 < discount_rate < 100:
            original_price = round(price / (1.0 - (discount_rate / 100.0)), 2)
        else:
            original_price = 0.0

        if only_discounted and original_price <= price:
            return None

        url = str(node.get("offerLink") or node.get("productLink") or "").strip()
        if not url:
            return None

        shop_name = " ".join(str(node.get("shopName") or "").split())
        source_label = f"Shopee ({shop_name})" if shop_name else "Shopee"

        image_url = extract_primary_image_url(node)

        raw_sales = node.get("sales")
        sales_count: int | None = None
        if isinstance(raw_sales, int) and not isinstance(raw_sales, bool) and raw_sales > 0:
            sales_count = raw_sales

        item_id = str(node.get("itemId") or "").strip()
        product_key = (
            f"shopee:{item_id}"
            if item_id
            else (extract_product_key(url, source_label) or None)
        )

        return Offer(
            title=title,
            price=price,
            original_price=original_price,
            url=url,
            source=source_label,
            image_url=image_url,
            sales_count=sales_count,
            product_key=product_key,
        )
