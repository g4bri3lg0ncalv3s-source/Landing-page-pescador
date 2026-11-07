"""Testes automatizados para a publicação de ofertas (com foto e texto) no Telegram.

Cobre os cenários obrigatórios:
- Cenário 1: Publicação de uma oferta com imagem válida (`send_offer_photo`);
- Cenário 2: Publicação de uma oferta sem imagem (`send_offer_message`);
- Cenário 3: Tratamento de imagem inválida ou indisponível (fallback automático para texto);
- Cenário 9: Tratamento de erros do Telegram (`RetryAfter` e `NetworkError`);
- Cenário 10: Ausência de publicação duplicada (mesmo após reiniciar a aplicação).
"""

import asyncio
from datetime import datetime
import logging
from pathlib import Path

import pytest
from telegram.error import NetworkError, RetryAfter

from pesca_bot.database import is_offer_published
from pesca_bot.offers import Offer
import pesca_bot.publisher as publisher_module
from pesca_bot.publisher import (
    BRASILIA_TZ,
    OfferPublisher,
    SimulatedChannelSender,
    is_within_operating_hours,
    normalize_channel_id,
)


@pytest.fixture(autouse=True)
def _horario_comercial_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixa o relógio padrão dos testes às 14:00 (Horário de Brasília) por padrão."""
    horario_ativo = datetime(2026, 10, 28, 14, 0, 0, tzinfo=BRASILIA_TZ)
    monkeypatch.setattr(publisher_module, "get_brasilia_now", lambda: horario_ativo)
    monkeypatch.setattr(
        OfferPublisher,
        "__init__",
        _wrap_init_with_default_now(OfferPublisher.__init__, lambda: horario_ativo),
    )


def _wrap_init_with_default_now(orig_init, default_now_func):
    def _wrapped(self, *args, **kwargs):
        if "now_func" not in kwargs:
            kwargs["now_func"] = default_now_func
        return orig_init(self, *args, **kwargs)

    return _wrapped


def test_publica_oferta_com_imagem_valida_e_sem_imagem(tmp_path: Path):
    """Cenários 1 e 2: Publica foto quando há image_url válida e texto quando não há."""
    banco_teste = tmp_path / "fotos_e_texto.db"
    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=banco_teste,
    )

    oferta_com_foto = Offer(
        title="Molinete Pesca Albatroz",
        price=47.61,
        original_price=78.99,
        url="https://www.mercadolivre.com.br/p/MLB28387728",
        source="Mercado Livre",
        image_url="https://http2.mlstatic.com/D_NQ_NP_880097-MLU73068598169_112023-F.jpg",
    )
    oferta_sem_foto = Offer(
        title="Vara de pesca carbono",
        price=99.90,
        original_price=149.90,
        url="https://www.mercadolivre.com.br/p/MLB11111111",
        source="Mercado Livre",
        image_url=None,
    )

    publicadas = asyncio.run(
        publisher.publish_approved_offers([oferta_com_foto, oferta_sem_foto])
    )

    assert len(publicadas) == 2
    assert len(sender.sent_messages) == 2

    # A oferta de maior desconto (Molinete ~40%) foi enviada como foto (`photo`)
    msg_foto = sender.sent_messages[0]
    assert msg_foto["type"] == "photo"
    assert (
        msg_foto["photo_url"]
        == "https://http2.mlstatic.com/D_NQ_NP_880097-MLU73068598169_112023-F.jpg"
    )
    assert "Molinete Pesca Albatroz" in msg_foto["text"]

    # A oferta sem imagem foi enviada como texto (`text`)
    msg_texto = sender.sent_messages[1]
    assert msg_texto["type"] == "text"
    assert msg_texto["photo_url"] is None
    assert "Vara de pesca carbono" in msg_texto["text"]


def test_fallback_para_texto_quando_imagem_invalida_ou_indisponivel(tmp_path: Path):
    """Cenário 3: Se a imagem falhar no Telegram ou for inválida, publica em texto sem parar."""
    banco_teste = tmp_path / "fallback_imagem.db"
    url_imagem_quebrada = "https://cdn.loja.example/imagem-expirada.jpg"

    sender = SimulatedChannelSender(
        has_permission=True,
        fail_photo_urls={url_imagem_quebrada},
    )
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=banco_teste,
    )

    oferta_foto_indisponivel = Offer(
        title="Carretilha Marine Sports",
        price=180.0,
        original_price=240.0,
        url="https://loja.example/carretilha-1",
        source="Loja Pesca",
        image_url=url_imagem_quebrada,
    )
    oferta_url_imagem_invalida = Offer(
        title="Isca Artificial Meia Água",
        price=25.0,
        original_price=40.0,
        url="https://loja.example/isca-2",
        source="Loja Pesca",
        image_url="http://link-inseguro-sem-https/foto.jpg",
    )

    publicadas = asyncio.run(
        publisher.publish_approved_offers(
            [oferta_foto_indisponivel, oferta_url_imagem_invalida]
        )
    )

    # Ambas são publicadas com sucesso em formato de texto
    assert len(publicadas) == 2
    assert len(sender.sent_messages) == 2
    assert sender.sent_messages[0]["type"] == "text"
    assert sender.sent_messages[1]["type"] == "text"


def test_publica_apenas_ofertas_validas_e_evita_duplicacao_apos_reinicio(
    tmp_path: Path,
):
    """Cenário 10: Garante que só ofertas válidas sejam postadas e não dupliquem após reiniciar."""
    banco_teste = tmp_path / "publicacoes.db"
    sender_1 = SimulatedChannelSender(has_permission=True)

    publisher_1 = OfferPublisher(
        sender=sender_1,
        channel_id="-100999888777",
        db_path=banco_teste,
        interval_seconds=60.0,
    )

    ofertas_lote = [
        Offer(
            title="Vara de pesca carbono",
            price=99.90,
            original_price=149.90,
            url="https://loja.example/vara?utm_source=teste",
            source="Loja Pesca",
        ),
        Offer(
            title="Oferta com preço inválido",
            price=350.0,
            original_price=300.0,
            url="https://loja.example/invalida",
            source="Loja Pesca",
        ),
    ]

    publicadas_1 = asyncio.run(publisher_1.publish_approved_offers(ofertas_lote))

    assert len(publicadas_1) == 1
    assert publicadas_1[0].title == "Vara de pesca carbono"
    assert len(sender_1.sent_messages) == 1
    assert is_offer_published(
        "https://loja.example/vara",
        "-100999888777",
        db_path=banco_teste,
    )

    sender_apos_reinicio = SimulatedChannelSender(has_permission=True)
    publisher_apos_reinicio = OfferPublisher(
        sender=sender_apos_reinicio,
        channel_id="-100999888777",
        db_path=banco_teste,
    )

    publicadas_2 = asyncio.run(
        publisher_apos_reinicio.publish_approved_offers(ofertas_lote)
    )

    assert publicadas_2 == []
    assert sender_apos_reinicio.sent_messages == []


def test_bloqueia_publicacao_sem_permissao_no_canal(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """Verifica que o bot não publica caso não tenha permissão de administrador no canal."""
    banco_teste = tmp_path / "sem_permissao.db"
    sender = SimulatedChannelSender(has_permission=False)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="@canal_pesca",
        db_path=banco_teste,
    )

    oferta = Offer(
        title="Molinete Pesca Esportiva",
        price=120.0,
        original_price=180.0,
        url="https://loja.example/molinete",
        source="Loja Pesca",
    )

    with caplog.at_level(logging.ERROR):
        resultado = asyncio.run(publisher.publish_approved_offers([oferta]))

    assert resultado == []
    assert sender.sent_messages == []
    assert "não possui permissão" in caplog.text


def test_pausar_e_retomar_publicacoes(tmp_path: Path):
    """Verifica que `pause()` interrompe os envios e `resume()` retoma normalmente."""
    banco_teste = tmp_path / "pausa.db"
    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="@canal_pesca",
        db_path=banco_teste,
    )

    oferta = Offer(
        title="Carretilha Perfil Baixo",
        price=199.90,
        original_price=299.90,
        url="https://loja.example/carretilha",
        source="Loja Pesca",
    )

    publisher.pause()
    assert publisher.is_paused is True
    assert asyncio.run(publisher.publish_approved_offers([oferta])) == []
    assert sender.sent_messages == []

    publisher.resume()
    assert publisher.is_paused is False
    publicadas = asyncio.run(publisher.publish_approved_offers([oferta]))
    assert len(publicadas) == 1
    assert len(sender.sent_messages) == 1


def test_trata_limite_retry_after_e_falha_de_rede(tmp_path: Path):
    """Cenário 9: Testa reenvio automático em caso de RetryAfter e segurança em falha de rede."""
    banco_teste = tmp_path / "erros_telegram.db"
    esperas_registradas: list[float] = []

    async def fake_sleep(segundos: float) -> None:
        esperas_registradas.append(segundos)

    class SenderComInstabilidade:
        def __init__(self) -> None:
            self.tentativas = 0

        async def can_post_to_channel(self, channel_id: str | int) -> bool:
            return True

        async def send_offer_message(self, channel_id: str | int, text: str) -> str:
            self.tentativas += 1
            if self.tentativas == 1:
                raise RetryAfter(2)
            if self.tentativas == 3:
                raise NetworkError("Queda temporária de conexão")
            return f"msg-{self.tentativas}"

    sender = SenderComInstabilidade()
    publisher = OfferPublisher(
        sender=sender,
        channel_id="@canal_pesca",
        db_path=banco_teste,
        sleep_func=fake_sleep,
    )

    oferta_1 = Offer(
        title="Isca Artificial Meia Água",
        price=29.90,
        original_price=49.90,
        url="https://loja.example/isca-1",
        source="Loja Pesca",
    )
    oferta_2_falha_rede = Offer(
        title="Linha Multifilamento 8X",
        price=59.90,
        original_price=89.90,
        url="https://loja.example/linha-2",
        source="Loja Pesca",
    )

    publicadas = asyncio.run(
        publisher.publish_approved_offers([oferta_1, oferta_2_falha_rede])
    )

    assert esperas_registradas == [2.0]
    assert len(publicadas) == 1
    assert publicadas[0].title == "Isca Artificial Meia Água"
    assert is_offer_published(oferta_1.url, "@canal_pesca", db_path=banco_teste) is True
    assert (
        is_offer_published(oferta_2_falha_rede.url, "@canal_pesca", db_path=banco_teste)
        is False
    )


def test_ciclo_periodico_configuravel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Testa configuração por variável de ambiente e ciclo periódico de publicação."""
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100111222")
    monkeypatch.setenv("PUBLISH_INTERVAL_SECONDS", "120")

    intervalos_aguardados: list[float] = []

    async def fake_sleep(segundos: float) -> None:
        intervalos_aguardados.append(segundos)

    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        db_path=tmp_path / "ciclo.db",
        sleep_func=fake_sleep,
    )

    assert publisher.channel_id == "-100111222"
    assert publisher.interval_seconds == 120.0

    oferta = Offer(
        title="Anzol Marine Sports",
        price=15.0,
        original_price=25.0,
        url="https://loja.example/anzol",
        source="Loja Pesca",
    )

    asyncio.run(publisher.run_periodic_loop(lambda: [oferta], max_cycles=2))

    assert intervalos_aguardados == [120.0]
    assert len(sender.sent_messages) == 1


def test_bloqueia_promocoes_apos_22h_e_retoma_as_07h_brasilia(tmp_path: Path):
    """Garante que promoções só apareçam das 07:00 às 22:00 (Horário de Brasília)."""
    assert is_within_operating_hours(datetime(2026, 10, 28, 6, 59, tzinfo=BRASILIA_TZ)) is False
    assert is_within_operating_hours(datetime(2026, 10, 28, 7, 0, tzinfo=BRASILIA_TZ)) is True
    assert is_within_operating_hours(datetime(2026, 10, 28, 21, 59, tzinfo=BRASILIA_TZ)) is True
    assert is_within_operating_hours(datetime(2026, 10, 28, 22, 0, tzinfo=BRASILIA_TZ)) is False
    assert is_within_operating_hours(datetime(2026, 10, 29, 0, 30, tzinfo=BRASILIA_TZ)) is False

    relogio = [datetime(2026, 10, 28, 22, 15, tzinfo=BRASILIA_TZ)]
    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-100111222",
        db_path=tmp_path / "horario.db",
        now_func=lambda: relogio[0],
    )

    oferta = Offer(
        title="Molinete Shimano FX 4000",
        price=179.90,
        original_price=249.90,
        url="https://loja.example/shimano-fx",
        source="Mercado Livre",
    )

    # Às 22:15 (após as 22:00 BRT) -> não deve publicar
    assert asyncio.run(publisher.publish_approved_offers([oferta])) == []
    assert sender.sent_messages == []

    # Às 06:59 BRT -> ainda bloqueado
    relogio[0] = datetime(2026, 10, 29, 6, 59, tzinfo=BRASILIA_TZ)
    assert asyncio.run(publisher.publish_approved_offers([oferta])) == []
    assert sender.sent_messages == []

    # Às 07:00 BRT -> volta a publicar normalmente
    relogio[0] = datetime(2026, 10, 29, 7, 0, tzinfo=BRASILIA_TZ)
    publicadas = asyncio.run(publisher.publish_approved_offers([oferta]))
    assert len(publicadas) == 1
    assert len(sender.sent_messages) == 1


def test_publica_um_post_a_cada_dez_minutos_sem_espamar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Garante que o loop periódico envie apenas 1 post por vez com intervalo padrão de 600s (10 min)."""
    monkeypatch.delenv("PUBLISH_INTERVAL_SECONDS", raising=False)
    esperas: list[float] = []

    async def fake_sleep(segundos: float) -> None:
        esperas.append(segundos)

    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=tmp_path / "cadencia.db",
        sleep_func=fake_sleep,
    )

    assert publisher.interval_seconds == 600.0

    lote_ofertas = [
        Offer(
            title=f"Molinete Pesca Modelo {i}",
            price=100.0 - i,
            original_price=200.0,
            url=f"https://loja.example/molinete-{i}",
            source="Shopee",
        )
        for i in range(4)
    ]

    chamadas_coleta = [0]

    def coletar() -> list[Offer]:
        chamadas_coleta[0] += 1
        return lote_ofertas

    # Em 3 ciclos deve enviar exatamente 3 posts (1 por ciclo), aguardando 600s entre cada um
    asyncio.run(publisher.run_periodic_loop(coletar, max_cycles=3))

    assert len(sender.sent_messages) == 3
    assert esperas == [600.0, 600.0]
    assert chamadas_coleta[0] == 1


def test_queda_de_rede_em_send_photo_nao_dispara_fallback_de_texto(tmp_path: Path):
    """OPS-02: NetworkError real na foto deve abortar envio em vez de tentar texto."""

    class SenderComQuedaDeRedeNaFoto:
        def __init__(self) -> None:
            self.chamadas_foto = 0
            self.chamadas_texto = 0

        async def can_post_to_channel(self, channel_id: str | int) -> bool:
            return True

        async def send_offer_photo(
            self,
            channel_id: str | int,
            photo_url: str,
            caption: str,
        ) -> str:
            self.chamadas_foto += 1
            raise NetworkError("Conexão perdida com api.telegram.org")

        async def send_offer_message(self, channel_id: str | int, text: str) -> str:
            self.chamadas_texto += 1
            return "msg-indevida"

    sender = SenderComQuedaDeRedeNaFoto()
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=tmp_path / "rede_foto.db",
    )

    oferta = Offer(
        title="Carretilha Marine Sports Lubina",
        price=299.0,
        original_price=399.0,
        url="https://loja.example/lubina",
        source="Mercado Livre",
        image_url="https://cdn.loja.example/lubina.jpg",
    )

    publicadas = asyncio.run(publisher.publish_approved_offers([oferta]))

    assert publicadas == []
    assert sender.chamadas_foto == 1
    assert sender.chamadas_texto == 0


def test_republica_apenas_apos_cooldown_15_dias_ou_queda_de_10_porcento(
    tmp_path: Path,
):
    """LOG-04: Bloqueia repetição recente, mas libera se cair >= 10% ou após 15 dias."""
    relogio = [datetime(2026, 11, 1, 14, 0, 0, tzinfo=BRASILIA_TZ)]
    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=tmp_path / "cooldown.db",
        now_func=lambda: relogio[0],
        republish_cooldown_days=15.0,
        republish_min_drop_percent=10.0,
    )

    # Dia 1: publica por R$ 200,00
    oferta_dia_1 = Offer(
        title="Carretilha Shimano Caius Pesca",
        price=200.0,
        original_price=280.0,
        url="https://www.mercadolivre.com.br/p/MLB777888?matt_tool=11",
        source="Mercado Livre",
        product_key="mlb-product:MLB777888",
    )
    assert len(asyncio.run(publisher.publish_approved_offers([oferta_dia_1]))) == 1

    # Dia 5 com queda leve (R$ 190,00 = -5%, < 10%) e outro vendedor -> BLOQUEADO
    relogio[0] = datetime(2026, 11, 6, 14, 0, 0, tzinfo=BRASILIA_TZ)
    oferta_queda_leve = Offer(
        title="Carretilha Shimano Caius Pesca",
        price=190.0,
        original_price=280.0,
        url="https://produto.mercadolivre.com.br/MLB-999999-caius?matt_tool=22",
        source="Mercado Livre",
        product_key="mlb-product:MLB777888",
    )
    assert asyncio.run(publisher.publish_approved_offers([oferta_queda_leve])) == []

    # Dia 5 com queda real >= 10% (R$ 175,00 <= R$ 180,00) -> REPUBLICA!
    oferta_queda_forte = Offer(
        title="Carretilha Shimano Caius Pesca",
        price=175.0,
        original_price=280.0,
        url="https://produto.mercadolivre.com.br/MLB-999999-caius?matt_tool=22",
        source="Mercado Livre",
        product_key="mlb-product:MLB777888",
    )
    assert len(asyncio.run(publisher.publish_approved_offers([oferta_queda_forte]))) == 1

    # Dia 22 (> 15 dias desde 06/11) mesmo por R$ 185,00 -> REPUBLICA pelo cooldown!
    relogio[0] = datetime(2026, 11, 22, 14, 0, 0, tzinfo=BRASILIA_TZ)
    oferta_apos_cooldown = Offer(
        title="Carretilha Shimano Caius Pesca",
        price=185.0,
        original_price=280.0,
        url="https://www.mercadolivre.com.br/p/MLB777888?matt_tool=22",
        source="Mercado Livre",
        product_key="mlb-product:MLB777888",
    )
    assert len(asyncio.run(publisher.publish_approved_offers([oferta_apos_cooldown]))) == 1
    assert len(sender.sent_messages) == 3


def test_limpa_fila_pendente_a_noite_e_preserva_id_de_grupo_comum(tmp_path: Path):
    """OPS-06 / LOG-06: Não guarda preços da noite anterior e preserva IDs de grupos."""
    assert normalize_channel_id("-4123456789") == "-4123456789"
    assert normalize_channel_id("-1003913046292") == "-1003913046292"

    horarios = [
        datetime(2026, 11, 1, 21, 58, 0, tzinfo=BRASILIA_TZ),  # Ciclo 1: coleta e posta 1
        datetime(2026, 11, 1, 22, 1, 0, tzinfo=BRASILIA_TZ),   # Ciclo 2: fora do horário -> limpa fila
        datetime(2026, 11, 2, 7, 1, 0, tzinfo=BRASILIA_TZ),    # Ciclo 3: manhã -> nova coleta!
    ]
    idx = [0]

    def relogio_dinamico() -> datetime:
        atual = horarios[min(idx[0], len(horarios) - 1)]
        return atual

    async def avancar_ciclo(_segundos: float) -> None:
        idx[0] += 1

    coletas = [0]

    def coletar() -> list[Offer]:
        coletas[0] += 1
        return [
            Offer(
                title=f"Vara de Pesca Safra {coletas[0]} Item {i}",
                price=100.0 - i,
                original_price=150.0,
                url=f"https://loja.example/safra-{coletas[0]}-item-{i}",
                source="Shopee",
            )
            for i in range(2)
        ]

    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=tmp_path / "noturno.db",
        sleep_func=avancar_ciclo,
        now_func=relogio_dinamico,
    )

    asyncio.run(publisher.run_periodic_loop(coletar, max_cycles=3))

    # Deve ter feito 2 coletas (uma às 21:58 e uma nova às 07:01, descartando a sobra da noite)
    assert coletas[0] == 2
    assert len(sender.sent_messages) == 2
    assert "Safra 1" in sender.sent_messages[0]["text"]
    assert "Safra 2" in sender.sent_messages[1]["text"]


def test_intercala_mercado_livre_e_shopee_mesmo_com_descontos_maiores_na_shopee(
    tmp_path: Path,
):
    """Evita que descontos altos de várias lojas da Shopee sufoquem o Mercado Livre."""
    banco_teste = tmp_path / "intercalado.db"
    relogio = [datetime(2026, 11, 7, 13, 0, 0, tzinfo=BRASILIA_TZ)]

    async def avancar_10_min(segundos: float) -> None:
        from datetime import timedelta

        relogio[0] = relogio[0] + timedelta(seconds=segundos)

    def coletar_lote_misto() -> list[Offer]:
        # Shopee com descontos enormes (70% e 65%) em lojas diferentes vs ML com 12% e 10%
        return [
            Offer(
                title="Carretilha Pesca Perfil Baixo Shopee A",
                price=60.0,
                original_price=200.0,  # 70% OFF
                url="https://s.shopee.com.br/itemA",
                source="Shopee (Loja Joyfox)",
                product_key="shopee:101",
            ),
            Offer(
                title="Vara De Pesca Carbono Shopee B",
                price=70.0,
                original_price=200.0,  # 65% OFF
                url="https://s.shopee.com.br/itemB",
                source="Shopee (Loja Sougayilang)",
                product_key="shopee:102",
            ),
            Offer(
                title="Molinete Shimano FX 4000 Pesca ML 1",
                price=176.0,
                original_price=200.0,  # 12% OFF
                url="https://www.mercadolivre.com.br/p/MLB1001",
                source="Mercado Livre",
                product_key="mlb-product:MLB1001",
            ),
            Offer(
                title="Alicate De Pesca Contenção ML 2",
                price=90.0,
                original_price=100.0,  # 10% OFF
                url="https://www.mercadolivre.com.br/p/MLB1002",
                source="Mercado Livre",
                product_key="mlb-product:MLB1002",
            ),
        ]

    sender = SimulatedChannelSender(has_permission=True)
    publisher = OfferPublisher(
        sender=sender,
        channel_id="-1003913046292",
        db_path=banco_teste,
        sleep_func=avancar_10_min,
        now_func=lambda: relogio[0],
    )

    # Roda 4 ciclos de 10 min (passando inclusive pela expiração de 30 min da fila!)
    asyncio.run(publisher.run_periodic_loop(coletar_lote_misto, max_cycles=4))

    assert len(sender.sent_messages) == 4
    textos = [m["text"] for m in sender.sent_messages]
    # Ciclo 1: Shopee A (70%) -> Ciclo 2: ML 1 (12%) -> Ciclo 3: Shopee B (65%) -> Ciclo 4 (após refresh de 30m!): ML 2 (10%)
    assert "Shopee A" in textos[0]
    assert "ML 1" in textos[1]
    assert "Shopee B" in textos[2]
    assert "ML 2" in textos[3]

    # Simula reinício do bot (nova instância lendo última fonte = Mercado Livre no SQLite):
    # se a última foi Mercado Livre, o próximo post após reiniciar deve priorizar Shopee;
    # e se a última for Shopee, após reiniciar deve priorizar Mercado Livre!
    novo_lote = [
        Offer(
            title="Linha Multifilamento Pesca Shopee C",
            price=30.0,
            original_price=100.0,  # 70% OFF
            url="https://s.shopee.com.br/itemC",
            source="Shopee (Outra Loja)",
            product_key="shopee:103",
        ),
        Offer(
            title="Caixa De Pesca Organizadora ML 3",
            price=85.0,
            original_price=100.0,  # 15% OFF
            url="https://www.mercadolivre.com.br/p/MLB1003",
            source="Mercado Livre",
            product_key="mlb-product:MLB1003",
        ),
    ]
    sender_reinicio = SimulatedChannelSender(has_permission=True)
    publisher_reinicio = OfferPublisher(
        sender=sender_reinicio,
        channel_id="-1003913046292",
        db_path=banco_teste,
        now_func=lambda: relogio[0],
    )
    # Última publicação no banco foi ML 2 -> primeiro post após reinício é Shopee C
    pub_1 = asyncio.run(publisher_reinicio.publish_approved_offers(novo_lote, max_offers=1))
    assert len(pub_1) == 1
    assert "Shopee C" in pub_1[0].title

    # Novo reinício agora que a última no banco é Shopee C -> primeiro post deve ser ML 3!
    publisher_reinicio_2 = OfferPublisher(
        sender=sender_reinicio,
        channel_id="-1003913046292",
        db_path=banco_teste,
        now_func=lambda: relogio[0],
    )
    pub_2 = asyncio.run(publisher_reinicio_2.publish_approved_offers(novo_lote, max_offers=1))
    assert len(pub_2) == 1
    assert "ML 3" in pub_2[0].title
