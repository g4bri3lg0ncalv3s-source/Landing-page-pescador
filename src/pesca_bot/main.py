"""Ponto de entrada principal da aplicação Pesca Bot.

Você pode iniciar o bot de duas formas:
1. Como módulo (recomendado): `python -m pesca_bot.main`
2. Executando este arquivo diretamente no VS Code: `python src/pesca_bot/main.py`
"""

import sys
from pathlib import Path

# Garante que a pasta `src/` esteja no caminho de busca do Python (sys.path)
# mesmo quando este arquivo for executado diretamente pelo botão Play do VS Code.
SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pesca_bot.telegram_bot import main  # noqa: E402

if __name__ == "__main__":
    main()
