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
│       ├── telegram_bot.py # Comandos e integração com Telegram
│       ├── publisher.py    # Publicação com foto (send_photo) e fallback para texto
│       ├── copywriting.py  # Textos persuasivos e formatação das ofertas
│       ├── offers.py       # Modelo padronizado (Offer) e fachada do motor de ofertas
│       ├── validator.py    # Validação de preços, URLs, imagens e deduplicação
│       ├── database.py     # Persistência, histórico de preços e registro de publicações (SQLite)
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

## Personalização das Mensagens (`.env`)

Você pode personalizar o estilo das postagens adicionando estas variáveis opcionais no seu arquivo `.env`:

- `COPY_MAX_EMOJIS=12` — Quantidade máxima de emojis por postagem.
- `COPY_TONE=comercial` — Tom da comunicação (`comercial`, `direto` ou `neutro`).
- `COPY_MAX_CAPTION_LENGTH=1024` — Tamanho máximo da legenda (respeita o limite de 1024 caracteres do `send_photo` no Telegram).
- `COPY_MIN_DISCOUNT_PERCENT=5.0` — Desconto mínimo (%) para usar títulos promocionais fortes.
- `PUBLISH_INTERVAL_SECONDS=180` — Intervalo em segundos entre cada postagem automática (padrão: 180s = 3 minutos).

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
