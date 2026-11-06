"""Geração e extração de links de afiliado para o Mercado Livre.

Como funciona a atribuição de comissão no Mercado Livre:
- Quando um afiliado gera um link curto (`https://mercadolivre.com/sec/...`),
  o Mercado Livre redireciona o comprador para a página do produto adicionando
  os parâmetros de identificação da conta do afiliado (`matt_word` e `matt_tool`).
- Este módulo:
  1. Anexa automaticamente `matt_word` e `matt_tool` (configurados no `.env`)
     a todos os links de produtos encontrados pela API do Mercado Livre;
  2. Permite extrair automaticamente o seu `matt_word` e `matt_tool` a partir
     de qualquer link curto `https://mercadolivre.com/sec/...` da sua conta.
"""

import logging
import os
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

from pesca_bot.config import load_env

logger = logging.getLogger(__name__)

load_env()

# Domínios oficiais aceitos para seguir links curtos de afiliado (proteção SSRF).
ALLOWED_AFFILIATE_HOSTS = frozenset(
    {
        "mercadolivre.com",
        "www.mercadolivre.com",
        "mercadolivre.com.br",
        "www.mercadolivre.com.br",
        "produto.mercadolivre.com.br",
        "lista.mercadolivre.com.br",
        "meli.la",
    }
)
MAX_AFFILIATE_REDIRECTS = 5
_AFFILIATE_PARAM_VALUE_RE = r"([A-Za-z0-9_.\-]+)"


def is_allowed_affiliate_url(url: str) -> bool:
    """Valida se a URL usa HTTPS, porta padrão e um domínio oficial permitido."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
        port = parsed.port
    except ValueError:
        return False

    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme.lower() == "https"
        and port in (None, 443)
        and not parsed.username
        and not parsed.password
        and host in ALLOWED_AFFILIATE_HOSTS
    )


def _is_short_affiliate_link(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "meli.la":
        return True
    return host in {"mercadolivre.com", "www.mercadolivre.com"} and parsed.path.startswith(
        "/sec/"
    )


def build_mercadolivre_affiliate_url(
    permalink: str,
    item_id: str = "",
    matt_word: str | None = None,
    matt_tool: str | None = None,
) -> str:
    """Adiciona os parâmetros de afiliado (`matt_word` e `matt_tool`) na URL do produto.

    Se `matt_word` e `matt_tool` não forem passados por parâmetro, lê
    automaticamente `MERCADOLIVRE_AFFILIATE_WORD` e `MERCADOLIVRE_AFFILIATE_TOOL`
    do arquivo `.env`. Caso ambos estejam vazios, retorna o `permalink` original.
    """
    clean_url = permalink.strip()
    if not clean_url:
        return ""

    word = (
        matt_word
        if matt_word is not None
        else os.getenv("MERCADOLIVRE_AFFILIATE_WORD", "")
    ).strip()
    tool = (
        matt_tool
        if matt_tool is not None
        else os.getenv("MERCADOLIVRE_AFFILIATE_TOOL", "")
    ).strip()

    if not word and not tool:
        return clean_url

    parsed = urlparse(clean_url)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=False))

    if word:
        query_params["matt_word"] = word
    if tool:
        query_params["matt_tool"] = tool

    new_query = urlencode(query_params)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )


def extract_affiliate_params_from_url(url_or_text: str) -> dict[str, str]:
    """Extrai `matt_word` e `matt_tool` de uma URL completa ou seguindo um link `/sec/`.

    Segurança: somente links HTTPS de domínios oficiais do Mercado Livre são
    acessados, e cada redirecionamento é validado novamente contra a mesma
    lista (máximo de `MAX_AFFILIATE_REDIRECTS` saltos). Erros de rede retornam
    um dicionário vazio em vez de propagar exceções.
    """
    cleaned = url_or_text.strip()
    if not cleaned:
        return {}

    parsed = urlparse(cleaned)
    params = dict(parse_qsl(parsed.query))
    if "matt_word" in params or "matt_tool" in params:
        return {
            "matt_word": params.get("matt_word", "").strip(),
            "matt_tool": params.get("matt_tool", "").strip(),
        }

    if not is_allowed_affiliate_url(cleaned) or not _is_short_affiliate_link(cleaned):
        return {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    current_url = cleaned
    try:
        with httpx.Client(follow_redirects=False, timeout=15.0, headers=headers) as client:
            response = client.get(current_url)
            redirects = 0
            while response.is_redirect and redirects < MAX_AFFILIATE_REDIRECTS:
                location = response.headers.get("location", "")
                next_url = urljoin(current_url, location)
                if not is_allowed_affiliate_url(next_url):
                    logger.warning(
                        "Redirecionamento de link de afiliado bloqueado (domínio não permitido)."
                    )
                    return {}
                current_url = next_url
                redirects += 1
                response = client.get(current_url)

            if response.is_redirect:
                logger.warning("Link de afiliado excedeu o limite de redirecionamentos.")
                return {}

            final_params = dict(parse_qsl(urlparse(current_url).query))
            if "matt_word" in final_params or "matt_tool" in final_params:
                return {
                    "matt_word": final_params.get("matt_word", "").strip(),
                    "matt_tool": final_params.get("matt_tool", "").strip(),
                }

            # Caso o redirecionamento esteja no HTML da página intermediária
            html = response.text
    except httpx.HTTPError as exc:
        logger.warning(
            "Falha ao seguir link curto de afiliado do Mercado Livre: %s",
            type(exc).__name__,
        )
        return {}

    word_match = re.search(rf"matt_word={_AFFILIATE_PARAM_VALUE_RE}", html)
    tool_match = re.search(rf"matt_tool={_AFFILIATE_PARAM_VALUE_RE}", html)
    if word_match or tool_match:
        return {
            "matt_word": word_match.group(1) if word_match else "",
            "matt_tool": tool_match.group(1) if tool_match else "",
        }

    return {}

