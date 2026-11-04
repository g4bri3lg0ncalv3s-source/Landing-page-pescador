"""Comandos e integração com o Telegram.

Este módulo conecta as fontes reais (Mercado Livre e Shopee), o banco de dados
SQLite, o módulo de copywriting e o publicador automatizado ao Telegram:
- `/start` e `/ajuda`: instruções de uso;
- `/ofertas`: busca ofertas reais no Mercado Livre (ou Shopee, quando configurada),
  filtra pelos melhores descontos, salva no SQLite e exibe no chat (com foto e legenda);
- `/publicar`: envia imediatamente as ofertas aprovadas e inéditas para o canal;
- `/status`, `/pausar` e `/retomar`: gerenciam a publicação automática periódica no canal.
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from pesca_bot.database import initialize_database, is_offer_published, save_offer
from pesca_bot.offers import Offer, filter_offers, format_offer, is_valid_image_url
from pesca_bot.publisher import (
    OfferPublisher,
    TelegramChannelSender,
    get_brasilia_now,
    is_within_operating_hours,
    normalize_channel_id,
)
from pesca_bot.sources.base import SourceAPIError, SourceConfigurationError
from pesca_bot.sources.demo import get_demo_offers
from pesca_bot.sources.mercadolivre import MercadoLivreClient
from pesca_bot.sources.shopee import ShopeeAffiliateClient

logger = logging.getLogger(__name__)

# Localiza a raiz do projeto para carregar o arquivo .env com segurança
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=True)

# Token de exemplo presente no .env.example (usado para detectar se o usuário
# esqueceu de trocar pelo token verdadeiro gerado no @BotFather)
EXAMPLE_TOKEN_PLACEHOLDER = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Termos principais de busca para varrer promoções de artigos de pesca
DEFAULT_SEARCH_KEYWORDS = (
    "vara de pesca",
    "molinete de pesca",
    "carretilha de pesca",
    "isca artificial",
    "linha multifilamento pesca",
    "anzol de pesca",
    "caixa de pesca",
    "alicate de pesca",
    "kit de pesca",
    "bolsa de pesca",
    "passagua pesca",
    "oculos polarizado pesca",
)

# Cursores globais em memória para rotacionar palavras-chave e páginas
# a cada busca, evitando consultar sempre apenas os mesmos primeiros itens.
_search_cycle_index = 0
_shopee_cycle_index = 0


def collect_offers(
    keywords: tuple[str, ...] = DEFAULT_SEARCH_KEYWORDS,
    limit_per_keyword: int = 6,
    max_pages_to_scan: int = 3,
) -> list[Offer]:
    """Coleta ofertas das fontes oficiais configuradas no `.env` (Mercado Livre / Shopee).

    Rotaciona automaticamente os termos de busca e avança a paginação (`offset`/`page`)
    quando as ofertas iniciais já tiverem sido publicadas no canal, garantindo
    sempre a descoberta de novos produtos.
    """
    global _search_cycle_index, _shopee_cycle_index
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    collected: list[Offer] = []
    used_real_source = False

    channel_id = normalize_channel_id(os.getenv("TELEGRAM_CHANNEL_ID", ""))
    initialize_database()

    # 1. Fonte Oficial: Mercado Livre
    ml_has_creds = bool(
        os.getenv("MERCADOLIVRE_ACCESS_TOKEN", "").strip()
        or (
            os.getenv("MERCADOLIVRE_APP_ID", "").strip()
            and os.getenv("MERCADOLIVRE_CLIENT_SECRET", "").strip()
        )
    )
    if ml_has_creds:
        used_real_source = True
        ml_client = MercadoLivreClient()
        total_keywords = len(keywords)
        batch_size = min(4, total_keywords) if total_keywords > 0 else 0

        for page_attempt in range(max(1, max_pages_to_scan)):
            current_cycle = _search_cycle_index
            _search_cycle_index += 1

            start_idx = (current_cycle * batch_size) % max(1, total_keywords)
            selected_keywords = [
                keywords[(start_idx + i) % total_keywords]
                for i in range(batch_size)
            ]
            # Avança o offset a cada volta completa pelas palavras-chave
            round_number = (current_cycle * batch_size) // max(1, total_keywords)
            offset = (round_number % 10) * limit_per_keyword

            batch_offers: list[Offer] = []
            for keyword in selected_keywords:
                try:
                    batch_offers.extend(
                        ml_client.fetch_offers(
                            keyword,
                            limit=limit_per_keyword,
                            offset=offset,
                        )
                    )
                except (SourceConfigurationError, SourceAPIError) as exc:
                    logger.warning(
                        "Aviso ao buscar '%s' (offset=%s) no Mercado Livre: %s",
                        keyword,
                        offset,
                        exc,
                    )

            collected.extend(batch_offers)
            valid_so_far = filter_offers(collected)
            if channel_id:
                unpublished = [
                    o
                    for o in valid_so_far
                    if not is_offer_published(o.url, channel_id)
                ]
            else:
                unpublished = valid_so_far

            # Se já encontramos ofertas inéditas suficientes, encerra a varredura
            if len(unpublished) >= 3:
                break

    # 2. Fonte Oficial: Shopee (ativada automaticamente quando configurada no .env)
    shopee_has_creds = bool(
        os.getenv("SHOPEE_APP_ID", "").strip()
        and os.getenv("SHOPEE_APP_SECRET", "").strip()
    )
    if shopee_has_creds:
        used_real_source = True
        shopee_client = ShopeeAffiliateClient()
        total_keywords = len(keywords)
        batch_size = min(4, total_keywords) if total_keywords > 0 else 0
        shopee_collected: list[Offer] = []

        for _ in range(max(1, max_pages_to_scan)):
            current_cycle = _shopee_cycle_index
            _shopee_cycle_index += 1

            start_idx = (current_cycle * batch_size) % max(1, total_keywords)
            selected_keywords = [
                keywords[(start_idx + i) % total_keywords]
                for i in range(batch_size)
            ]
            round_number = (current_cycle * batch_size) // max(1, total_keywords)
            page = (round_number % 10) + 1

            for keyword in selected_keywords:
                try:
                    shopee_collected.extend(
                        shopee_client.fetch_offers(
                            keyword,
                            limit=limit_per_keyword,
                            page=page,
                        )
                    )
                except (SourceConfigurationError, SourceAPIError) as exc:
                    logger.warning(
                        "Aviso ao buscar '%s' (página %s) na Shopee: %s",
                        keyword,
                        page,
                        exc,
                    )

            valid_shopee = filter_offers(shopee_collected)
            if channel_id:
                unpublished_shopee = [
                    o
                    for o in valid_shopee
                    if not is_offer_published(o.url, channel_id)
                ]
            else:
                unpublished_shopee = valid_shopee

            if len(unpublished_shopee) >= 3:
                break

        collected.extend(shopee_collected)

    if not used_real_source:
        return get_demo_offers()

    return collected


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Responde ao comando /start apresentando o bot e seus comandos."""
    if not update.message:
        return

    await update.message.reply_text(
        "🎣 Bot Inteligente de Promoções de Pesca\n\n"
        "Comandos disponíveis:\n"
        "/ofertas - buscar promoções reais de pesca agora\n"
        "/publicar - publicar ofertas inéditas no canal\n"
        "/status - ver estado das publicações no canal\n"
        "/pausar - pausar publicações automáticas\n"
        "/retomar - retomar publicações automáticas\n"
        "/ajuda - consultar os comandos"
    )


async def ajuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Responde ao comando /ajuda explicando como consultar e controlar promoções."""
    if not update.message:
        return

    await update.message.reply_text(
        "Use /ofertas para buscar promoções reais no Mercado Livre e Shopee.\n"
        "Use /publicar para enviar ofertas inéditas ao canal imediatamente.\n"
        "Use /pausar, /retomar e /status para gerenciar o envio automático ao canal."
    )


async def ofertas(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Responde ao comando /ofertas buscando, filtrando, salvando e exibindo promoções."""
    if not update.message:
        return

    if not is_within_operating_hours():
        await update.message.reply_text(
            "🌙 As promoções aparecem apenas até as 22:00 (Horário de Brasília).\n"
            "Elas voltarão a aparecer novamente às 07:00!"
        )
        return

    await update.message.reply_text("🔎 Buscando ofertas de artigos de pesca...")
    initialize_database()

    raw_offers = await asyncio.to_thread(collect_offers)
    valid_offers = filter_offers(raw_offers)

    if not valid_offers:
        await update.message.reply_text(
            "Nenhuma oferta válida com desconto encontrada neste momento."
        )
        return

    for offer in valid_offers[:5]:
        save_offer(offer)
        caption = format_offer(offer)
        if is_valid_image_url(offer.image_url) and offer.image_url:
            try:
                await update.message.reply_photo(
                    photo=offer.image_url,
                    caption=caption,
                )
                continue
            except TelegramError:
                logger.warning(
                    "Imagem indisponível ao responder /ofertas para '%s'; enviando texto.",
                    offer.title,
                )
        await update.message.reply_text(caption)


async def publicar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Comando /publicar: coleta ofertas reais e publica as inéditas no canal."""
    if not update.message:
        return

    publisher: OfferPublisher | None = context.bot_data.get("publisher")
    if publisher is None:
        await update.message.reply_text("Publicador não configurado.")
        return

    if not publisher.is_operating_hours():
        await update.message.reply_text(
            "🌙 As promoções aparecem apenas até as 22:00 (Horário de Brasília).\n"
            "As publicações retornarão automaticamente às 07:00!"
        )
        return

    publisher.reload_env()
    was_paused = publisher.is_paused
    if was_paused:
        publisher.resume()

    await update.message.reply_text(
        "🚀 Coletando ofertas e verificando a próxima inédita para publicar no canal..."
    )
    raw_offers = await asyncio.to_thread(collect_offers)
    published = await publisher.publish_approved_offers(raw_offers, max_offers=1)

    if was_paused:
        publisher.pause()

    if not published:
        await update.message.reply_text(
            "Nenhuma oferta inédita foi publicada (verifique se já foram postadas "
            "ou se o bot é administrador do canal configurado em TELEGRAM_CHANNEL_ID)."
        )
        return

    await update.message.reply_text(
        f"✅ {len(published)} oferta(s) inédita(s) publicada(s) com sucesso no canal!"
    )


async def pausar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Comando /pausar: pausa o envio automático de ofertas ao canal."""
    if not update.message:
        return

    publisher: OfferPublisher | None = context.bot_data.get("publisher")
    if publisher is not None:
        publisher.pause()

    await update.message.reply_text(
        "⏸️ Publicações automáticas no canal foram pausadas."
    )


async def retomar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Comando /retomar: retoma o envio automático de ofertas ao canal."""
    if not update.message:
        return

    publisher: OfferPublisher | None = context.bot_data.get("publisher")
    if publisher is not None:
        publisher.resume()

    await update.message.reply_text(
        "▶️ Publicações automáticas no canal foram retomadas."
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Comando /status: informa se o publicador está ativo ou pausado."""
    if not update.message:
        return

    publisher: OfferPublisher | None = context.bot_data.get("publisher")
    if publisher is None:
        await update.message.reply_text("Publicador ainda não inicializado.")
        return

    publisher.reload_env()
    agora_brt = get_brasilia_now().strftime("%H:%M")
    janela_ativa = publisher.is_operating_hours()
    horario_status = (
        f"DENTRO do horário ({agora_brt} BRT - ativo das 07:00 às 22:00)"
        if janela_ativa
        else f"FORA do horário ({agora_brt} BRT - retorna às 07:00)"
    )
    estado = "PAUSADO ⏸️" if publisher.is_paused else "ATIVO ▶️"
    canal = publisher.channel_id or "(não configurado)"
    await update.message.reply_text(
        f"Status do Publicador: {estado}\n"
        f"Janela de exibição: {horario_status}\n"
        f"Canal alvo: {canal}\n"
        f"Intervalo: {publisher.interval_seconds:.0f}s"
    )


async def _post_init(application: Application) -> None:
    """Inicia a tarefa assíncrona periódica de publicação em segundo plano."""
    publisher: OfferPublisher | None = application.bot_data.get("publisher")
    if publisher is not None:
        application.bot_data["publisher_task"] = asyncio.create_task(
            publisher.run_periodic_loop(collect_offers)
        )


def build_application() -> Application:
    """Valida o token do Telegram, cria a aplicação e registra os comandos."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or TOKEN or "").strip()

    if not token or token == EXAMPLE_TOKEN_PLACEHOLDER:
        raise RuntimeError(
            "Configure um TELEGRAM_BOT_TOKEN válido no arquivo .env "
            "(gere seu token real com o @BotFather no Telegram)."
        )

    app = Application.builder().token(token).post_init(_post_init).build()

    publisher = OfferPublisher(sender=TelegramChannelSender(app.bot))
    auto_publish = os.getenv("AUTO_PUBLISH_ENABLED", "true").strip().lower() == "true"
    if not auto_publish:
        publisher.pause()

    app.bot_data["publisher"] = publisher

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("ofertas", ofertas))
    app.add_handler(CommandHandler("publicar", publicar))
    app.add_handler(CommandHandler("pausar", pausar))
    app.add_handler(CommandHandler("retomar", retomar))
    app.add_handler(CommandHandler("status", status))

    return app


def main() -> None:
    """Inicializa o banco SQLite e inicia o bot do Telegram em modo polling."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    initialize_database()
    app = build_application()
    print("Bot iniciado. Pressione Ctrl+C para encerrar.")
    app.run_polling()


if __name__ == "__main__":
    main()
