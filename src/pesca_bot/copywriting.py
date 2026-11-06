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
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pesca_bot.offers import Offer

TELEGRAM_CAPTION_MAX_LENGTH = 1024
MIN_CAPTION_LENGTH = 200
ELLIPSIS = "..."


@dataclass
class CopywritingConfig:
    """Configurações de limite de tamanho da legenda do Telegram."""

    max_caption_length: int = TELEGRAM_CAPTION_MAX_LENGTH

    @classmethod
    def from_env(cls) -> "CopywritingConfig":
        """Lê `COPY_MAX_CAPTION_LENGTH` do ambiente (limitado a 200..1024)."""
        raw = (os.getenv("COPY_MAX_CAPTION_LENGTH") or "").strip()
        try:
            value = int(raw) if raw else TELEGRAM_CAPTION_MAX_LENGTH
        except ValueError:
            value = TELEGRAM_CAPTION_MAX_LENGTH
        value = max(MIN_CAPTION_LENGTH, min(value, TELEGRAM_CAPTION_MAX_LENGTH))
        return cls(max_caption_length=value)


def format_brl(value: float) -> str:
    """Formata um valor numérico no padrão monetário brasileiro (ex.: `409,70`)."""
    formatted = f"{value:,.2f}"
    return formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def build_offer_caption(
    offer: "Offer",
    config: CopywritingConfig | None = None,
) -> str:
    """Monta o texto enxuto da oferta no formato visual definido para o canal.

    Se a legenda ultrapassar o limite, apenas o TÍTULO é encurtado. O link de
    compra (com parâmetros de afiliado) nunca é cortado: se nem o título mínimo
    couber, a legenda é mantida com o link íntegro e o publicador fará o
    fallback automático para mensagem de texto (limite maior do Telegram).
    """
    cfg = config or CopywritingConfig.from_env()
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
        fixed_length = len(_assemble(""))
        title_budget = cfg.max_caption_length - fixed_length

        if title_budget > len(ELLIPSIS):
            shortened_title = (
                clean_title[: title_budget - len(ELLIPSIS)].rstrip() + ELLIPSIS
            )
        else:
            # Nem o título mínimo cabe (link muito longo): preserva o link.
            shortened_title = clean_title[:15].rstrip() + ELLIPSIS

        caption = _assemble(shortened_title)

    return caption
