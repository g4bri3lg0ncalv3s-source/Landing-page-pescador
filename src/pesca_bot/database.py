"""Camada de persistência, histórico de preços e registro de publicações em SQLite.

Este módulo gerencia o banco de dados local (`data/ofertas.db`) com quatro tabelas:
1. `offers`: guarda cada oferta única identificada pelo seu link (URL canônica),
   incluindo a URL da imagem principal (`image_url`) e o identificador estável
   do produto (`product_key`).
2. `price_history`: guarda o histórico de preços de cada oferta ao longo do tempo.
3. `publications`: registra cada oferta já enviada ao canal do Telegram (com o
   preço publicado), evitando publicações repetidas mesmo após reiniciar o bot
   e permitindo republicar após o período de cooldown ou queda real de preço.
4. `oauth_tokens`: guarda os tokens OAuth rotativos (ex.: Mercado Livre), que
   são renovados automaticamente sem reescrever o arquivo `.env`.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator

from pesca_bot.config import PROJECT_ROOT, env_str
from pesca_bot.offers import (
    Offer,
    canonicalize_url,
    extract_product_key,
    is_valid_image_url,
)

logger = logging.getLogger(__name__)

# Versão atual do esquema do banco (PRAGMA user_version).
SCHEMA_VERSION = 2
SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized_paths: set[str] = set()
_init_lock = threading.Lock()


def _resolve_database_path(custom_path: Path | str | None = None) -> Path:
    """Resolve o caminho do arquivo SQLite a partir da raiz do projeto."""
    if custom_path is not None:
        return Path(custom_path)

    raw_path = env_str("DATABASE_PATH", "data/ofertas.db")
    path = Path(raw_path or "data/ofertas.db")
    if not path.is_absolute():
        return PROJECT_ROOT / path
    return path


DATABASE_PATH = _resolve_database_path()


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Abre uma conexão segura (timeout + chaves estrangeiras) e sempre a fecha."""
    connection = sqlite3.connect(db_path, timeout=10.0)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_sqlite_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(SQLITE_TIMESTAMP_FORMAT)


def _parse_sqlite_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        parsed = datetime.strptime(text, SQLITE_TIMESTAMP_FORMAT)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _migrate_canonical_urls(connection: sqlite3.Connection) -> None:
    """Recalcula URLs canônicas e `product_key` de registros antigos.

    Necessário porque a canonicalização passou a ignorar `matt_word` e
    `matt_tool`. Sem esta migração, ofertas já publicadas com esses
    parâmetros seriam vistas como inéditas e republicadas em massa.
    """
    rows = connection.execute("SELECT id, url, product_key FROM offers").fetchall()
    for offer_id, url, current_key in rows:
        new_url = canonicalize_url(url) or url
        product_key = current_key or extract_product_key(url)

        if new_url != url:
            conflict = connection.execute(
                "SELECT id FROM offers WHERE url = ? AND id != ?",
                (new_url, offer_id),
            ).fetchone()
            if conflict is None:
                connection.execute(
                    "UPDATE offers SET url = ? WHERE id = ?",
                    (new_url, offer_id),
                )

        if product_key and product_key != current_key:
            connection.execute(
                "UPDATE offers SET product_key = ? WHERE id = ?",
                (product_key, offer_id),
            )


def initialize_database(db_path: Path | str | None = None) -> None:
    """Cria/migra as tabelas uma única vez por arquivo de banco no processo."""
    target_path = _resolve_database_path(db_path)
    cache_key = str(target_path.resolve())

    if cache_key in _initialized_paths and target_path.exists():
        return

    with _init_lock:
        if cache_key in _initialized_paths and target_path.exists():
            return

        target_path.parent.mkdir(parents=True, exist_ok=True)

        with _connect(target_path) as connection:
            connection.execute("PRAGMA journal_mode = WAL")

            connection.execute("""
                CREATE TABLE IF NOT EXISTS offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    price REAL NOT NULL,
                    original_price REAL NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    image_url TEXT,
                    product_key TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migração transparente: adiciona colunas criadas em versões
            # posteriores caso o banco tenha sido criado em versão anterior.
            offer_columns = _column_names(connection, "offers")
            if "image_url" not in offer_columns:
                connection.execute("ALTER TABLE offers ADD COLUMN image_url TEXT")
            if "product_key" not in offer_columns:
                connection.execute("ALTER TABLE offers ADD COLUMN product_key TEXT")

            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_offers_product_key ON offers (product_key)"
            )

            connection.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id INTEGER NOT NULL,
                    price REAL NOT NULL,
                    original_price REAL NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (offer_id) REFERENCES offers (id) ON DELETE CASCADE
                )
            """)

            connection.execute("""
                CREATE TABLE IF NOT EXISTS publications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id INTEGER NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT,
                    published_price REAL,
                    published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (offer_id, channel_id),
                    FOREIGN KEY (offer_id) REFERENCES offers (id) ON DELETE CASCADE
                )
            """)

            if "published_price" not in _column_names(connection, "publications"):
                connection.execute(
                    "ALTER TABLE publications ADD COLUMN published_price REAL"
                )
                connection.execute("""
                    UPDATE publications
                    SET published_price = (
                        SELECT o.price FROM offers o WHERE o.id = publications.offer_id
                    )
                    WHERE published_price IS NULL
                """)

            connection.execute("""
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    provider TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    env_seed_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version < SCHEMA_VERSION:
                _migrate_canonical_urls(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        _initialized_paths.add(cache_key)


def _clean_url(url: str) -> str:
    return canonicalize_url(url) or url.strip()


def save_offer(
    offer: Offer,
    db_path: Path | str | None = None,
) -> bool:
    """Salva uma oferta (incluindo sua imagem principal) e registra seu histórico."""
    target_path = _resolve_database_path(db_path)
    initialize_database(target_path)

    clean_url = _clean_url(offer.url)
    clean_image = (
        offer.image_url.strip()
        if is_valid_image_url(offer.image_url) and offer.image_url
        else None
    )
    product_key = (offer.product_key or "").strip() or extract_product_key(offer.url)

    with _connect(target_path) as connection:
        row = connection.execute(
            "SELECT id, price, original_price, image_url, product_key "
            "FROM offers WHERE url = ?",
            (clean_url,),
        ).fetchone()

        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO offers
                    (title, price, original_price, url, source, image_url, product_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    offer.title,
                    offer.price,
                    offer.original_price,
                    clean_url,
                    offer.source,
                    clean_image,
                    product_key or None,
                ),
            )
            offer_id = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO price_history (offer_id, price, original_price)
                VALUES (?, ?, ?)
                """,
                (offer_id, offer.price, offer.original_price),
            )
            return True

        offer_id, last_price, last_original_price, existing_image, existing_key = row

        history_count = connection.execute(
            "SELECT COUNT(*) FROM price_history WHERE offer_id = ?",
            (offer_id,),
        ).fetchone()[0]

        price_changed = (
            round(float(last_price), 2) != round(float(offer.price), 2)
            or round(float(last_original_price), 2)
            != round(float(offer.original_price), 2)
        )

        updated_image = clean_image or existing_image
        updated_key = product_key or existing_key

        if (
            history_count == 0
            or price_changed
            or updated_image != existing_image
            or updated_key != existing_key
        ):
            connection.execute(
                """
                UPDATE offers
                SET title = ?, price = ?, original_price = ?, source = ?,
                    image_url = ?, product_key = ?
                WHERE id = ?
                """,
                (
                    offer.title,
                    offer.price,
                    offer.original_price,
                    offer.source,
                    updated_image,
                    updated_key,
                    offer_id,
                ),
            )

        if history_count == 0 or price_changed:
            connection.execute(
                """
                INSERT INTO price_history (offer_id, price, original_price)
                VALUES (?, ?, ?)
                """,
                (offer_id, offer.price, offer.original_price),
            )

        return False


def get_price_history(
    url: str,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Retorna a lista de preços históricos registrados para uma URL de oferta."""
    target_path = _resolve_database_path(db_path)
    initialize_database(target_path)

    clean_url = _clean_url(url)

    with _connect(target_path) as connection:
        rows = connection.execute(
            """
            SELECT ph.price, ph.original_price, ph.recorded_at
            FROM price_history ph
            INNER JOIN offers o ON o.id = ph.offer_id
            WHERE o.url = ?
            ORDER BY ph.id ASC
            """,
            (clean_url,),
        ).fetchall()

    return [
        {
            "price": float(price),
            "original_price": float(original_price),
            "recorded_at": recorded_at,
        }
        for price, original_price, recorded_at in rows
    ]


def get_last_publication(
    url: str,
    channel_id: str | int,
    product_key: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Retorna a publicação mais recente do produto no canal (por URL ou `product_key`)."""
    target_path = _resolve_database_path(db_path)
    initialize_database(target_path)

    clean_url = _clean_url(url)
    clean_channel = str(channel_id).strip()
    key = (product_key or "").strip() or extract_product_key(url)

    with _connect(target_path) as connection:
        row = connection.execute(
            """
            SELECT p.published_at, p.published_price, p.message_id
            FROM publications p
            INNER JOIN offers o ON o.id = p.offer_id
            WHERE p.channel_id = ?
              AND (o.url = ? OR (? != '' AND o.product_key = ?))
            ORDER BY p.published_at DESC, p.id DESC
            LIMIT 1
            """,
            (clean_channel, clean_url, key, key),
        ).fetchone()

    if row is None:
        return None

    published_at, published_price, message_id = row
    return {
        "published_at": _parse_sqlite_timestamp(published_at),
        "published_price": float(published_price) if published_price is not None else None,
        "message_id": message_id,
    }


def get_last_published_source(
    channel_id: str | int,
    db_path: Path | str | None = None,
) -> str | None:
    """Retorna o nome da fonte (`offers.source`) da última publicação feita no canal."""
    clean_channel = str(channel_id or "").strip()
    if not clean_channel:
        return None

    target_path = _resolve_database_path(db_path)
    initialize_database(target_path)

    with _connect(target_path) as connection:
        row = connection.execute(
            """
            SELECT o.source
            FROM publications p
            INNER JOIN offers o ON o.id = p.offer_id
            WHERE p.channel_id = ?
            ORDER BY p.published_at DESC, p.id DESC
            LIMIT 1
            """,
            (clean_channel,),
        ).fetchone()

    if row is None or not row[0]:
        return None
    return str(row[0]).strip()


def is_offer_published(
    url: str,
    channel_id: str | int,
    db_path: Path | str | None = None,
    product_key: str | None = None,
) -> bool:
    """Verifica se uma oferta (URL canônica ou mesmo produto) já foi publicada no canal."""
    return (
        get_last_publication(url, channel_id, product_key=product_key, db_path=db_path)
        is not None
    )


def should_publish_offer(
    offer: Offer,
    channel_id: str | int,
    cooldown_days: float = 15.0,
    min_price_drop_percent: float = 10.0,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Decide se a oferta pode ser (re)publicada no canal.

    Regras:
    - Nunca publicada no canal -> pode publicar.
    - Publicada há `cooldown_days` dias ou mais -> pode republicar.
      (`cooldown_days <= 0` desativa a republicação por tempo.)
    - Dentro do cooldown, só republica se o preço atual tiver caído pelo menos
      `min_price_drop_percent`% em relação ao último preço publicado.
      (`min_price_drop_percent <= 0` desativa a republicação por queda.)
    """
    last = get_last_publication(
        offer.url,
        channel_id,
        product_key=offer.product_key,
        db_path=db_path,
    )
    if last is None:
        return True

    current_time = now or _utc_now()
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    published_at = last["published_at"]
    if cooldown_days > 0 and published_at is not None:
        if current_time - published_at >= timedelta(days=cooldown_days):
            return True

    last_price = last["published_price"]
    if min_price_drop_percent > 0 and last_price and last_price > 0:
        threshold = last_price * (1 - min_price_drop_percent / 100.0)
        if float(offer.price) <= round(threshold, 2):
            return True

    return False


def record_publication(
    offer: Offer,
    channel_id: str | int,
    message_id: str | int | None = None,
    db_path: Path | str | None = None,
    published_at: datetime | None = None,
) -> bool:
    """Registra (ou atualiza, em caso de republicação) a publicação da oferta no canal."""
    target_path = _resolve_database_path(db_path)
    save_offer(offer, db_path=target_path)

    clean_url = _clean_url(offer.url)
    clean_channel = str(channel_id).strip()
    clean_msg_id = str(message_id) if message_id is not None else None
    timestamp = _to_sqlite_timestamp(published_at or _utc_now())

    with _connect(target_path) as connection:
        row = connection.execute(
            "SELECT id FROM offers WHERE url = ?",
            (clean_url,),
        ).fetchone()
        if row is None:
            return False

        offer_id = row[0]
        cursor = connection.execute(
            """
            INSERT INTO publications
                (offer_id, channel_id, message_id, published_price, published_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (offer_id, channel_id) DO UPDATE SET
                message_id = excluded.message_id,
                published_price = excluded.published_price,
                published_at = excluded.published_at
            """,
            (offer_id, clean_channel, clean_msg_id, float(offer.price), timestamp),
        )
        return cursor.rowcount == 1


class OAuthTokenStore:
    """Armazena tokens OAuth rotativos no SQLite usando o `.env` como semente.

    - Na primeira execução, os tokens do `.env` são gravados no banco.
    - A cada renovação (`save`), o novo par de tokens é persistido no banco,
      sobrevivendo a reinícios do bot sem reescrever o `.env`.
    - Se você trocar manualmente os tokens no `.env`, a mudança é detectada
      (por hash SHA-256 da semente) e os novos valores passam a valer.

    Os tokens nunca são registrados em logs.
    """

    def __init__(
        self,
        provider: str,
        env_access_token: str = "",
        env_refresh_token: str = "",
        db_path: Path | str | None = None,
    ) -> None:
        self.provider = provider
        self._env_access = (env_access_token or "").strip()
        self._env_refresh = (env_refresh_token or "").strip()
        self.db_path = _resolve_database_path(db_path)

    @property
    def env_seed_hash(self) -> str:
        raw = f"{self.provider}\n{self._env_access}\n{self._env_refresh}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def load(self) -> tuple[str, str]:
        """Retorna `(access_token, refresh_token)` mais recentes conhecidos."""
        initialize_database(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT access_token, refresh_token, env_seed_hash "
                "FROM oauth_tokens WHERE provider = ?",
                (self.provider,),
            ).fetchone()

        env_has_seed = bool(self._env_access or self._env_refresh)

        if row is not None:
            stored_access, stored_refresh, stored_hash = row
            if stored_hash == self.env_seed_hash or not env_has_seed:
                return stored_access, stored_refresh
            logger.info(
                "Novos tokens de %s detectados no .env; substituindo os armazenados.",
                self.provider,
            )

        if env_has_seed:
            self.save(self._env_access, self._env_refresh)

        return self._env_access, self._env_refresh

    def save(self, access_token: str, refresh_token: str) -> None:
        """Persiste o par de tokens mais recente no SQLite."""
        initialize_database(self.db_path)
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO oauth_tokens
                    (provider, access_token, refresh_token, env_seed_hash, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (provider) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    env_seed_hash = excluded.env_seed_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    self.provider,
                    access_token or "",
                    refresh_token or "",
                    self.env_seed_hash,
                    _to_sqlite_timestamp(_utc_now()),
                ),
            )
