# Pesca Bot 🎣

Bot inteligente para Telegram focado em monitorar, filtrar e divulgar promoções de artigos de pesca com imagens reais dos produtos e copywriting persuasivo responsável.

## Estrutura do Projeto

```text
botPython/
├── .venv/                  # Ambiente virtual Python (ignorado pelo Git)
├── src/
│   └── pesca_bot/          # Pacote principal da aplicação
│       ├── __init__.py
│       ├── main.py         # Ponto de entrada da aplicação
│       ├── config.py       # Carregamento seguro e hot-reload controlado do .env
│       ├── affiliate.py    # Links de afiliado Mercado Livre com proteção SSRF
│       ├── telegram_bot.py # Comandos exclusivos ADM (DM + Stealth) e integração Telegram
│       ├── publisher.py    # Publicação concorrente segura, foto/fallback e janela 07h-22h BRT
│       ├── copywriting.py  # Formatação limpa das ofertas preservando URL de afiliado
│       ├── offers.py       # Modelo padronizado (Offer) e fachada do motor de ofertas
│       ├── validator.py    # Validação de preços, nicho de pesca, URLs e deduplicação
│       ├── database.py     # Persistência SQLite (WAL), histórico, cooldown e tokens OAuth
│       └── sources/        # Pacote de coleta e padronização de fontes
│           ├── __init__.py
│           ├── base.py         # Contrato base, seleção de imagem principal e RateLimiter
│           ├── demo.py         # Ofertas fictícias de demonstração para testes locais
│           ├── shopee.py       # Cliente oficial Shopee Affiliate Open API (GraphQL)
│           └── mercadolivre.py # Cliente oficial Mercado Livre Developers API (MLB)
├── tests/                  # Testes automatizados (pytest)
│   ├── __init__.py
│   ├── test_offers.py      # Testes do motor de ofertas, imagens e banco SQLite
│   ├── test_copywriting.py # Testes de copywriting persuasivo e gatilhos responsáveis
│   ├── test_sources.py     # Testes com respostas simuladas (Shopee e Mercado Livre)
│   └── test_publisher.py   # Testes de publicação com foto, fallback e erros
├── data/                   # Armazenamento local do banco SQLite (ignorado pelo Git)
├── .env                    # Credenciais locais (ignorado pelo Git)
├── .env.example            # Modelo de configuração
├── .gitignore              # Arquivos excluídos do controle de versão
├── pyproject.toml          # Configuração do pacote em src/ e do pytest
├── requirements.txt        # Dependências do projeto (UTF-8)
└── README.md               # Documentação
```

## Configuração e Segurança (`.env`)

Configure as variáveis principais no seu arquivo `.env` (veja `.env.example`):

- `TELEGRAM_ADMIN_IDS=123456789` — IDs numéricos dos administradores autorizados (separados por vírgula). Todos os comandos funcionam **exclusivamente em chat privado (DM)** para esses IDs (*Modo Stealth + Fail-Closed*). Caso ainda não saiba seu `user_id`, inicie o bot, envie `/start` no privado e copie o `user_id` exibido no aviso do console.
- `AUTO_PUBLISH_ENABLED=false` — Mantém o envio automático pausado ao iniciar até você ativar (`/retomar` ou `true`).
- `PUBLISH_INTERVAL_SECONDS=600` — Intervalo em segundos entre cada postagem automática (padrão: 600s = 10 minutos).
- `COPY_MAX_CAPTION_LENGTH=1024` — Tamanho máximo da legenda (respeita o limite de 1024 caracteres do `send_photo` sem cortar o link de compra).
- `COPY_MIN_DISCOUNT_PERCENT=5.0` — Desconto real mínimo (%) exigido no Mercado Livre e Shopee.
- `REPUBLISH_COOLDOWN_DAYS=15` — Dias mínimos antes de permitir republicar o mesmo produto no canal.
- `REPUBLISH_MIN_PRICE_DROP_PERCENT=10.0` — Queda adicional mínima (%) sobre o último preço postado para antecipar a republicação.

## Como executar no Windows (PowerShell)

1. **Execute a suíte de testes automatizados**:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -v
   ```

2. **Inicie o bot no Telegram**:
   ```powershell
   .\.venv\Scripts\python.exe src\pesca_bot\main.py
   ```
   - Use `/ofertas` no chat do bot para buscar ofertas reais com imagem e legenda persuasiva.
   - Use `/publicar` para postar imediatamente ofertas inéditas no seu canal (`TELEGRAM_CHANNEL_ID`).
   - Use `/status`, `/pausar` e `/retomar` para controlar as publicações automáticas.
