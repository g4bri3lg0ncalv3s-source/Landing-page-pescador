"""Motor de publicação automatizada de ofertas (com imagem e fallback) no Telegram.

Responsabilidades deste módulo:
- Ler o identificador do canal (`TELEGRAM_CHANNEL_ID`) e o intervalo
  (`PUBLISH_INTERVAL_SECONDS`) a partir do `.env`;
- Verificar se o bot possui permissão de administrador para postar no canal;
- Publicar ofertas com imagem (`send_photo`) quando houver `image_url` válida;
- Realizar fallback automático para texto (`send_message`) caso a oferta não
  tenha imagem ou caso a imagem esteja indisponível no servidor de origem;
- Registrar cada envio na tabela `publications` do SQLite (evitando duplicatas
  mesmo se o bot for reiniciado);
- Tratar limites de envio do Telegram (`RetryAfter`), quedas de rede e timeouts;
- Permitir pausar (`pause()`) e retomar (`resume()`) as publicações.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from dotenv import load_dotenv
from telegram import Bot
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

from pesca_bot.database import (
    DATABASE_PATH,
    initialize_database,
    is_offer_published,
    record_publication,
    save_offer,
)
from pesca_bot.offers import Offer, filter_offers, format_offer, is_valid_image_url

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=True)

DEFAULT_PUBLISH_INTERVAL_SECONDS = 180.0

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
    """Padroniza o identificador do canal do Telegram.

    Na API de Bots do Telegram, todo ID numérico de canal começa com `-100`.
    Se o usuário copiar o ID pelo Telegram Web (ex.: `-3913046292`),
    esta função converte automaticamente para `-1003913046292`.
    """
    if raw_channel_id is None:
        return ""
    value = str(raw_channel_id).strip()
    if not value:
        return ""
    if value.startswith("-") and value[1:].isdigit() and not value.startswith("-100"):
        if len(value[1:]) >= 10:
            return f"-100{value[1:]}"
    return value


class ChannelSender(Protocol):
    """Interface de envio de mensagens e fotos para o canal (real ou simulada)."""

    async def can_post_to_channel(self, channel_id: str | int) -> bool:
        """Retorna `True` se o bot tiver permissão para publicar no canal."""

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

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def can_post_to_channel(self, channel_id: str | int) -> bool:
        """Consulta o Telegram para confirmar se o bot é administrador com permissão de postagem."""
        bot_user = await self._bot.get_me()
        member = await self._bot.get_chat_member(chat_id=channel_id, user_id=bot_user.id)

        if member.status not in ("administrator", "creator"):
            return False

        can_post = getattr(member, "can_post_messages", None)
        return can_post is not False

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
    raw = os.getenv("PUBLISH_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_PUBLISH_INTERVAL_SECONDS
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_PUBLISH_INTERVAL_SECONDS
    except ValueError:
        return DEFAULT_PUBLISH_INTERVAL_SECONDS


class OfferPublisher:
    """Gerencia o fluxo de aprovação, verificação de permissão e publicação no canal."""

    def __init__(
        self,
        sender: ChannelSender,
        channel_id: str | int | None = None,
        db_path: Path | str | None = None,
        interval_seconds: float | None = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_func: Callable[[], datetime] = get_brasilia_now,
    ) -> None:
        self._custom_channel_id = channel_id is not None
        self._custom_interval = interval_seconds is not None and interval_seconds > 0
        raw_channel = (
            str(channel_id)
            if channel_id is not None
            else os.getenv("TELEGRAM_CHANNEL_ID", "")
        )
        self.channel_id = normalize_channel_id(raw_channel)
        self.db_path = Path(db_path) if db_path is not None else DATABASE_PATH
        self.interval_seconds = (
            float(interval_seconds)
            if self._custom_interval and interval_seconds is not None
            else _load_interval_from_env()
        )
        self._sender = sender
        self._sleep = sleep_func
        self._now = now_func
        self._paused = False

    def is_operating_hours(self) -> bool:
        """Retorna `True` se o horário atual de Brasília estiver entre 07:00 e 22:00."""
        return is_within_operating_hours(self._now())

    def reload_env(self) -> None:
        """Recarrega o `TELEGRAM_CHANNEL_ID` e o intervalo diretamente do arquivo `.env`."""
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        if not self._custom_channel_id:
            self.channel_id = normalize_channel_id(
                os.getenv("TELEGRAM_CHANNEL_ID", "")
            )
        if not self._custom_interval:
            self.interval_seconds = _load_interval_from_env()

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

    @staticmethod
    def _extract_retry_seconds(exc: RetryAfter) -> float:
        raw_wait = getattr(exc, "_retry_after", None) or getattr(exc, "retry_after", 1.0)
        return (
            float(raw_wait.total_seconds())
            if hasattr(raw_wait, "total_seconds")
            else float(raw_wait)
        )

    async def _send_text_with_retry(self, text: str) -> str | int | None:
        """Envia uma mensagem de texto tratando limites de taxa (`RetryAfter`) e erros de rede."""
        try:
            return await self._sender.send_offer_message(self.channel_id, text)
        except RetryAfter as exc:
            wait_seconds = self._extract_retry_seconds(exc)
            logger.warning(
                "Limite da API do Telegram atingido (RetryAfter). Aguardando %.1fs para reenviar.",
                wait_seconds,
            )
            await self._sleep(wait_seconds)
            try:
                return await self._sender.send_offer_message(self.channel_id, text)
            except (TimedOut, NetworkError, TelegramError, OSError) as retry_exc:
                logger.error(
                    "Falha ao reenviar oferta após espera de limite: %s",
                    type(retry_exc).__name__,
                )
                return None
        except (TimedOut, NetworkError, TelegramError, OSError) as exc:
            logger.error(
                "Falha ao enviar oferta para o canal %s: %s",
                self.channel_id,
                type(exc).__name__,
            )
            return None

    async def _send_offer(self, offer: Offer, caption: str) -> str | int | None:
        """Tenta publicar com foto (`send_photo`) quando disponível, com fallback para texto."""
        has_photo_method = hasattr(self._sender, "send_offer_photo")
        if is_valid_image_url(offer.image_url) and offer.image_url and has_photo_method:
            photo_url = offer.image_url.strip()
            send_photo_fn = getattr(self._sender, "send_offer_photo")
            try:
                return await send_photo_fn(self.channel_id, photo_url, caption)
            except RetryAfter as exc:
                wait_seconds = self._extract_retry_seconds(exc)
                logger.warning(
                    "Limite da API atingido no envio de foto. Aguardando %.1fs...",
                    wait_seconds,
                )
                await self._sleep(wait_seconds)
                try:
                    return await send_photo_fn(self.channel_id, photo_url, caption)
                except (BadRequest, TimedOut, NetworkError, TelegramError, OSError):
                    logger.warning(
                        "Não foi possível enviar a imagem de '%s' após espera; usando fallback de texto.",
                        offer.title,
                    )
            except (BadRequest, TelegramError, ValueError) as exc:
                # Se a imagem falhar (ex.: URL expirada ou recusada pelo Telegram),
                # registra um aviso e publica normalmente em formato de texto!
                logger.warning(
                    "Imagem indisponível para '%s' (%s); publicando em formato de texto.",
                    offer.title,
                    type(exc).__name__,
                )
            except (TimedOut, NetworkError, OSError) as exc:
                logger.error(
                    "Falha de conexão ao enviar foto para o canal %s: %s",
                    self.channel_id,
                    type(exc).__name__,
                )
                return None

        return await self._send_text_with_retry(caption)

    async def publish_approved_offers(
        self,
        offers: list[Offer],
        max_offers: int | None = None,
    ) -> list[Offer]:
        """Filtra ofertas válidas, ignora as já publicadas e envia as inéditas ao canal.

        Args:
            offers: Lista de ofertas candidatas coletadas das fontes.
            max_offers: Quantidade máxima de postagens a enviar nesta chamada
                (ex.: `1` para enviar apenas 1 post por intervalo). Se `None`,
                envia todas as inéditas da lista.
        """
        if self._paused:
            logger.info("Publicação ignorada: o publicador está pausado.")
            return []

        if not self.is_operating_hours():
            logger.info(
                "Publicação ignorada: fora do horário de exibição (07:00 às 22:00 - Horário de Brasília)."
            )
            return []

        if max_offers is not None and max_offers <= 0:
            return []

        initialize_database(self.db_path)

        valid_offers = filter_offers(offers)
        if not valid_offers:
            logger.info("Nenhuma oferta válida para publicar no canal.")
            return []

        if not await self.verify_channel_permission():
            return []

        published_now: list[Offer] = []

        for offer in valid_offers:
            if is_offer_published(offer.url, self.channel_id, db_path=self.db_path):
                logger.info("Oferta já publicada anteriormente (ignorando): %s", offer.url)
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
        """Publica no máximo 1 oferta inédita a cada `interval_seconds` (padrão: 180s = 3 min)."""
        cycles = 0
        pending_offers: list[Offer] = []

        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            if self._paused:
                logger.info("Ciclo %s ignorado porque as publicações estão pausadas.", cycles)
            elif not self.is_operating_hours():
                logger.info(
                    "Ciclo %s ignorado: fora do horário permitido (07:00 às 22:00 - Horário de Brasília).",
                    cycles,
                )
            else:
                try:
                    initialize_database(self.db_path)
                    if self.channel_id:
                        pending_offers = [
                            o
                            for o in pending_offers
                            if not is_offer_published(o.url, self.channel_id, db_path=self.db_path)
                        ]

                    if not pending_offers:
                        fetched = await asyncio.to_thread(fetch_offers_callback)
                        pending_offers = filter_offers(fetched)
                        if self.channel_id:
                            pending_offers = [
                                o
                                for o in pending_offers
                                if not is_offer_published(o.url, self.channel_id, db_path=self.db_path)
                            ]

                    if pending_offers:
                        published = await self.publish_approved_offers(
                            pending_offers,
                            max_offers=1,
                        )
                        if published and self.channel_id:
                            pending_offers = [
                                o
                                for o in pending_offers
                                if not is_offer_published(o.url, self.channel_id, db_path=self.db_path)
                            ]
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Erro inesperado durante o ciclo de coleta/publicação: %s",
                        type(exc).__name__,
                    )

            if max_cycles is not None and cycles >= max_cycles:
                break

            await self._sleep(self.interval_seconds)
