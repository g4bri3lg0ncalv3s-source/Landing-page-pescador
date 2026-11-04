"""Fonte de ofertas demonstrativas para testes locais.

IMPORTANTE: Os produtos abaixo são fictícios e servem apenas para testar o
funcionamento do bot e do motor de ofertas localmente. Não representam
promoções reais de nenhum marketplace.
"""

from pesca_bot.offers import Offer


def get_demo_offers() -> list[Offer]:
    """Retorna uma lista de ofertas fictícias de artigos de pesca."""
    return [
        Offer(
            title="Vara de pesca telescópica",
            price=89.90,
            original_price=129.90,
            url="https://example.com/vara",
            source="Loja demonstrativa",
        ),
        Offer(
            title="Molinete para pesca esportiva",
            price=119.90,
            original_price=149.90,
            url="https://example.com/molinete",
            source="Loja demonstrativa",
        ),
        Offer(
            title="Carretilha de pesca",
            price=199.90,
            original_price=249.90,
            url="https://example.com/carretilha",
            source="Loja demonstrativa",
        ),
    ]

