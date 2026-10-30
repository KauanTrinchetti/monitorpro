"""
Alvo do processo filho que faz o parsing de um PDF.

IMPORTANTE: este modulo nao pode importar nada do Django no nivel de modulo.

Com o metodo de inicializacao `spawn` (unico disponivel no Windows), o
processo filho re-importa o modulo onde a funcao alvo esta definida antes de
executa-la. Se esse modulo importar models na importacao, o Django ainda nao
terminou de carregar as apps no filho e o import falha com
AppRegistryNotReady — foi exatamente o que aconteceu quando esta funcao morava
dentro do management command.

Por isso todo import pesado acontece dentro da funcao, depois de django.setup().
"""

import os
from pathlib import Path


def executar(caminho_pdf: str, canal) -> None:
    """
    Processa um PDF e devolve o resumo pelo `canal` (multiprocessing.Queue).

    Roda isolado do worker principal: se o parsing travar, o pai mata este
    processo sem perder os demais arquivos do lote; se estourar a memoria,
    quem morre e so este processo.
    """
    try:
        import django

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
        django.setup()

        from robo_leitor_cpfl import processar_pdf

        resumo = processar_pdf(Path(caminho_pdf))
        canal.put({"ok": True, "resumo": resumo})
    except Exception as erro:
        canal.put({"ok": False, "erro": f"{type(erro).__name__}: {erro}"})
