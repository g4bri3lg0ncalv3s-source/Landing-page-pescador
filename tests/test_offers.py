"""Testes automatizados para o motor de ofertas e persistência SQLite.

Para executar estes testes no terminal, use:
    .\\.venv\\Scripts\\python.exe -m pytest -v
"""

from pathlib import Path

from pesca_bot.database import get_price_history, initialize_database, save_offer
from pesca_bot.offers import (
    Offer,
    calculate_discount_percent,
    calculate_savings_amount,
    filter_offers,
    is_valid_image_url,
    is_valid_offer,
)
from pesca_bot.sources import (
    extract_primary_image_url,
    get_demo_offers,
    parse_price,
    standardize_offer,
)
from pesca_bot.validator import canonicalize_url, deduplicate_offers, is_valid_url


def test_calcula_desconto_e_economia():
    """Cenário 4: Verifica cálculo correto de percentual de desconto e economia em reais."""
    offer = Offer(
        title="Vara de pesca",
        price=80.0,
        original_price=100.0,
        url="https://loja.example/vara",
        source="Loja teste",
    )

    assert offer.discount_percent == 20.0
    assert offer.savings_amount == 20.0
    assert offer.has_verified_discount is True


def test_calcula_desconto_com_valores_invalidos():
    """Garante que valores inválidos de preço retornem 0.0 sem quebrar o sistema."""
    assert calculate_discount_percent(100.0, 0.0) == 0.0
    assert calculate_discount_percent(-10.0, 100.0) == 0.0
    assert calculate_discount_percent(120.0, 100.0) == 0.0
    assert calculate_discount_percent(None, 100.0) == 0.0
    assert calculate_discount_percent("invalido", 100.0) == 0.0
    assert calculate_discount_percent(float("nan"), 100.0) == 0.0
    assert calculate_discount_percent(50.0, float("inf")) == 0.0
    assert calculate_savings_amount(120.0, 100.0) == 0.0


def test_padroniza_produtos_e_seleciona_imagem_principal():
    """Testa padronização de fontes variadas e escolha da imagem principal de uma lista."""
    fonte_a = {
        "titulo": "  Molinete   Marine Sports  ",
        "preco": "R$ 149,90",
        "preco_original": "R$ 199,90",
        "link": "https://loja-a.example/molinete",
        "loja": "Loja A",
        "pictures": [
            {"url": "http://insegura.example/img1.jpg"},
            {"url": "https://cdn.loja-a.example/img-principal.jpg"},
            {"url": "https://cdn.loja-a.example/img-secundaria.jpg"},
        ],
    }
    fonte_b = {
        "name": "Carretilha Perfil Baixo",
        "current_price": "1.200,00",
        "old_price": "1.500,00",
        "url": "https://loja-b.example/carretilha",
        "imageUrl": "https://cdn.loja-b.example/carretilha.png",
    }

    oferta_a = standardize_offer(fonte_a)
    oferta_b = standardize_offer(fonte_b, default_source="Loja B")

    assert oferta_a.title == "Molinete Marine Sports"
    assert oferta_a.price == 149.90
    assert oferta_a.original_price == 199.90
    assert oferta_a.source == "Loja A"
    assert oferta_a.image_url == "https://cdn.loja-a.example/img-principal.jpg"

    assert oferta_b.title == "Carretilha Perfil Baixo"
    assert oferta_b.price == 1200.00
    assert oferta_b.original_price == 1500.00
    assert oferta_b.source == "Loja B"
    assert oferta_b.image_url == "https://cdn.loja-b.example/carretilha.png"
    assert parse_price("texto sem numero") == 0.0
    assert extract_primary_image_url({"image_url": "url-invalida"}) is None


def test_rejeita_preco_maior_que_original():
    """Garante que falsas promoções (preço atual maior que o original) sejam barradas."""
    offer = Offer(
        title="Vara de pesca",
        price=120.0,
        original_price=100.0,
        url="https://loja.example/vara",
        source="Loja teste",
    )

    assert not is_valid_offer(offer)


def test_valida_urls_imagens_e_campos_obrigatorios():
    """Garante validação de URLs de oferta e de imagens."""
    assert is_valid_url("https://loja.example/item") is True
    assert is_valid_url("http://loja.example/item") is False
    assert is_valid_url(" url-invalida ") is False

    assert is_valid_image_url("https://cdn.example.com/foto.jpg") is True
    assert is_valid_image_url("http://cdn.example.com/foto.jpg") is False
    assert is_valid_image_url("https://cdn.example.com/") is False
    assert is_valid_image_url(None) is False

    oferta_sem_loja = Offer(
        title="Molinete de pesca",
        price=90.0,
        original_price=100.0,
        url="https://loja.example/molinete",
        source="   ",
    )
    assert not is_valid_offer(oferta_sem_loja)


def test_identifica_e_remove_ofertas_duplicadas():
    """Verifica se links equivalentes (com utm_*) são deduplicados mantendo o menor preço."""
    url_limpa = canonicalize_url(
        "https://Loja.Example/vara/?utm_source=telegram&id=10#topo"
    )
    assert url_limpa == "https://loja.example/vara?id=10"

    oferta_1 = Offer(
        title="Vara de pesca carbono",
        price=110.0,
        original_price=150.0,
        url="https://loja.example/vara?utm_source=insta",
        source="Loja X",
        image_url="https://cdn.loja.example/vara.jpg",
    )
    oferta_2_mais_barata = Offer(
        title="Vara de pesca carbono",
        price=95.0,
        original_price=150.0,
        url="https://loja.example/vara/?utm_medium=cpc",
        source="Loja X",
    )

    unicas = deduplicate_offers([oferta_1, oferta_2_mais_barata])
    assert len(unicas) == 1
    assert unicas[0].price == 95.0
    assert unicas[0].image_url == "https://cdn.loja.example/vara.jpg"


def test_salva_ofertas_e_historico_de_precos_em_sqlite(tmp_path: Path):
    """Testa a gravação de ofertas inéditas, duplicadas e histórico de preços no SQLite."""
    banco_teste = tmp_path / "teste_ofertas.db"
    initialize_database(banco_teste)

    oferta_inicial = Offer(
        title="Carretilha de pesca",
        price=200.0,
        original_price=250.0,
        url="https://loja.example/carretilha?utm_source=teste",
        source="Loja demonstrativa",
        image_url="https://cdn.loja.example/carretilha.jpg",
    )

    assert save_offer(oferta_inicial, db_path=banco_teste) is True
    assert save_offer(oferta_inicial, db_path=banco_teste) is False

    historico = get_price_history("https://loja.example/carretilha", db_path=banco_teste)
    assert len(historico) == 1
    assert historico[0]["price"] == 200.0

    oferta_com_novo_preco = Offer(
        title="Carretilha de pesca",
        price=179.90,
        original_price=250.0,
        url="https://loja.example/carretilha",
        source="Loja demonstrativa",
    )
    assert save_offer(oferta_com_novo_preco, db_path=banco_teste) is False

    historico_atualizado = get_price_history(
        "https://loja.example/carretilha",
        db_path=banco_teste,
    )
    assert len(historico_atualizado) == 2
    assert historico_atualizado[0]["price"] == 200.0
    assert historico_atualizado[1]["price"] == 179.90


def test_ofertas_demonstrativas_sao_validas():
    """Confirma que a fonte demonstrativa passa por todo o motor de ofertas."""
    ofertas_demo = filter_offers(get_demo_offers())
    assert len(ofertas_demo) == 3
    assert ofertas_demo[0].discount_percent >= ofertas_demo[1].discount_percent
