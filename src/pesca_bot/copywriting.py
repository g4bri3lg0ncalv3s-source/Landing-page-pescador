"""Formatação limpa e direta das postagens de ofertas para o Telegram.

Formato padrão das publicações:
🎣[Título do Produto]

⚡️[NOME DO MARKETPLACE EM MAIÚSCULAS]

❌De: R$ [Preço Original]

💰Saindo: R$ [Preço Atual]

🔗Link de compra:
[URL]
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pesca_bot.offers import Offer

TELEGRAM_CAPTION_MAX_LENGTH = 1024


@dataclass
class CopywritingConfig:
    """Configurações de limite de tamanho da legenda do Telegram."""

    max_caption_length: int = TELEGRAM_CAPTION_MAX_LENGTH


def format_brl(value: float) -> str:
    """Formata um valor numérico no padrão monetário brasileiro (ex.: `409,70`)."""
    formatted = f"{value:,.2f}"
    return formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def build_offer_caption(
    offer: "Offer",
    config: CopywritingConfig | None = None,
) -> str:
    """Monta o texto enxuto da oferta no formato visual definido para o canal."""
    cfg = config or CopywritingConfig()
    clean_title = " ".join(offer.title.split())
    category = offer.category
    marketplace = " ".join(offer.source.split()).upper()

    def _assemble(title_text: str) -> str:
        lines: list[str] = [f"🎣{title_text}"]
        if category:
            lines.append(f"Categoria: {category}")

        lines.extend(
            [
                "",
                f"⚡️{marketplace}",
            ]
        )

        if offer.has_verified_discount:
            lines.extend(
                [
                    "",
                    f"❌De: R$ {format_brl(offer.original_price)}",
                ]
            )

        lines.extend(
            [
                "",
                f"💰Saindo: R$ {format_brl(offer.price)}",
                "",
                "🔗Link de compra:",
                offer.url.strip(),
            ]
        )
        return "\n".join(lines)

    caption = _assemble(clean_title)

    if len(caption) > cfg.max_caption_length:
        excess = len(caption) - cfg.max_caption_length + 5
        short_len = max(15, len(clean_title) - excess)
        shortened_title = clean_title[:short_len].rstrip() + "..."
        caption = _assemble(shortened_title)[: cfg.max_caption_length]

    return caption
