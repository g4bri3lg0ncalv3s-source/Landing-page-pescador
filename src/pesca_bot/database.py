"""Camada de persistência, histórico de preços e registro de publicações em SQLite.

Este módulo gerencia o banco de dados local (`data/ofertas.db`) com três tabelas:
1. `offers`: guarda cada oferta única identificada pelo seu link (URL canônica),
   incluindo a URL da imagem principal (`image_url`).
2. `price_history`: guarda o histórico de preços de cada oferta ao longo do tempo.
3. `publications`: registra cada oferta já enviada ao canal do Telegram, evitando
   publicações repetidas mesmo após reiniciar o computador ou o bot.
"""

import os
from pathlib import Path
import sqlite3
from typing import Any

from dotenv import load_dotenv

from pesca_bot.offers import Offer, canonicalize_url, is_valid_image_url

# Descobre automaticamente a pasta raiz do projeto (d:\botPython)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _resolve_database_path(custom_path: Path | str | None = None) -> Path:
    """Resolve o caminho do arquivo SQLite a partir da raiz do projeto."""
    if custom_path is not None:
        return Path(custom_path)

    raw_path = os.getenv("DATABASE_PATH", "data/ofertas.db").strip()
    path = Path(raw_path or "data/ofertas.db")
    if not path.is_absolute():
        return PROJECT_ROOT / path
    return path


DATABASE_PATH = _resolve_database_path()


def initialize_database(db_path: Path | str | None = None) -> None:
    """Cria a pasta de dados e as tabelas `offers`, `price_history` e `publications`."""
    target_path = _resolve_database_path(db_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(target_path) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                price REAL NOT NULL,
                original_price REAL NOT NULL,
                url TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                image_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migração transparente: adiciona a coluna `image_url` caso o banco
        # tenha sido criado em uma versão anterior sem essa coluna.
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(offers)").fetchall()
        }
        if "image_url" not in columns:
            connection.execute("ALTER TABLE offers ADD COLUMN image_url TEXT")

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
                published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (offer_id, channel_id),
                FOREIGN KEY (offer_id) REFERENCES offers (id) ON DELETE CASCADE
            )
        """)


def save_offer(
    offer: Offer,
    db_path: Path | str | None = None,
) -> bool:
    """Salva uma oferta (incluindo sua imagem principal) e registra seu histórico."""
    target_path = _resolve_database_path(db_path)
    initialize_database(target_path)

    clean_url = canonicalize_url(offer.url) or offer.url.strip()
    clean_image = (
        offer.image_url.strip()
        if is_valid_image_url(offer.image_url) and offer.image_url
        else None
    )

    with sqlite3.connect(target_path) as connection:
        row = connection.execute(
            "SELECT id, price, original_price, image_url FROM offers WHERE url = ?",
            (clean_url,),
        ).fetchone()

        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO offers (title, price, original_price, url, source, image_url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    offer.title,
                    offer.price,
                    offer.original_price,
                    clean_url,
                    offer.source,
                    clean_image,
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

        offer_id, last_price, last_original_price, existing_image = row

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

        if history_count == 0 or price_changed or updated_image != existing_image:
            connection.execute(
                """
                UPDATE offers
                SET title = ?, price = ?, original_price = ?, source = ?, image_url = ?
                WHERE id = ?
                """,
                (
                    offer.title,
                    offer.price,
                    offer.original_price,
                    offer.source,
                    updated_image,
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

    clean_url = canonicalize_url(url) or url.strip()

    with sqlite3.connect(target_path) as connection:
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


def is_offer_published(
    url: str,
    channel_id: str | int,
    db_path: Path | str | None = None,
) -> bool:
    """Verifica se uma oferta (pela URL canônica) já foi publicada no canal informado."""
    target_path = _resolve_database_path(db_path)
    initialize_database(target_path)

    clean_url = canonicalize_url(url) or url.strip()
    clean_channel = str(channel_id).strip()

    with sqlite3.connect(target_path) as connection:
        row = connection.execute(
            """
            SELECT p.id
            FROM publications p
            INNER JOIN offers o ON o.id = p.offer_id
            WHERE o.url = ? AND p.channel_id = ?
            """,
            (clean_url, clean_channel),
        ).fetchone()

    return row is not None


def record_publication(
    offer: Offer,
    channel_id: str | int,
    message_id: str | int | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Registra no SQLite que a oferta foi publicada com sucesso no canal."""
    target_path = _resolve_database_path(db_path)
    save_offer(offer, db_path=target_path)

    clean_url = canonicalize_url(offer.url) or offer.url.strip()
    clean_channel = str(channel_id).strip()
    clean_msg_id = str(message_id) if message_id is not None else None

    with sqlite3.connect(target_path) as connection:
        row = connection.execute(
            "SELECT id FROM offers WHERE url = ?",
            (clean_url,),
        ).fetchone()
        if row is None:
            return False

        offer_id = row[0]
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO publications (offer_id, channel_id, message_id)
            VALUES (?, ?, ?)
            """,
            (offer_id, clean_channel, clean_msg_id),
        )
        return cursor.rowcount == 1
