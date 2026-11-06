"""Carregamento centralizado e seguro das configurações do arquivo `.env`.

Regras de precedência:
1. Variáveis definidas no ambiente real do sistema operacional (ex.: Docker,
   systemd, painel da hospedagem) SEMPRE têm prioridade e nunca são
   sobrescritas pelo arquivo `.env`.
2. O arquivo `.env` preenche apenas as variáveis ausentes no ambiente.
3. `reload_env()` permite reler alterações feitas no arquivo `.env` em disco
   durante a execução sem sobrescrever variáveis reais do sistema nem valores
   injetados em memória durante testes (`monkeypatch.setenv`).
"""

import logging
import os
from pathlib import Path
import threading

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# Fotografia das variáveis que já existiam no ambiente real ANTES de qualquer
# leitura do `.env`. Essas chaves nunca serão sobrescritas por `reload_env()`.
_OS_ENV_KEYS = frozenset(os.environ)
_last_file_values: dict[str, str] = {}
_reload_lock = threading.Lock()


def load_env() -> None:
    """Carrega o `.env` sem sobrescrever variáveis já definidas no ambiente."""
    if not ENV_FILE.exists():
        return

    with _reload_lock:
        for key, value in dotenv_values(ENV_FILE).items():
            if value is None:
                continue
            _last_file_values[key] = value
            if key not in os.environ:
                os.environ[key] = value


def reload_env() -> None:
    """Relê o `.env`, aplicando apenas valores novos ou alterados no arquivo em disco."""
    if not ENV_FILE.exists():
        return

    with _reload_lock:
        for key, value in dotenv_values(ENV_FILE).items():
            if value is None or key in _OS_ENV_KEYS:
                continue
            previous_file_value = _last_file_values.get(key)
            file_changed_on_disk = previous_file_value != value
            env_matches_previous_file = os.environ.get(key) == previous_file_value

            _last_file_values[key] = value
            if key not in os.environ or file_changed_on_disk or env_matches_previous_file:
                os.environ[key] = value


def env_str(name: str, default: str = "") -> str:
    """Lê uma variável de texto, removendo espaços nas extremidades."""
    return (os.getenv(name) or default).strip()


def env_float(
    name: str,
    default: float,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    """Lê uma variável numérica decimal com fallback seguro para o padrão."""
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        logger.warning("Valor inválido em %s; usando padrão %s.", name, default)
        return default
    if value != value:  # NaN
        return default
    if min_value is not None and value < min_value:
        return default
    if max_value is not None and value > max_value:
        return max_value
    return value


def env_int(
    name: str,
    default: int,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Lê uma variável numérica inteira com fallback seguro para o padrão."""
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Valor inválido em %s; usando padrão %s.", name, default)
        return default
    if min_value is not None and value < min_value:
        return default
    if max_value is not None and value > max_value:
        return max_value
    return value


def env_bool(name: str, default: bool) -> bool:
    """Lê uma variável booleana (`true/false`, `1/0`, `sim/não`, `yes/no`)."""
    raw = env_str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "sim", "yes", "on"}:
        return True
    if raw in {"0", "false", "nao", "não", "no", "off"}:
        return False
    logger.warning("Valor booleano inválido em %s; usando padrão %s.", name, default)
    return default


load_env()
