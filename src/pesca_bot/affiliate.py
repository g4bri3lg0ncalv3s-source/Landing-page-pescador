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

import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv
import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=True)


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
    """Extrai `matt_word` e `matt_tool` de uma URL completa ou seguindo um link `/sec/`."""
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

    # Se for um link curto https://mercadolivre.com/sec/..., segue o redirecionamento
    if "mercadolivre.com/sec/" in cleaned or "meli.la/" in cleaned:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        with httpx.Client(follow_redirects=True, timeout=15.0, headers=headers) as client:
            response = client.get(cleaned)
            final_url = str(response.url)
            final_params = dict(parse_qsl(urlparse(final_url).query))
            if "matt_word" in final_params or "matt_tool" in final_params:
                return {
                    "matt_word": final_params.get("matt_word", "").strip(),
                    "matt_tool": final_params.get("matt_tool", "").strip(),
                }

            # Caso o redirecionamento esteja no HTML da página intermediária
            html = response.text
            word_match = re.search(r"matt_word=([^&\"'\s]+)", html)
            tool_match = re.search(r"matt_tool=([^&\"'\s]+)", html)
            if word_match or tool_match:
                return {
                    "matt_word": word_match.group(1) if word_match else "",
                    "matt_tool": tool_match.group(1) if tool_match else "",
                }

    return {}

