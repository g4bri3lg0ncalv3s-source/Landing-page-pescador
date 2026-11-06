"""Testes automatizados para o formato limpo das postagens de ofertas."""

from pesca_bot.copywriting import (
    TELEGRAM_CAPTION_MAX_LENGTH,
    CopywritingConfig,
    build_offer_caption,
)
from pesca_bot.offers import Offer


def test_formata_post_no_layout_limpo_padrao():
    """Verifica se a legenda segue exatamente o layout enxuto definido pelo usuário."""
    oferta = Offer(
        title="Vara Para Carretilha Mizu C-581h 1,73mts 12-25lbs - Maruri",
        price=188.27,
        original_price=409.70,
        url="https://www.mercadolivre.com.br/p/MLB12345",
        source="Mercado Livre",
    )

    legenda = build_offer_caption(oferta)

    esperado = (
        "🎣Vara Para Carretilha Mizu C-581h 1,73mts 12-25lbs - Maruri\n\n"
        "⚡️MERCADO LIVRE\n\n"
        "❌De: R$ 409,70\n\n"
        "💰Saindo: R$ 188,27\n\n"
        "🔗Link de compra:\n"
        "https://www.mercadolivre.com.br/p/MLB12345"
    )

    assert legenda == esperado


def test_omite_linha_de_preco_original_quando_ausente():
    """Se não houver preço original maior que o atual, exibe apenas o preço Saindo."""
    oferta_sem_preco_anterior = Offer(
        title="Vara de Pesca Carbono 2,10m",
        price=129.90,
        original_price=0.0,
        url="https://www.mercadolivre.com.br/p/MLB99999",
        source="Shopee",
    )

    legenda = build_offer_caption(oferta_sem_preco_anterior)

    assert "⚡️SHOPEE" in legenda
    assert "❌De: R$" not in legenda
    assert "💰Saindo: R$ 129,90" in legenda
    assert "🔗Link de compra:\nhttps://www.mercadolivre.com.br/p/MLB99999" in legenda


def test_respeita_limite_maximo_da_legenda():
    """Garante que títulos muito longos sejam encurtados sem cortar o link final."""
    titulo_gigante = "Vara de Pesca Telescópica " + ("Carbono Ultra Resistente " * 40)
    oferta = Offer(
        title=titulo_gigante,
        price=89.90,
        original_price=139.90,
        url="https://www.mercadolivre.com.br/p/MLB55555",
        source="Mercado Livre",
    )

    config = CopywritingConfig(max_caption_length=300)
    legenda = build_offer_caption(oferta, config=config)

    assert len(legenda) <= 300
    assert len(legenda) <= TELEGRAM_CAPTION_MAX_LENGTH
    assert "https://www.mercadolivre.com.br/p/MLB55555" in legenda


def test_preserva_link_de_afiliado_integro_mesmo_com_url_muito_longa():
    """Garante que o encurtamento nunca corte o final da URL de compra/comissão."""
    url_longa = (
        "https://produto.mercadolivre.com.br/MLB-9988776655-"
        + ("vara-de-pesca-carbono-profissional-" * 4)
        + "?matt_word=afiliado_pesca&matt_tool=88776655"
    )
    oferta = Offer(
        title="Vara de Pesca Carbono Intergreen 2,40m Ação Rápida",
        price=349.90,
        original_price=499.90,
        url=url_longa,
        source="Mercado Livre",
    )

    config = CopywritingConfig(max_caption_length=220)
    legenda = build_offer_caption(oferta, config=config)

    assert legenda.endswith("matt_word=afiliado_pesca&matt_tool=88776655")
    assert url_longa in legenda
