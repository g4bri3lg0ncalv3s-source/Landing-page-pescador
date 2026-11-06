"""Motor de publicação automatizada de ofertas (com imagem e fallback) no Telegram.

Responsabilidades deste módulo:
- Ler o identificador do canal (`TELEGRAM_CHANNEL_ID`), o intervalo
  (`PUBLISH_INTERVAL_SECONDS`), o desconto mínimo (`COPY_MIN_DISCOUNT_PERCENT`)
  e as regras de republicação a partir do `.env`;
- Verificar se o bot possui permissão de administrador para postar no canal
  (com cache em memória para não sobrecarregar a API do Telegram);
- Publicar ofertas com imagem (`send_photo`) quando houver `image_url` válida;
- Realizar fallback automático para texto (`send_message`) apenas quando a
  imagem for inválida/recusada, sem repetir envios durante quedas de rede;
- Registrar cada envio na tabela `publications` do SQLite (respeitando cooldown
  de republicação e gatilho de queda adicional de preço);
- Proteger contra concorrência (`asyncio.Lock`) entre `/publicar` e o loop
  periódico, além de expirar filas de ofertas antigas na memória.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Protocol

from telegram import Bot
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from pesca_bot.config import env_float, env_str, load_env, reload_env
from pesca_bot.database import (
    DATABASE_PATH,
    initialize_database,
    record_publication,
    save_offer,
    should_publish_offer,
)
from pesca_bot.offers import Offer, filter_offers, format_offer, is_valid_image_url

logger = logging.getLogger(__name__)

load_env()

DEFAULT_PUBLISH_INTERVAL_SECONDS = 180.0
DEFAULT_MIN_DISCOUNT_PERCENT = 5.0
DEFAULT_REPUBLISH_COOLDOWN_DAYS = 15.0
DEFAULT_REPUBLISH_MIN_PRICE_DROP_PERCENT = 10.0
DEFAULT_PENDING_OFFERS_TTL_SECONDS = 1800.0
PERMISSION_CACHE_TTL_SECONDS = 300.0

# Fuso horário oficial de Brasília (UTC-3) e janela de exibição das promoções (07:00 às 22:00)
BRASILIA_TZ = timezone(timedelta(hours=-3), name="BRT")
OPERATING_START_HOUR = 7
OPERATING_END_HOUR = 22


def get_brasilia_now() -> datetime:
    """Retorna a data e hora atual no fuso horário de Brasília (UTC-3)."""
    return datetime.now(BRASILIA_TZ)


def is_within_operating_hours(
    now: datetime | None = None,
    start_hour: int = OPERATING_START_HOUR,
    end_hour: int = OPERATING_END_HOUR,
) -> bool:
    """Verifica se o horário atual em Brasília está dentro da janela permitida (07:00 às 22:00).

    As promoções aparecem a partir das 07:00 até as 22:00 (exclusivo, ou seja, 07:00:00 até 21:59:59).
    Das 22:00 até as 06:59:59, retorna `False`.
    """
    if now is None:
        current = get_brasilia_now()
    elif now.tzinfo is not None:
        current = now.astimezone(BRASILIA_TZ)
    else:
        current = now.replace(tzinfo=BRASILIA_TZ)

    return start_hour <= current.hour < end_hour


def normalize_channel_id(raw_channel_id: str | int | None) -> str:
    """Limpa o identificador do canal ou grupo do Telegram sem adulterar IDs de grupos.

    Aceita `@username` (ex.: `@canal_pesca`), IDs de canais/supergrupos
    (`-100...`) ou IDs negativos de grupos comuns (`-...`) exatamente como
    exigidos pela Bot API do Telegram.
    """
    if raw_channel_id is None:
        return ""
    return str(raw_channel_id).strip()


class ChannelSender(Protocol):
    """Interface de envio de mensagens e fotos para o canal (real ou simulada)."""

    async def can_post_to_channel(self, channel_id: str | int) -> bool:
        """Retorna `True` se o bot tiver permissão para publicar no canal."""

    async def send_offer_photo(
        self,
        channel_id: str | int,
        photo_url: str,
        caption: str,
    ) -> str | int | None:
        """Envia a foto com legenda ao canal e retorna o ID da mensagem gerada."""

    async def send_offer_message(
        self,
        channel_id: str | int,
        text: str,
    ) -> str | int | None:
        """Envia o texto da oferta ao canal e retorna o ID da mensagem gerada."""


class SimulatedChannelSender:
    """Interface simulada de envio para testes sem mandar mensagens reais ao Telegram."""

    def __init__(
        self,
        has_permission: bool = True,
        fail_photo_urls: set[str] | None = None,
    ) -> None:
        self.has_permission = has_permission
        self.fail_photo_urls: set[str] = set(fail_photo_urls or ())
        self.sent_messages: list[dict[str, Any]] = []
        self._counter = 0

    async def can_post_to_channel(self, channel_id: str | int) -> bool:
        return self.has_permission

    async def send_offer_photo(
        self,
        channel_id: str | int,
        photo_url: str,
        caption: str,
    ) -> str:
        """Simula o envio de foto com legenda (`send_photo`)."""
        if photo_url in self.fail_photo_urls:
            raise BadRequest("Simulated remote image URL unavailable")

        self._counter += 1
        msg_id = f"sim-photo-{self._counter}"
        self.sent_messages.append(
            {
                "channel_id": str(channel_id),
                "message_id": msg_id,
                "text": caption,
                "photo_url": photo_url,
                "type": "photo",
            }
        )
        return msg_id

    async def send_offer_message(self, channel_id: str | int, text: str) -> str:
        """Simula o envio de mensagem de texto (`send_message`)."""
        self._counter += 1
        msg_id = f"sim-{self._counter}"
        self.sent_messages.append(
            {
                "channel_id": str(channel_id),
                "message_id": msg_id,
                "text": text,
                "photo_url": None,
                "type": "text",
            }
        )
        return msg_id


class TelegramChannelSender:
    """Adaptador oficial que utiliza `telegram.Bot` (`send_photo` e `send_message`)."""

    def __init__(
        self,
        bot: Bot,
        permission_cache_ttl: float = PERMISSION_CACHE_TTL_SECONDS,
    ) -> None:
        self._bot = bot
        self._bot_user_id: int | None = None
        self._permission_cache_ttl = max(0.0, float(permission_cache_ttl))
        self._verified_channel: str | None = None
        self._verified_until: float = 0.0

    def invalidate_permission_cache(self) -> None:
        """Limpa o cache de permissão do canal (ex.: após erro `Forbidden`)."""
        self._verified_channel = None
        self._verified_until = 0.0

    async def can_post_to_channel(self, channel_id: str | int) -> bool:
        """Consulta o Telegram (com cache) para confirmar permissão de postagem."""
        channel_key = str(channel_id).strip()
        now_mono = time.monotonic()

        if (
            self._permission_cache_ttl > 0
            and self._verified_channel == channel_key
            and now_mono < self._verified_until
        ):
            return True

        if self._bot_user_id is None:
            bot_user = await self._bot.get_me()
            self._bot_user_id = bot_user.id

        member = await self._bot.get_chat_member(
            chat_id=channel_id,
            user_id=self._bot_user_id,
        )

        if member.status not in ("administrator", "creator"):
            self.invalidate_permission_cache()
            return False

        can_post = getattr(member, "can_post_messages", None)
        allowed = can_post is not False
        if allowed and self._permission_cache_ttl > 0:
            self._verified_channel = channel_key
            self._verified_until = now_mono + self._permission_cache_ttl
        else:
            self.invalidate_permission_cache()
        return allowed

    async def send_offer_photo(
        self,
        channel_id: str | int,
        photo_url: str,
        caption: str,
    ) -> int:
        """Publica a foto principal do produto com a legenda da oferta."""
        message = await self._bot.send_photo(
            chat_id=channel_id,
            photo=photo_url,
            caption=caption,
        )
        return message.message_id

    async def send_offer_message(self, channel_id: str | int, text: str) -> int:
        """Publica a oferta em formato de texto."""
        message = await self._bot.send_message(chat_id=channel_id, text=text)
        return message.message_id


def _load_interval_from_env() -> float:
    """Lê o intervalo de publicação (`PUBLISH_INTERVAL_SECONDS`) do `.env`."""
    value = env_float(
        "PUBLISH_INTERVAL_SECONDS",
        DEFAULT_PUBLISH_INTERVAL_SECONDS,
    )
    return value if value > 0 else DEFAULT_PUBLISH_INTERVAL_SECONDS


def extract_retry_seconds(exc: RetryAfter) -> float:
    """Extrai o tempo de espera em segundos de uma exceção `RetryAfter`."""
    raw_wait = getattr(exc, "_retry_after", None)
    if raw_wait is None:
        raw_wait = getattr(exc, "retry_after", 1.0)
    return (
        float(raw_wait.total_seconds())
        if hasattr(raw_wait, "total_seconds")
        else float(raw_wait)
    )


class OfferPublisher:
    """Gerencia o fluxo de aprovação, verificação de permissão e publicação no canal."""

    def __init__(
        self,
        sender: Any,
        channel_id: str | int | None = None,
        db_path: Path | str | None = None,
        interval_seconds: float | None = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_func: Callable[[], datetime] = get_brasilia_now,
        min_discount_percent: float | None = None,
        require_fishing_niche: bool = True,
        republish_cooldown_days: float | None = None,
        republish_min_drop_percent: float | None = None,
        pending_ttl_seconds: float = DEFAULT_PENDING_OFFERS_TTL_SECONDS,
    ) -> None:
        self._custom_channel_id = channel_id is not None
        self._custom_interval = interval_seconds is not None and interval_seconds > 0
        self._custom_min_discount = min_discount_percent is not None
        self._custom_cooldown = republish_cooldown_days is not None
        self._custom_min_drop = republish_min_drop_percent is not None

        raw_channel = (
            str(channel_id)
            if channel_id is not None
            else env_str("TELEGRAM_CHANNEL_ID")
        )
        self.channel_id = normalize_channel_id(raw_channel)
        self.db_path = Path(db_path) if db_path is not None else DATABASE_PATH
        self.interval_seconds = (
            float(interval_seconds)
            if self._custom_interval and interval_seconds is not None
            else _load_interval_from_env()
        )
        self.min_discount_percent = (
            max(0.0, float(min_discount_percent))
            if min_discount_percent is not None
            else env_float(
                "COPY_MIN_DISCOUNT_PERCENT",
                DEFAULT_MIN_DISCOUNT_PERCENT,
                min_value=0.0,
            )
        )
        self.require_fishing_niche = require_fishing_niche
        self.republish_cooldown_days = (
            max(0.0, float(republish_cooldown_days))
            if republish_cooldown_days is not None
            else env_float(
                "REPUBLISH_COOLDOWN_DAYS",
                DEFAULT_REPUBLISH_COOLDOWN_DAYS,
                min_value=0.0,
            )
        )
        self.republish_min_drop_percent = (
            max(0.0, float(republish_min_drop_percent))
            if republish_min_drop_percent is not None
            else env_float(
                "REPUBLISH_MIN_PRICE_DROP_PERCENT",
                DEFAULT_REPUBLISH_MIN_PRICE_DROP_PERCENT,
                min_value=0.0,
            )
        )
        self.pending_ttl_seconds = max(0.0, float(pending_ttl_seconds))

        self._sender = sender
        self._sleep = sleep_func
        self._now = now_func
        self._paused = False
        self._publish_lock = asyncio.Lock()

    def is_operating_hours(self) -> bool:
        """Retorna `True` se o horário atual de Brasília estiver entre 07:00 e 22:00."""
        return is_within_operating_hours(self._now())

    def reload_env(self) -> None:
        """Recarrega configurações operacionais diretamente do `.env`."""
        reload_env()
        if not self._custom_channel_id:
            self.channel_id = normalize_channel_id(env_str("TELEGRAM_CHANNEL_ID"))
        if not self._custom_interval:
            self.interval_seconds = _load_interval_from_env()
        if not self._custom_min_discount:
            self.min_discount_percent = env_float(
                "COPY_MIN_DISCOUNT_PERCENT",
                DEFAULT_MIN_DISCOUNT_PERCENT,
                min_value=0.0,
            )
        if not self._custom_cooldown:
            self.republish_cooldown_days = env_float(
                "REPUBLISH_COOLDOWN_DAYS",
                DEFAULT_REPUBLISH_COOLDOWN_DAYS,
                min_value=0.0,
            )
        if not self._custom_min_drop:
            self.republish_min_drop_percent = env_float(
                "REPUBLISH_MIN_PRICE_DROP_PERCENT",
                DEFAULT_REPUBLISH_MIN_PRICE_DROP_PERCENT,
                min_value=0.0,
            )

    @property
    def is_paused(self) -> bool:
        """Indica se as publicações automáticas estão pausadas."""
        return self._paused

    def pause(self) -> None:
        """Pausa o envio de ofertas para o canal."""
        self._paused = True
        logger.info("Publicações automáticas no canal foram PAUSADAS.")

    def resume(self) -> None:
        """Retoma o envio de ofertas para o canal."""
        self._paused = False
        logger.info("Publicações automáticas no canal foram RETOMADAS.")

    async def verify_channel_permission(self) -> bool:
        """Verifica se `channel_id` está configurado e se o bot pode postar nele."""
        if not self.channel_id:
            logger.error("TELEGRAM_CHANNEL_ID não está configurado no arquivo .env.")
            return False

        try:
            allowed = await self._sender.can_post_to_channel(self.channel_id)
        except (TimedOut, NetworkError, TelegramError, OSError) as exc:
            logger.error(
                "Erro ao verificar permissão do bot no canal %s: %s",
                self.channel_id,
                type(exc).__name__,
            )
            return False

        if not allowed:
            logger.error(
                "O bot não possui permissão para publicar no canal %s.",
                self.channel_id,
            )
            return False

        return True

    def _invalidate_sender_cache(self) -> None:
        if hasattr(self._sender, "invalidate_permission_cache"):
            self._sender.invalidate_permission_cache()

    async def _send_text_with_retry(self, text: str) -> str | int | None:
        """Envia uma mensagem de texto tratando limites de taxa (`RetryAfter`) e erros de rede."""
        try:
            return await self._sender.send_offer_message(self.channel_id, text)
        except RetryAfter as exc:
            wait_seconds = extract_retry_seconds(exc)
            logger.warning(
                "Limite da API do Telegram atingido (RetryAfter). Aguardando %.1fs para reenviar.",
                wait_seconds,
            )
            await self._sleep(wait_seconds)
            try:
                return await self._sender.send_offer_message(self.channel_id, text)
            except (TimedOut, NetworkError, TelegramError, OSError) as retry_exc:
                if isinstance(retry_exc, Forbidden):
                    self._invalidate_sender_cache()
                logger.error(
                    "Falha ao reenviar oferta após espera de limite: %s",
                    type(retry_exc).__name__,
                )
                return None
        except (TimedOut, NetworkError, TelegramError, OSError) as exc:
            if isinstance(exc, Forbidden):
                self._invalidate_sender_cache()
            logger.error(
                "Falha ao enviar oferta para o canal %s: %s",
                self.channel_id,
                type(exc).__name__,
            )
            return None

    async def _send_offer(self, offer: Offer, caption: str) -> str | int | None:
        """Tenta publicar com foto (`send_photo`) quando disponível, com fallback para texto.

        Atenção à hierarquia de exceções do `python-telegram-bot`:
        - `BadRequest` herda de `NetworkError` (que herda de `TelegramError`);
        - Por isso `BadRequest` é capturado ANTES de `(TimedOut, Forbidden, NetworkError)`,
          garantindo fallback para texto apenas quando a imagem é inválida/indisponível
          e abortando imediatamente quando a internet cai ou falta permissão no canal.
        """
        has_photo_method = hasattr(self._sender, "send_offer_photo")
        if is_valid_image_url(offer.image_url) and offer.image_url and has_photo_method:
            photo_url = offer.image_url.strip()
            send_photo_fn = getattr(self._sender, "send_offer_photo")
            try:
                return await send_photo_fn(self.channel_id, photo_url, caption)
            except RetryAfter as exc:
                wait_seconds = extract_retry_seconds(exc)
                logger.warning(
                    "Limite da API atingido no envio de foto. Aguardando %.1fs...",
                    wait_seconds,
                )
                await self._sleep(wait_seconds)
                try:
                    return await send_photo_fn(self.channel_id, photo_url, caption)
                except BadRequest as retry_bad_exc:
                    logger.warning(
                        "Imagem recusada para '%s' após espera (%s); usando fallback de texto.",
                        offer.title,
                        type(retry_bad_exc).__name__,
                    )
                except (TimedOut, Forbidden, NetworkError, RetryAfter, OSError) as retry_net_exc:
                    if isinstance(retry_net_exc, Forbidden):
                        self._invalidate_sender_cache()
                    logger.error(
                        "Falha de conexão ou permissão ao reenviar foto no canal %s: %s",
                        self.channel_id,
                        type(retry_net_exc).__name__,
                    )
                    return None
                except (TelegramError, ValueError) as retry_other_exc:
                    logger.warning(
                        "Não foi possível enviar a imagem de '%s' (%s); usando fallback de texto.",
                        offer.title,
                        type(retry_other_exc).__name__,
                    )
            except BadRequest as exc:
                logger.warning(
                    "Imagem indisponível para '%s' (%s); publicando em formato de texto.",
                    offer.title,
                    type(exc).__name__,
                )
            except (TimedOut, Forbidden, NetworkError, OSError) as exc:
                if isinstance(exc, Forbidden):
                    self._invalidate_sender_cache()
                logger.error(
                    "Falha de conexão ou permissão ao enviar foto para o canal %s: %s",
                    self.channel_id,
                    type(exc).__name__,
                )
                return None
            except (TelegramError, ValueError) as exc:
                logger.warning(
                    "Imagem indisponível para '%s' (%s); publicando em formato de texto.",
                    offer.title,
                    type(exc).__name__,
                )

        return await self._send_text_with_retry(caption)

    def _filter_candidates(self, offers: list[Offer]) -> list[Offer]:
        return filter_offers(
            offers,
            min_discount_percent=self.min_discount_percent,
            require_fishing_niche=self.require_fishing_niche,
        )

    def _can_publish_now(self, offer: Offer) -> bool:
        if not self.channel_id:
            return True
        return should_publish_offer(
            offer=offer,
            channel_id=self.channel_id,
            cooldown_days=self.republish_cooldown_days,
            min_price_drop_percent=self.republish_min_drop_percent,
            db_path=self.db_path,
            now=self._now(),
        )

    async def publish_approved_offers(
        self,
        offers: list[Offer],
        max_offers: int | None = None,
        ignore_pause: bool = False,
    ) -> list[Offer]:
        """Filtra ofertas válidas, aplica regras de republicação e envia ao canal.

        Args:
            offers: Lista de ofertas candidatas coletadas das fontes.
            max_offers: Quantidade máxima de postagens nesta chamada.
            ignore_pause: Quando `True` (usado pelo comando administrativo `/publicar`),
                permite publicar mesmo que o loop periódico esteja pausado, sem
                precisar alternar o estado `_paused`.
        """
        if self._paused and not ignore_pause:
            logger.info("Publicação ignorada: o publicador está pausado.")
            return []

        if not self.is_operating_hours():
            logger.info(
                "Publicação ignorada: fora do horário de exibição (07:00 às 22:00 - Horário de Brasília)."
            )
            return []

        if max_offers is not None and max_offers <= 0:
            return []

        async with self._publish_lock:
            if self._paused and not ignore_pause:
                return []

            initialize_database(self.db_path)

            valid_offers = self._filter_candidates(offers)
            if not valid_offers:
                logger.info("Nenhuma oferta válida para publicar no canal.")
                return []

            if not await self.verify_channel_permission():
                return []

            published_now: list[Offer] = []

            for offer in valid_offers:
                if not self._can_publish_now(offer):
                    logger.info(
                        "Oferta em período de cooldown no canal (ignorando): %s",
                        offer.url,
                    )
                    continue

                save_offer(offer, db_path=self.db_path)
                caption = format_offer(offer)
                message_id = await self._send_offer(offer, caption)

                if message_id is None:
                    continue

                record_publication(
                    offer=offer,
                    channel_id=self.channel_id,
                    message_id=message_id,
                    db_path=self.db_path,
                    published_at=self._now(),
                )
                logger.info(
                    "Oferta publicada com sucesso no canal %s: %s (msg_id=%s)",
                    self.channel_id,
                    offer.title,
                    message_id,
                )
                published_now.append(offer)

                if max_offers is not None and len(published_now) >= max_offers:
                    break

            return published_now

    async def run_periodic_loop(
        self,
        fetch_offers_callback: Callable[[], list[Offer]],
        max_cycles: int | None = None,
    ) -> None:
        """Publica no máximo 1 oferta elegível a cada `interval_seconds` (padrão: 180s = 3 min)."""
        cycles = 0
        pending_offers: list[Offer] = []
        pending_fetched_at: datetime | None = None

        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            self.reload_env()

            if self._paused:
                logger.info("Ciclo %s ignorado porque as publicações estão pausadas.", cycles)
            elif not self.is_operating_hours():
                # Descarta fila pendente ao sair da janela 07:00-22:00 para nunca
                # publicar pela manhã preços coletados na noite anterior.
                pending_offers = []
                pending_fetched_at = None
                logger.info(
                    "Ciclo %s ignorado: fora do horário permitido (07:00 às 22:00 - Horário de Brasília).",
                    cycles,
                )
            else:
                try:
                    initialize_database(self.db_path)
                    now_dt = self._now()

                    if (
                        pending_offers
                        and pending_fetched_at is not None
                        and self.pending_ttl_seconds > 0
                        and (now_dt - pending_fetched_at).total_seconds()
                        >= self.pending_ttl_seconds
                    ):
                        logger.info(
                            "Fila em memória expirou após %.0fs; renovando coleta.",
                            self.pending_ttl_seconds,
                        )
                        pending_offers = []
                        pending_fetched_at = None

                    if self.channel_id:
                        pending_offers = [
                            o for o in pending_offers if self._can_publish_now(o)
                        ]

                    if not pending_offers:
                        fetched = await asyncio.to_thread(fetch_offers_callback)
                        pending_fetched_at = self._now()
                        pending_offers = self._filter_candidates(fetched)
                        if self.channel_id:
                            pending_offers = [
                                o for o in pending_offers if self._can_publish_now(o)
                            ]

                    if pending_offers:
                        published = await self.publish_approved_offers(
                            pending_offers,
                            max_offers=1,
                        )
                        if published and self.channel_id:
                            pending_offers = [
                                o for o in pending_offers if self._can_publish_now(o)
                            ]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Erro inesperado durante o ciclo de coleta/publicação: %s",
                        type(exc).__name__,
                    )

            if max_cycles is not None and cycles >= max_cycles:
                break

            await self._sleep(self.interval_seconds)
