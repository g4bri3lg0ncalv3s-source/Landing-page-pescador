"""Integração oficial com a API do Mercado Livre Brasil (site MLB).

Documentação oficial:
- Portal de Desenvolvedores: https://developers.mercadolivre.com.br/
- Obtenção de Token OAuth 2.0: `POST https://api.mercadolibre.com/oauth/token`
- Busca de Produtos de Catálogo (MLB):
  `GET https://api.mercadolibre.com/products/search?status=active&site_id=MLB&q={termo}&limit={limite}`
- Listagem de Ofertas Ativas de um Produto:
  `GET https://api.mercadolibre.com/products/{product_id}/items`
"""

import logging
import os
import threading
from typing import Any, Callable, Protocol

import httpx

from pesca_bot.affiliate import build_mercadolivre_affiliate_url
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

MERCADOLIVRE_API_BASE_URL = "https://api.mercadolibre.com"
MERCADOLIVRE_SITE_ID = "MLB"


class TokenStore(Protocol):
    """Contrato de armazenamento persistente dos tokens OAuth rotativos."""

    def load(self) -> tuple[str, str]:
        """Retorna `(access_token, refresh_token)`."""

    def save(self, access_token: str, refresh_token: str) -> None:
        """Persiste o par de tokens mais recente."""


class MercadoLivreClient:
    """Cliente oficial para consulta de ofertas na API do Mercado Livre (MLB).

    Nunca registra `access_token`, `client_secret` ou `refresh_token` em logs
    ou mensagens de exceção.

    Quando um `token_store` é informado, os tokens renovados automaticamente
    (o `refresh_token` do Mercado Livre é de uso único) são persistidos e
    reutilizados entre reinícios do bot.
    """

    source_name = "Mercado Livre"

    def __init__(
        self,
        access_token: str | None = None,
        app_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        base_url: str = MERCADOLIVRE_API_BASE_URL,
        site_id: str = MERCADOLIVRE_SITE_ID,
        http_client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        affiliate_link_builder: Callable[[str, str], str] | None = None,
        only_discounted: bool = True,
        token_store: TokenStore | None = None,
    ) -> None:
        self._access_token = (
            access_token
            if access_token is not None
            else os.getenv("MERCADOLIVRE_ACCESS_TOKEN", "")
        ).strip()
        self._app_id = (
            app_id if app_id is not None else os.getenv("MERCADOLIVRE_APP_ID", "")
        ).strip()
        self._client_secret = (
            client_secret
            if client_secret is not None
            else os.getenv("MERCADOLIVRE_CLIENT_SECRET", "")
        ).strip()
        self._refresh_token = (
            refresh_token
            if refresh_token is not None
            else os.getenv("MERCADOLIVRE_REFRESH_TOKEN", "")
        ).strip()

        self._token_store = token_store
        if token_store is not None:
            stored_access, stored_refresh = token_store.load()
            if stored_access:
                self._access_token = stored_access.strip()
            if stored_refresh:
                self._refresh_token = stored_refresh.strip()

        self.base_url = base_url.rstrip("/")
        self.site_id = site_id
        self._http_client = http_client
        self._owns_http_client = False
        self._client_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._rate_limiter = rate_limiter or RateLimiter(min_interval_seconds=0.15)
        self._affiliate_link_builder = (
            affiliate_link_builder or build_mercadolivre_affiliate_url
        )
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

    def _has_client_credentials(self) -> bool:
        """Indica se há `APP_ID` e `CLIENT_SECRET` configurados para gerar token."""
        return bool(self._app_id and self._client_secret)

    def _validate_credentials(self) -> None:
        """Verifica se há token OAuth ou par App ID + Client Secret configurados."""
        if not self._access_token and not self._has_client_credentials():
            raise SourceConfigurationError(
                "Credenciais do Mercado Livre ausentes. Configure "
                "(MERCADOLIVRE_APP_ID e MERCADOLIVRE_CLIENT_SECRET) ou "
                "MERCADOLIVRE_ACCESS_TOKEN no arquivo .env."
            )

    def refresh_access_token(self) -> str:
        """Solicita um novo `access_token` ao endpoint oficial `/oauth/token`."""
        if not self._has_client_credentials():
            raise SourceConfigurationError(
                "Para gerar ou renovar o token automaticamente, configure "
                "MERCADOLIVRE_APP_ID e MERCADOLIVRE_CLIENT_SECRET no .env."
            )

        with self._token_lock:
            self._rate_limiter.wait()
            url = f"{self.base_url}/oauth/token"

            if self._refresh_token:
                payload = {
                    "grant_type": "refresh_token",
                    "client_id": self._app_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                }
            else:
                payload = {
                    "grant_type": "client_credentials",
                    "client_id": self._app_id,
                    "client_secret": self._client_secret,
                }

            headers = {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            try:
                response = self._get_client().post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=15.0,
                )
            except httpx.HTTPError as exc:
                logger.error(
                    "Falha de rede ao solicitar token OAuth do Mercado Livre: %s",
                    type(exc).__name__,
                )
                raise SourceAPIError(
                    "Erro de conexão ao autenticar na API do Mercado Livre."
                ) from exc

            if response.status_code >= 400:
                error_code = ""
                try:
                    error_code = str(response.json().get("error") or "")
                except ValueError:
                    pass

                logger.error(
                    "Falha ao obter token OAuth no Mercado Livre (HTTP %s, erro=%s).",
                    response.status_code,
                    error_code or "desconhecido",
                )
                if error_code == "unsupported_grant_type":
                    raise SourceAPIError(
                        "O Mercado Livre recusou 'client_credentials' (unsupported_grant_type). "
                        "Ative a opção 'client_credentials' nas configurações da sua aplicação no "
                        "DevCenter do Mercado Livre."
                    )
                raise SourceAPIError(
                    f"Não foi possível autenticar no Mercado Livre (HTTP {response.status_code}). "
                    "Verifique suas credenciais no .env."
                )

            data = response.json()
            new_token = str(data.get("access_token") or "").strip()
            if not new_token:
                raise SourceAPIError(
                    "A API do Mercado Livre não retornou um access_token válido."
                )

            new_refresh = str(data.get("refresh_token") or "").strip()
            if new_refresh:
                self._refresh_token = new_refresh

            self._access_token = new_token
            if self._token_store is not None:
                self._token_store.save(self._access_token, self._refresh_token)

            logger.info("Token OAuth 2.0 do Mercado Livre obtido/renovado com sucesso.")
            return new_token

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Executa uma requisição GET autenticada na API oficial do Mercado Livre."""
        self._validate_credentials()

        if not self._access_token and self._has_client_credentials():
            self.refresh_access_token()

        self._rate_limiter.wait()

        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
        }
        client = self._get_client()

        try:
            response = client.get(
                url,
                params=params,
                headers=headers,
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            logger.error(
                "Falha de rede ao consultar API do Mercado Livre: %s",
                type(exc).__name__,
            )
            raise SourceAPIError(
                "Erro de conexão ao consultar a API do Mercado Livre."
            ) from exc

        if response.status_code == 401 and self._has_client_credentials():
            logger.info("Token do Mercado Livre expirado (401). Tentando renovação automática...")
            self.refresh_access_token()
            headers["Authorization"] = f"Bearer {self._access_token}"
            self._rate_limiter.wait()
            try:
                response = client.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=15.0,
                )
            except httpx.HTTPError as exc:
                logger.error(
                    "Falha de rede ao repetir consulta na API do Mercado Livre: %s",
                    type(exc).__name__,
                )
                raise SourceAPIError(
                    "Erro de conexão ao consultar a API do Mercado Livre."
                ) from exc

        if response.status_code == 401:
            logger.error("Token OAuth expirado ou inválido na API do Mercado Livre (HTTP 401).")
            raise SourceAPIError(
                "Não autorizado na API do Mercado Livre (HTTP 401). Verifique suas credenciais no .env."
            )

        if response.status_code == 429:
            logger.warning("Limite de requisições (HTTP 429) atingido na API do Mercado Livre.")
            raise SourceAPIError(
                "Limite de requisições excedido na API do Mercado Livre (HTTP 429)."
            )

        if response.status_code == 404:
            logger.debug("Recurso não encontrado na API do Mercado Livre (HTTP 404): %s", path)
            raise SourceAPIError("Erro HTTP 404 na API do Mercado Livre.")

        if response.status_code >= 400:
            logger.error(
                "API do Mercado Livre retornou status HTTP %s.",
                response.status_code,
            )
            raise SourceAPIError(
                f"Erro HTTP {response.status_code} na API do Mercado Livre."
            )

        try:
            return response.json()
        except ValueError as exc:
            raise SourceAPIError(
                "Resposta inválida (não-JSON) da API do Mercado Livre."
            ) from exc

    def fetch_offers(
        self,
        keyword: str,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Offer]:
        """Busca ofertas disponíveis no catálogo do Mercado Livre Brasil (`/products/search`)."""
        clean_keyword = keyword.strip()
        if not clean_keyword:
            return []

        params: dict[str, Any] = {
            "status": "active",
            "site_id": self.site_id,
            "q": clean_keyword,
            "limit": max(1, min(int(limit), 50)),
        }
        if offset > 0:
            params["offset"] = int(offset)

        logger.info(
            "Buscando ofertas no Mercado Livre para o termo '%s' (offset=%s, limit=%s).",
            clean_keyword,
            offset,
            params["limit"],
        )
        data = self._get_json("products/search", params=params)
        results = data.get("results") or []

        offers: list[Offer] = []
        for entry in results:
            if "price" in entry:
                offer = self._parse_item_to_offer(entry)
                if offer is not None:
                    offers.append(offer)
                continue

            product_id = str(entry.get("id") or "").strip()
            product_title = " ".join(str(entry.get("name") or entry.get("title") or "").split())
            if not product_id or not product_title:
                continue

            # Extrai a imagem principal oficial do produto de catálogo (pictures[0]["url"])
            primary_image = extract_primary_image_url(entry)
            offer = self._fetch_best_offer_for_product(
                product_id=product_id,
                product_title=product_title,
                product_image_url=primary_image,
            )
            if offer is not None:
                offers.append(offer)

        logger.info(
            "Mercado Livre retornou %s ofertas válidas para o termo '%s'.",
            len(offers),
            clean_keyword,
        )
        return offers

    def _fetch_best_offer_for_product(
        self,
        product_id: str,
        product_title: str,
        product_image_url: str | None = None,
    ) -> Offer | None:
        """Consulta `/products/{product_id}/items` e seleciona a melhor oferta ativa."""
        try:
            items_data = self._get_json(
                f"products/{product_id}/items",
                params={"limit": 10},
            )
        except SourceAPIError:
            return None

        listings = items_data.get("results") or []
        best_offer: Offer | None = None

        for listing in listings:
            item_id = str(listing.get("item_id") or listing.get("id") or product_id).strip()
            permalink = str(
                listing.get("permalink")
                or f"https://www.mercadolivre.com.br/p/{product_id}"
            ).strip()

            merged: dict[str, Any] = {
                "id": item_id,
                "title": product_title,
                "price": listing.get("price"),
                "original_price": listing.get("original_price"),
                "permalink": permalink,
                "image_url": extract_primary_image_url(listing) or product_image_url,
                "product_key": f"mlb-product:{product_id.upper()}",
            }
            if "available_quantity" in listing:
                merged["available_quantity"] = listing.get("available_quantity")

            candidate = self._parse_item_to_offer(merged)
            if candidate is None:
                continue

            if best_offer is None or candidate.price < best_offer.price:
                best_offer = candidate

        return best_offer

    def fetch_item_details(self, item_id: str) -> Offer | None:
        """Consulta os detalhes completos de um item específico (`/items/{Item_ID}`)."""
        clean_id = item_id.strip()
        if not clean_id:
            raise ValueError("O identificador do item (item_id) não pode estar vazio.")

        data = self._get_json(f"items/{clean_id}")
        return self._parse_item_to_offer(data)

    def _parse_item_to_offer(self, item: dict[str, Any]) -> Offer | None:
        """Converte um registro JSON da API do Mercado Livre no modelo `Offer`."""
        has_explicit_qty = "available_quantity" in item and item.get("available_quantity") is not None
        available_quantity: int | None = None

        if has_explicit_qty:
            try:
                qty = int(item["available_quantity"])
                if qty <= 0:
                    return None
                available_quantity = qty
            except (TypeError, ValueError):
                return None

        item_id = str(item.get("id") or item.get("item_id") or "").strip()
        title = " ".join(str(item.get("title") or item.get("name") or "").split())
        price = parse_price(item.get("price"))
        raw_original = item.get("original_price")
        original_price = parse_price(raw_original) if raw_original is not None else 0.0

        if not item_id or not title or price <= 0:
            return None

        if self.only_discounted:
            if original_price <= price:
                return None
        elif original_price < price:
            original_price = 0.0

        permalink = str(item.get("permalink") or "").strip()
        if not permalink:
            return None

        final_url = (
            self._affiliate_link_builder(permalink, item_id)
            if self._affiliate_link_builder is not None
            else permalink
        )

        image_url = extract_primary_image_url(item)
        product_key = (
            str(item.get("product_key") or "").strip()
            or extract_product_key(permalink, self.source_name)
            or f"mlb-item:{item_id.upper()}"
        )

        return Offer(
            title=title,
            price=price,
            original_price=original_price,
            url=final_url,
            source=self.source_name,
            image_url=image_url,
            available_quantity=available_quantity,
            product_key=product_key,
        )
