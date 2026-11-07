"""Comandos e integração com o Telegram.

Este módulo conecta as fontes reais (Mercado Livre e Shopee), o banco de dados
SQLite, o módulo de copywriting e o publicador automatizado ao Telegram.

Segurança (Modo Stealth + Fail-Closed):
- Todos os comandos exigem chat privado (`ChatType.PRIVATE`) E `user_id`
  presente em `TELEGRAM_ADMIN_IDS` no `.env`;
- Caso `TELEGRAM_ADMIN_IDS` esteja vazio ou um usuário não autorizado envie
  qualquer comando, o bot permanece em silêncio no Telegram e registra um
  `WARNING` seguro nos logs com o `user_id` bloqueado.
"""

import asyncio
from functools import wraps
import logging
import re
import threading
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from pesca_bot.config import env_bool, env_float, env_str, load_env, reload_env
from pesca_bot.database import (
    OAuthTokenStore,
    initialize_database,
    save_offer,
    should_publish_offer,
)
from pesca_bot.offers import (
    Offer,
    filter_offers,
    format_offer,
    interleave_offers_by_source,
    is_valid_image_url,
)
from pesca_bot.publisher import (
    DEFAULT_MIN_DISCOUNT_PERCENT,
    DEFAULT_REPUBLISH_COOLDOWN_DAYS,
    DEFAULT_REPUBLISH_MIN_PRICE_DROP_PERCENT,
    OfferPublisher,
    TelegramChannelSender,
    extract_retry_seconds,
    get_brasilia_now,
    is_within_operating_hours,
    normalize_channel_id,
)
from pesca_bot.sources.base import SourceAPIError, SourceConfigurationError
from pesca_bot.sources.demo import get_demo_offers
from pesca_bot.sources.mercadolivre import MercadoLivreClient
from pesca_bot.sources.shopee import ShopeeAffiliateClient

logger = logging.getLogger(__name__)

load_env()

# Token de exemplo presente no .env.example (usado para detectar se o usuário
# esqueceu de trocar pelo token verdadeiro gerado no @BotFather)
EXAMPLE_TOKEN_PLACEHOLDER = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

TOKEN = env_str("TELEGRAM_BOT_TOKEN")

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

# Cursores e clientes reutilizáveis protegidos por locks para uso seguro em threads
_cycle_lock = threading.Lock()
_search_cycle_index = 0
_shopee_cycle_index = 0

_clients_lock = threading.Lock()
_ml_client: MercadoLivreClient | None = None
_ml_client_signature: tuple[str, ...] | None = None
_shopee_client: ShopeeAffiliateClient | None = None
_shopee_client_signature: tuple[str, ...] | None = None

CommandCallback = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def parse_admin_ids(raw: str | None = None) -> set[int]:
    """Interpreta `TELEGRAM_ADMIN_IDS` (IDs separados por vírgula ou espaço)."""
    source = raw if raw is not None else env_str("TELEGRAM_ADMIN_IDS")
    if not source or not source.strip():
        return set()

    admin_ids: set[int] = set()
    for part in re.split(r"[,\s;]+", source.strip()):
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            logger.warning(
                "Identificador inválido ignorado em TELEGRAM_ADMIN_IDS: '%s'.",
                part,
            )
            continue
        if value > 0:
            admin_ids.add(value)
        else:
            logger.warning(
                "ID de usuário não positivo ignorado em TELEGRAM_ADMIN_IDS: %s.",
                value,
            )
    return admin_ids


def is_authorized_admin_update(
    update: Update,
    admin_ids: set[int] | None = None,
) -> bool:
    """Valida se o comando veio de um ADM autorizado em chat privado (DM 1:1)."""
    allowed_ids = admin_ids if admin_ids is not None else parse_admin_ids()
    if not allowed_ids:
        return False

    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    if chat is None or user is None:
        return False

    if getattr(user, "is_bot", False):
        return False

    chat_type = getattr(chat, "type", None)
    if chat_type != ChatType.PRIVATE:
        return False

    user_id = getattr(user, "id", None)
    return isinstance(user_id, int) and user_id in allowed_ids


def require_admin_private_chat(handler: CommandCallback) -> CommandCallback:
    """Decorator que impõe acesso exclusivo a ADMs em chat privado (Modo Stealth)."""

    @wraps(handler)
    async def _wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        reload_env()
        admin_ids = parse_admin_ids()
        if not is_authorized_admin_update(update, admin_ids=admin_ids):
            user = getattr(update, "effective_user", None)
            chat = getattr(update, "effective_chat", None)
            user_id: Any = getattr(user, "id", "desconhecido")
            chat_type: Any = getattr(chat, "type", "desconhecido")
            logger.warning(
                "Acesso bloqueado ao comando /%s: user_id=%s chat_type=%s",
                handler.__name__,
                user_id,
                chat_type,
            )
            return None

        return await handler(update, context)

    return _wrapper


def _get_mercadolivre_client() -> MercadoLivreClient:
    """Retorna instância reutilizável de `MercadoLivreClient` com persistência OAuth."""
    global _ml_client, _ml_client_signature

    app_id = env_str("MERCADOLIVRE_APP_ID")
    client_secret = env_str("MERCADOLIVRE_CLIENT_SECRET")
    env_access = env_str("MERCADOLIVRE_ACCESS_TOKEN")
    env_refresh = env_str("MERCADOLIVRE_REFRESH_TOKEN")
    db_raw = env_str("DATABASE_PATH", "data/ofertas.db")
    signature = (app_id, client_secret, env_access, env_refresh, db_raw)

    with _clients_lock:
        if _ml_client is None or _ml_client_signature != signature:
            if _ml_client is not None:
                _ml_client.close()
            token_store = OAuthTokenStore(
                provider="mercadolivre",
                env_access_token=env_access,
                env_refresh_token=env_refresh,
            )
            _ml_client = MercadoLivreClient(
                access_token=env_access,
                app_id=app_id,
                client_secret=client_secret,
                refresh_token=env_refresh,
                token_store=token_store,
            )
            _ml_client_signature = signature
        return _ml_client


def _get_shopee_client() -> ShopeeAffiliateClient:
    """Retorna instância reutilizável de `ShopeeAffiliateClient`."""
    global _shopee_client, _shopee_client_signature

    app_id = env_str("SHOPEE_APP_ID")
    app_secret = env_str("SHOPEE_APP_SECRET")
    signature = (app_id, app_secret)

    with _clients_lock:
        if _shopee_client is None or _shopee_client_signature != signature:
            if _shopee_client is not None:
                _shopee_client.close()
            _shopee_client = ShopeeAffiliateClient(
                app_id=app_id,
                app_secret=app_secret,
            )
            _shopee_client_signature = signature
        return _shopee_client


def close_source_clients() -> None:
    """Fecha os pools de conexões HTTP das fontes externas."""
    global _ml_client, _ml_client_signature, _shopee_client, _shopee_client_signature
    with _clients_lock:
        if _ml_client is not None:
            _ml_client.close()
            _ml_client = None
            _ml_client_signature = None
        if _shopee_client is not None:
            _shopee_client.close()
            _shopee_client = None
            _shopee_client_signature = None


def _next_ml_cycle() -> int:
    global _search_cycle_index
    with _cycle_lock:
        current = _search_cycle_index
        _search_cycle_index += 1
        return current


def _next_shopee_cycle() -> int:
    global _shopee_cycle_index
    with _cycle_lock:
        current = _shopee_cycle_index
        _shopee_cycle_index += 1
        return current


def collect_offers(
    keywords: tuple[str, ...] = DEFAULT_SEARCH_KEYWORDS,
    limit_per_keyword: int = 6,
    max_pages_to_scan: int = 3,
) -> list[Offer]:
    """Coleta ofertas das fontes oficiais configuradas no `.env` (Mercado Livre / Shopee).

    Rotaciona automaticamente os termos de busca e avança a paginação (`offset`/`page`)
    quando as ofertas iniciais já tiverem sido publicadas recentemente no canal,
    garantindo sempre a descoberta de novos produtos.
    """
    reload_env()
    collected: list[Offer] = []
    used_real_source = False

    channel_id = normalize_channel_id(env_str("TELEGRAM_CHANNEL_ID"))
    min_discount = env_float(
        "COPY_MIN_DISCOUNT_PERCENT",
        DEFAULT_MIN_DISCOUNT_PERCENT,
        min_value=0.0,
    )
    cooldown_days = env_float(
        "REPUBLISH_COOLDOWN_DAYS",
        DEFAULT_REPUBLISH_COOLDOWN_DAYS,
        min_value=0.0,
    )
    min_drop = env_float(
        "REPUBLISH_MIN_PRICE_DROP_PERCENT",
        DEFAULT_REPUBLISH_MIN_PRICE_DROP_PERCENT,
        min_value=0.0,
    )
    initialize_database()

    def _eligible(candidates: list[Offer]) -> list[Offer]:
        filtered = filter_offers(
            candidates,
            min_discount_percent=min_discount,
            require_fishing_niche=True,
        )
        if not channel_id:
            return filtered
        return [
            o
            for o in filtered
            if should_publish_offer(
                o,
                channel_id,
                cooldown_days=cooldown_days,
                min_price_drop_percent=min_drop,
            )
        ]

    # 1. Fonte Oficial: Mercado Livre
    ml_has_creds = bool(
        env_str("MERCADOLIVRE_ACCESS_TOKEN")
        or (env_str("MERCADOLIVRE_APP_ID") and env_str("MERCADOLIVRE_CLIENT_SECRET"))
    )
    if ml_has_creds:
        used_real_source = True
        ml_client = _get_mercadolivre_client()
        total_keywords = len(keywords)
        batch_size = min(4, total_keywords) if total_keywords > 0 else 0

        for _ in range(max(1, max_pages_to_scan)):
            current_cycle = _next_ml_cycle()

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
            if len(_eligible(collected)) >= 3:
                break

    # 2. Fonte Oficial: Shopee (ativada automaticamente quando configurada no .env)
    shopee_has_creds = bool(
        env_str("SHOPEE_APP_ID") and env_str("SHOPEE_APP_SECRET")
    )
    if shopee_has_creds:
        used_real_source = True
        shopee_client = _get_shopee_client()
        total_keywords = len(keywords)
        batch_size = min(4, total_keywords) if total_keywords > 0 else 0
        shopee_collected: list[Offer] = []

        for _ in range(max(1, max_pages_to_scan)):
            current_cycle = _next_shopee_cycle()

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

            if len(_eligible(shopee_collected)) >= 3:
                break

        collected.extend(shopee_collected)

    if not used_real_source:
        return get_demo_offers()

    return collected


async def _reply_offer_safely(update: Update, offer: Offer, caption: str) -> None:
    """Envia a prévia da oferta no chat privado do ADM tratando limites e erros de rede."""
    if not update.message:
        return

    if is_valid_image_url(offer.image_url) and offer.image_url:
        try:
            await update.message.reply_photo(
                photo=offer.image_url,
                caption=caption,
            )
            return
        except RetryAfter as exc:
            await asyncio.sleep(extract_retry_seconds(exc))
            try:
                await update.message.reply_photo(
                    photo=offer.image_url,
                    caption=caption,
                )
                return
            except BadRequest:
                logger.warning(
                    "Imagem recusada em /ofertas para '%s'; usando texto.",
                    offer.title,
                )
            except (TimedOut, NetworkError, TelegramError, OSError) as retry_exc:
                logger.error(
                    "Falha de rede ao responder /ofertas: %s",
                    type(retry_exc).__name__,
                )
                return
        except BadRequest:
            logger.warning(
                "Imagem indisponível ao responder /ofertas para '%s'; enviando texto.",
                offer.title,
            )
        except (TimedOut, NetworkError, OSError) as exc:
            logger.error(
                "Falha de conexão ao enviar foto em /ofertas: %s",
                type(exc).__name__,
            )
            return
        except TelegramError:
            logger.warning(
                "Erro do Telegram na foto de '%s' em /ofertas; enviando texto.",
                offer.title,
            )

    try:
        await update.message.reply_text(caption)
    except RetryAfter as exc:
        await asyncio.sleep(extract_retry_seconds(exc))
        try:
            await update.message.reply_text(caption)
        except (TimedOut, NetworkError, TelegramError, OSError) as retry_exc:
            logger.error(
                "Falha ao reenviar texto em /ofertas: %s",
                type(retry_exc).__name__,
            )
    except (TimedOut, NetworkError, TelegramError, OSError) as exc:
        logger.error("Falha ao enviar texto em /ofertas: %s", type(exc).__name__)


@require_admin_private_chat
async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Responde ao comando /start apresentando o bot aos administradores."""
    if not update.message:
        return

    await update.message.reply_text(
        "🎣 Painel ADM — Bot Inteligente de Promoções de Pesca\n\n"
        "Comandos administrativos:\n"
        "/ofertas - buscar promoções reais de pesca agora\n"
        "/publicar - publicar próxima oferta elegível no canal\n"
        "/status - ver estado das publicações no canal\n"
        "/pausar - pausar publicações automáticas\n"
        "/retomar - retomar publicações automáticas\n"
        "/ajuda - consultar os comandos"
    )


@require_admin_private_chat
async def ajuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Responde ao comando /ajuda explicando como consultar e controlar promoções."""
    if not update.message:
        return

    await update.message.reply_text(
        "Use /ofertas para buscar promoções reais no Mercado Livre e Shopee.\n"
        "Use /publicar para enviar uma oferta elegível ao canal imediatamente.\n"
        "Use /pausar, /retomar e /status para gerenciar o envio automático ao canal."
    )


@require_admin_private_chat
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

    min_discount = env_float(
        "COPY_MIN_DISCOUNT_PERCENT",
        DEFAULT_MIN_DISCOUNT_PERCENT,
        min_value=0.0,
    )
    raw_offers = await asyncio.to_thread(collect_offers)
    valid_offers = interleave_offers_by_source(
        filter_offers(
            raw_offers,
            min_discount_percent=min_discount,
            require_fishing_niche=True,
        )
    )

    if not valid_offers:
        await update.message.reply_text(
            "Nenhuma oferta válida de pesca com desconto mínimo encontrada neste momento."
        )
        return

    for offer in valid_offers[:5]:
        save_offer(offer)
        caption = format_offer(offer)
        await _reply_offer_safely(update, offer, caption)


@require_admin_private_chat
async def publicar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Comando /publicar: coleta ofertas reais e publica a próxima elegível no canal."""
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
    await update.message.reply_text(
        "🚀 Coletando ofertas e verificando a próxima elegível para publicar no canal..."
    )
    raw_offers = await asyncio.to_thread(collect_offers)
    published = await publisher.publish_approved_offers(
        raw_offers,
        max_offers=1,
        ignore_pause=True,
    )

    if not published:
        await update.message.reply_text(
            "Nenhuma oferta elegível foi publicada (verifique se já estão em "
            "período de cooldown ou se o bot é administrador do canal em TELEGRAM_CHANNEL_ID)."
        )
        return

    await update.message.reply_text(
        f"✅ {len(published)} oferta(s) publicada(s) com sucesso no canal!"
    )


@require_admin_private_chat
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


@require_admin_private_chat
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


@require_admin_private_chat
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
        f"Intervalo: {publisher.interval_seconds:.0f}s\n"
        f"Desconto mínimo: {publisher.min_discount_percent:.1f}%\n"
        f"Republicação: após {publisher.republish_cooldown_days:.0f}d ou queda >= {publisher.republish_min_drop_percent:.0f}%"
    )


async def _post_init(application: Application) -> None:
    """Inicia a tarefa assíncrona periódica de publicação em segundo plano."""
    publisher: OfferPublisher | None = application.bot_data.get("publisher")
    if publisher is not None:
        application.bot_data["publisher_task"] = asyncio.create_task(
            publisher.run_periodic_loop(collect_offers)
        )


async def _post_shutdown(application: Application) -> None:
    """Cancela a tarefa periódica com segurança e fecha pools HTTP ao encerrar."""
    task: asyncio.Task[Any] | None = application.bot_data.get("publisher_task")
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    close_source_clients()


def build_application() -> Application:
    """Valida o token do Telegram, cria a aplicação e registra os comandos."""
    token = (env_str("TELEGRAM_BOT_TOKEN") or TOKEN or "").strip()

    if not token or token == EXAMPLE_TOKEN_PLACEHOLDER:
        raise RuntimeError(
            "Configure um TELEGRAM_BOT_TOKEN válido no arquivo .env "
            "(gere seu token real com o @BotFather no Telegram)."
        )

    if not parse_admin_ids():
        logger.warning(
            "TELEGRAM_ADMIN_IDS não está configurado no .env (modo fail-closed ativo: "
            "nenhum comando será aceito). Envie /start em chat privado para o bot "
            "para visualizar seu user_id nos logs e adicione-o em TELEGRAM_ADMIN_IDS."
        )

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    publisher = OfferPublisher(sender=TelegramChannelSender(app.bot))
    auto_publish = env_bool("AUTO_PUBLISH_ENABLED", False)
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
