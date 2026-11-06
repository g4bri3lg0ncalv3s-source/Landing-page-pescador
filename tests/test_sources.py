"""Testes automatizados com respostas simuladas para Shopee e Mercado Livre.

Usa `httpx.MockTransport` para simular respostas das APIs oficiais sem realizar
chamadas reais na internet e sem precisar de credenciais comerciais ativas.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest

from pesca_bot.database import OAuthTokenStore
from pesca_bot.offers import filter_offers
from pesca_bot.sources import (
    MercadoLivreClient,
    RateLimiter,
    ShopeeAffiliateClient,
    SourceAPIError,
    SourceConfigurationError,
    build_shopee_signature,
)


def test_rate_limiter_respeita_intervalo_minimo():
    """Garante que o limitador de requisições aguarde o intervalo configurado."""
    relogio = [100.0]
    esperas: list[float] = []

    def clock() -> float:
        return relogio[0]

    def sleeper(segundos: float) -> None:
        esperas.append(round(segundos, 2))
        relogio[0] += segundos

    limiter = RateLimiter(min_interval_seconds=1.5, clock=clock, sleeper=sleeper)

    # 1ª chamada: imediata
    limiter.wait()
    assert esperas == []

    # 2ª chamada 0.5s depois: deve aguardar os 1.0s restantes
    relogio[0] += 0.5
    limiter.wait()
    assert esperas == [1.0]


def test_shopee_busca_ofertas_e_assina_requisicao(caplog: pytest.LogCaptureFixture):
    """Testa busca productOfferV2, assinatura SHA-256 e proteção de credenciais nos logs."""
    app_id = "app_secreto_123"
    app_secret = "chave_super_secreta_456"
    timestamp_fixo = 1700000000

    headers_recebidos: dict[str, str] = {}
    corpo_recebido: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers_recebidos.update(dict(request.headers))
        corpo_recebido.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "data": {
                    "productOfferV2": {
                        "nodes": [
                            {
                                "itemId": "998877",
                                "productName": "Carretilha Pesca Perfil Baixo 18 Rolamentos",
                                "priceMin": "160.00",
                                "priceMax": "200.00",
                                "priceDiscountRate": 20,
                                "productLink": "https://shopee.com.br/item-i.1.998877",
                                "offerLink": "https://s.shopee.com.br/afiliado123",
                                "shopName": "Pesca Brasil Oficial",
                                "commissionRate": "0.08",
                                "sales": 150,
                            }
                        ]
                    }
                }
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ShopeeAffiliateClient(
        app_id=app_id,
        app_secret=app_secret,
        http_client=http_client,
        rate_limiter=RateLimiter(0.0),
        time_provider=lambda: timestamp_fixo,
    )

    with caplog.at_level(logging.INFO):
        ofertas = client.fetch_offers("carretilha", limit=10)

    # Verifica assinatura SHA-256 oficial no cabeçalho Authorization
    assinatura_esperada = build_shopee_signature(
        app_id=app_id,
        app_secret=app_secret,
        timestamp=timestamp_fixo,
        payload_json=corpo_recebido[0],
    )
    assert headers_recebidos["authorization"] == (
        f"SHA256 Credential={app_id},Timestamp={timestamp_fixo},Signature={assinatura_esperada}"
    )

    # Verifica padronização da oferta retornada e aprovação pelo motor de ofertas
    assert len(ofertas) == 1
    oferta = ofertas[0]
    assert oferta.title == "Carretilha Pesca Perfil Baixo 18 Rolamentos"
    assert oferta.price == 160.0
    assert oferta.original_price == 200.0
    assert oferta.discount_percent == 20.0
    assert oferta.url == "https://s.shopee.com.br/afiliado123"
    assert oferta.source == "Shopee (Pesca Brasil Oficial)"
    assert len(filter_offers(ofertas)) == 1

    # Garante que nenhuma credencial vazou nos logs
    texto_logs = caplog.text
    assert app_id not in texto_logs
    assert app_secret not in texto_logs


def test_shopee_gera_short_link_com_sub_ids():
    """Verifica a geração de link curto de afiliado com subIds de rastreamento."""
    payload_enviado: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload_enviado.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "data": {
                    "generateShortLink": {
                        "shortLink": "https://s.shopee.com.br/curto999"
                    }
                }
            },
        )

    client = ShopeeAffiliateClient(
        app_id="id_teste",
        app_secret="segredo_teste",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(0.0),
    )

    short_url = client.generate_short_link(
        "https://shopee.com.br/vara-de-pesca-i.10.20",
        sub_ids=["telegram", "oferta_dia"],
    )
    assert short_url == "https://s.shopee.com.br/curto999"
    assert payload_enviado["variables"] == {
        "originUrl": "https://shopee.com.br/vara-de-pesca-i.10.20",
        "subIds": ["telegram", "oferta_dia"],
    }


def test_shopee_tratamento_de_erros_e_credenciais_ausentes():
    """Garante erros claros quando faltam credenciais ou quando ocorre HTTP 429."""
    sem_credenciais = ShopeeAffiliateClient(app_id="", app_secret="")
    with pytest.raises(SourceConfigurationError):
        sem_credenciais.fetch_offers("molinete")

    client_429 = ShopeeAffiliateClient(
        app_id="id",
        app_secret="secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(429))
        ),
        rate_limiter=RateLimiter(0.0),
    )
    with pytest.raises(SourceAPIError, match="429"):
        client_429.fetch_offers("molinete")


def test_mercadolivre_busca_ofertas_filtra_estoque_e_desconto(
    caplog: pytest.LogCaptureFixture,
):
    """Testa busca na API MLB, filtragem de estoque/desconto e link de afiliado."""
    token_secreto = "APP_USR-token-super-secreto-999"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {token_secreto}"
        assert request.url.path == "/products/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "MLB111",
                        "title": "Molinete Shimano FX 4000 Pesca",
                        "price": 189.90,
                        "original_price": 249.90,
                        "available_quantity": 5,
                        "permalink": "https://produto.mercadolivre.com.br/MLB-111-molinete",
                    },
                    {
                        # Produto sem desconto (original_price None) -> deve ser ignorado
                        "id": "MLB222",
                        "title": "Vara de pesca sem desconto",
                        "price": 100.0,
                        "original_price": None,
                        "available_quantity": 3,
                        "permalink": "https://produto.mercadolivre.com.br/MLB-222-vara",
                    },
                    {
                        # Produto esgotado (available_quantity 0) -> deve ser ignorado
                        "id": "MLB333",
                        "title": "Isca artificial esgotada",
                        "price": 25.0,
                        "original_price": 50.0,
                        "available_quantity": 0,
                        "permalink": "https://produto.mercadolivre.com.br/MLB-333-isca",
                    },
                ]
            },
        )

    client = MercadoLivreClient(
        access_token=token_secreto,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(0.0),
        affiliate_link_builder=lambda url, item_id: f"{url}?matt_tool=pescabot&item={item_id}",
    )

    with caplog.at_level(logging.INFO):
        ofertas = client.fetch_offers("molinete")

    assert len(ofertas) == 1
    oferta = ofertas[0]
    assert oferta.title == "Molinete Shimano FX 4000 Pesca"
    assert oferta.price == 189.90
    assert oferta.original_price == 249.90
    assert oferta.url == (
        "https://produto.mercadolivre.com.br/MLB-111-molinete?matt_tool=pescabot&item=MLB111"
    )
    assert oferta.source == "Mercado Livre"
    assert len(filter_offers(ofertas)) == 1

    # Confirma que o token OAuth não aparece nos logs
    assert token_secreto not in caplog.text


def test_mercadolivre_tratamento_de_erro_401():
    """Garante erro claro e sem vazar token quando o OAuth retorna HTTP 401."""
    token_secreto = "TOKEN_EXPIRADO_SECRETO"
    client = MercadoLivreClient(
        access_token=token_secreto,
        app_id="",
        client_secret="",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(401))
        ),
        rate_limiter=RateLimiter(0.0),
    )

    with pytest.raises(SourceAPIError, match="401") as exc_info:
        client.fetch_offers("vara de pesca")

    assert token_secreto not in str(exc_info.value)


def test_mercadolivre_gera_token_automaticamente_com_app_id_e_secret():
    """Verifica que tendo APP_ID e CLIENT_SECRET o cliente obtém o access_token sozinho."""
    chamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request.url.path)
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "APP_USR-gerado-automaticamente"},
            )
        assert request.headers["authorization"] == "Bearer APP_USR-gerado-automaticamente"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "MLB777",
                        "title": "Carretilha Marine Sports",
                        "price": 199.0,
                        "original_price": 250.0,
                        "available_quantity": 2,
                        "permalink": "https://produto.mercadolivre.com.br/MLB-777-carretilha",
                    }
                ]
            },
        )

    client = MercadoLivreClient(
        access_token="",
        app_id="123456789",
        client_secret="segredo_app",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(0.0),
    )

    ofertas = client.fetch_offers("carretilha")
    assert chamadas == ["/oauth/token", "/products/search"]
    assert len(ofertas) == 1
    assert ofertas[0].title == "Carretilha Marine Sports"


def test_mercadolivre_aplica_parametros_de_afiliado_do_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """Garante que MERCADOLIVRE_AFFILIATE_WORD e TOOL do .env entrem no link da oferta."""
    monkeypatch.setenv("MERCADOLIVRE_AFFILIATE_WORD", "tralhasecreta")
    monkeypatch.setenv("MERCADOLIVRE_AFFILIATE_TOOL", "99887766")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "MLB777",
                        "title": "Carretilha Marine Sports",
                        "price": 199.0,
                        "original_price": 250.0,
                        "available_quantity": 2,
                        "permalink": "https://www.mercadolivre.com.br/p/MLB777",
                    }
                ]
            },
        )

    client = MercadoLivreClient(
        access_token="APP_USR-teste",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(0.0),
    )

    ofertas = client.fetch_offers("carretilha")
    assert len(ofertas) == 1
    assert (
        ofertas[0].url
        == "https://www.mercadolivre.com.br/p/MLB777?matt_word=tralhasecreta&matt_tool=99887766"
    )


def test_mercadolivre_persiste_refresh_token_rotacionado_no_sqlite(tmp_path: Path):
    """Garante que o novo refresh_token OAuth sobreviva entre instâncias sem editar o .env."""
    banco_tokens = tmp_path / "oauth.db"
    estado_servidor = {
        "valid_access": "ACCESS_RENOVADO_V2",
        "valid_refresh": "REFRESH_SEMENTE_V1",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            body = request.content.decode("utf-8")
            assert f"refresh_token={estado_servidor['valid_refresh']}" in body
            estado_servidor["valid_refresh"] = "REFRESH_ROTACIONADO_V2"
            return httpx.Response(
                200,
                json={
                    "access_token": estado_servidor["valid_access"],
                    "refresh_token": "REFRESH_ROTACIONADO_V2",
                },
            )

        if request.headers.get("authorization") != f"Bearer {estado_servidor['valid_access']}":
            return httpx.Response(401, json={"message": "expired_token"})

        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "MLB901",
                        "title": "Molinete Daiwa Sweepfire Pesca",
                        "price": 159.0,
                        "original_price": 220.0,
                        "available_quantity": 4,
                        "permalink": "https://www.mercadolivre.com.br/p/MLB901",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)

    # 1ª instância inicia com access_token expirado do .env -> renova e grava V2 no SQLite
    store_1 = OAuthTokenStore(
        provider="mercadolivre",
        env_access_token="ACCESS_EXPIRADO_V1",
        env_refresh_token="REFRESH_SEMENTE_V1",
        db_path=banco_tokens,
    )
    client_1 = MercadoLivreClient(
        access_token="ACCESS_EXPIRADO_V1",
        app_id="app123",
        client_secret="sec456",
        refresh_token="REFRESH_SEMENTE_V1",
        http_client=httpx.Client(transport=transport),
        rate_limiter=RateLimiter(0.0),
        token_store=store_1,
    )
    ofertas_1 = client_1.fetch_offers("molinete")
    assert len(ofertas_1) == 1

    # 2ª instância (simulando reinício com o MESMO .env antigo) carrega V2 do SQLite
    store_2 = OAuthTokenStore(
        provider="mercadolivre",
        env_access_token="ACCESS_EXPIRADO_V1",
        env_refresh_token="REFRESH_SEMENTE_V1",
        db_path=banco_tokens,
    )
    access_salvo, refresh_salvo = store_2.load()
    assert access_salvo == "ACCESS_RENOVADO_V2"
    assert refresh_salvo == "REFRESH_ROTACIONADO_V2"

    # Se o usuário substituir manualmente o token no .env, a nova semente prevalece
    store_novo_env = OAuthTokenStore(
        provider="mercadolivre",
        env_access_token="ACCESS_MANUAL_V3",
        env_refresh_token="REFRESH_MANUAL_V3",
        db_path=banco_tokens,
    )
    assert store_novo_env.load() == ("ACCESS_MANUAL_V3", "REFRESH_MANUAL_V3")


def test_shopee_nao_fabrica_falso_desconto_com_pricemax_de_variacoes():
    """Garante que anúncios da Shopee com vários SKUs e discount_rate=0 sejam ignorados."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "productOfferV2": {
                        "nodes": [
                            {
                                "itemId": "111",
                                "productName": "Anzol / Kit Completo Pesca",
                                "priceMin": "8.00",
                                "priceMax": "150.00",
                                "priceDiscountRate": 0,
                                "offerLink": "https://s.shopee.com.br/var111",
                                "shopName": "Loja X",
                            },
                            {
                                "itemId": "222",
                                "productName": "Molinete Shimano FX 2500",
                                "priceMin": "180.00",
                                "priceMax": "180.00",
                                "priceDiscountRate": 20,
                                "offerLink": "https://s.shopee.com.br/real222",
                                "shopName": "Loja Y",
                            },
                        ]
                    }
                }
            },
        )

    client = ShopeeAffiliateClient(
        app_id="id",
        app_secret="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(0.0),
    )

    ofertas = client.fetch_offers("pesca")
    assert len(ofertas) == 1
    assert ofertas[0].title == "Molinete Shimano FX 2500"
    assert ofertas[0].original_price == 225.0
    assert ofertas[0].product_key == "shopee:222"


