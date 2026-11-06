"""Testes de segurança, autenticação ADM (Modo Stealth + Fail-Closed) e SSRF."""

import asyncio
from datetime import datetime
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.constants import ChatType

from pesca_bot.affiliate import extract_affiliate_params_from_url, is_allowed_affiliate_url
from pesca_bot.offers import Offer
import pesca_bot.publisher as publisher_module
from pesca_bot.publisher import BRASILIA_TZ, OfferPublisher, SimulatedChannelSender
import pesca_bot.telegram_bot as bot_module
from pesca_bot.telegram_bot import (
    ajuda,
    build_application,
    ofertas,
    parse_admin_ids,
    pausar,
    publicar,
    retomar,
    start,
    status,
)


def _make_fake_update(
    user_id: int | None = 111222333,
    chat_type: str = ChatType.PRIVATE,
    is_bot: bool = False,
) -> SimpleNamespace:
    message = SimpleNamespace(
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    user = (
        SimpleNamespace(id=user_id, is_bot=is_bot)
        if user_id is not None
        else None
    )
    chat = SimpleNamespace(id=user_id or -999, type=chat_type)
    return SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        message=message,
    )


def _make_fake_context(publisher: OfferPublisher | None = None) -> SimpleNamespace:
    bot_data = {"publisher": publisher} if publisher is not None else {}
    return SimpleNamespace(bot_data=bot_data)


def test_parse_admin_ids_interpreta_multiplos_formatos_e_ignora_invalidos():
    """Garante parsing seguro de IDs separados por vírgula, espaço ou ponto e vírgula."""
    assert parse_admin_ids("") == set()
    assert parse_admin_ids("   ") == set()
    assert parse_admin_ids("12345, 67890; 999") == {12345, 67890, 999}
    assert parse_admin_ids("abc, -50, 0, 42") == {42}


def test_modo_fail_closed_quando_admin_ids_vazio(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Se TELEGRAM_ADMIN_IDS estiver vazio, nenhum comando responde no Telegram."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "")
    update = _make_fake_update(user_id=777888999, chat_type=ChatType.PRIVATE)
    context = _make_fake_context()

    with caplog.at_level(logging.WARNING):
        for cmd in (start, ajuda, ofertas, publicar, pausar, retomar, status):
            asyncio.run(cmd(update, context))

    update.message.reply_text.assert_not_awaited()
    update.message.reply_photo.assert_not_awaited()
    assert "777888999" in caplog.text
    assert "Acesso bloqueado" in caplog.text


def test_bloqueia_usuario_nao_autorizado_em_stealth_sem_alterar_estado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Intruso não consegue pausar, retomar, ver status nem receber resposta do bot."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "100001")
    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=tmp_path / "auth.db",
    )
    assert publisher.is_paused is False

    intruso_update = _make_fake_update(user_id=999999, chat_type=ChatType.PRIVATE)
    context = _make_fake_context(publisher=publisher)

    asyncio.run(pausar(intruso_update, context))
    asyncio.run(status(intruso_update, context))
    asyncio.run(publicar(intruso_update, context))

    assert publisher.is_paused is False
    assert sender.sent_messages == []
    intruso_update.message.reply_text.assert_not_awaited()


def test_bloqueia_comando_de_adm_fora_de_chat_privado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Mesmo um ADM legítimo é ignorado se enviar comandos em grupos públicos."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "100001")
    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=tmp_path / "grupo.db",
    )

    adm_em_grupo = _make_fake_update(user_id=100001, chat_type=ChatType.SUPERGROUP)
    context = _make_fake_context(publisher=publisher)

    asyncio.run(status(adm_em_grupo, context))
    asyncio.run(pausar(adm_em_grupo, context))

    assert publisher.is_paused is False
    adm_em_grupo.message.reply_text.assert_not_awaited()


def test_adm_autorizado_em_dm_executa_publicar_mantendo_estado_pausado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """ADM em DM publica normalmente via /publicar sem destravar o estado pausado."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "100001, 200002")
    horario_ativo = datetime(2026, 10, 28, 14, 0, 0, tzinfo=BRASILIA_TZ)
    monkeypatch.setattr(publisher_module, "get_brasilia_now", lambda: horario_ativo)

    oferta = Offer(
        title="Carretilha Shimano Curado Pesca",
        price=799.0,
        original_price=999.0,
        url="https://www.mercadolivre.com.br/p/MLB555777",
        source="Mercado Livre",
    )
    monkeypatch.setattr(bot_module, "collect_offers", lambda: [oferta])

    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=tmp_path / "adm_ok.db",
        now_func=lambda: horario_ativo,
    )
    publisher.pause()

    adm_dm = _make_fake_update(user_id=200002, chat_type=ChatType.PRIVATE)
    context = _make_fake_context(publisher=publisher)

    asyncio.run(publicar(adm_dm, context))

    assert len(sender.sent_messages) == 1
    assert publisher.is_paused is True
    assert adm_dm.message.reply_text.await_count == 2


def test_auto_publish_inicia_pausado_por_padrao(monkeypatch: pytest.MonkeyPatch):
    """Sem AUTO_PUBLISH_ENABLED definido, o publicador deve iniciar pausado."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:TOKEN_DE_TESTE_VALIDO_ABC123456")
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "100001")
    monkeypatch.delenv("AUTO_PUBLISH_ENABLED", raising=False)

    app = build_application()
    publisher: OfferPublisher = app.bot_data["publisher"]
    assert publisher.is_paused is True


def test_protecao_ssrf_em_links_curtos_de_afiliado(monkeypatch: pytest.MonkeyPatch):
    """Garante que URLs maliciosas ou redirecionamentos internos sejam bloqueados."""
    assert is_allowed_affiliate_url("https://mercadolivre.com/sec/abc123") is True
    assert is_allowed_affiliate_url("https://meli.la/xyz987") is True
    assert is_allowed_affiliate_url("http://mercadolivre.com/sec/abc123") is False
    assert (
        is_allowed_affiliate_url("https://127.0.0.1/?q=mercadolivre.com/sec/abc")
        is False
    )
    assert (
        is_allowed_affiliate_url("https://atacante.com/mercadolivre.com/sec/abc")
        is False
    )
    assert (
        is_allowed_affiliate_url("https://mercadolivre.com:8443/sec/abc123")
        is False
    )

    chamadas_rede: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str) -> httpx.Response:
            chamadas_rede.append(url)
            # Simula redirecionamento malicioso para metadados internos
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)

    # Substring em domínio falso nem sequer abre conexão
    assert (
        extract_affiliate_params_from_url(
            "https://evil.example/?next=https://mercadolivre.com/sec/123"
        )
        == {}
    )
    assert chamadas_rede == []

    # Domínio inicial válido, mas redirecionamento para IP interno é abortado na hora
    assert extract_affiliate_params_from_url("https://mercadolivre.com/sec/123") == {}
    assert chamadas_rede == ["https://mercadolivre.com/sec/123"]

