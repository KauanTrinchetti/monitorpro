"""
Fila de processamento de PDFs sobre o proprio PostgreSQL.

Por que nao Celery/Redis: o volume e de dezenas de faturas por rodada, nao
milhares por segundo. Um broker adicional significa mais um servico para
manter no ar, monitorar e reiniciar — e no Windows o Celery ainda exige
`--pool=solo`. O Postgres ja esta aqui, ja e transacional, e
`SELECT ... FOR UPDATE SKIP LOCKED` da entrega exclusiva por worker com a
mesma garantia que um broker daria neste porte.

Se um dia o volume crescer a ponto de justificar, a troca fica contida:
so `reivindicar_proximo` e o management command precisam mudar.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import ArquivoProcessamento, LoteProcessamento

log = logging.getLogger("monitor")

# Backoff exponencial entre tentativas: 1min, 4min, 9min...
# Erro transitorio (banco reiniciando, disco ocupado) tende a passar; erro
# permanente (PDF corrompido) esgota as tentativas e para de consumir worker.
BACKOFF_BASE_SEGUNDOS = 60


def criar_lote(origem, usuario=None, empresa=None, total=0) -> LoteProcessamento:
    return LoteProcessamento.objects.create(
        origem=origem,
        usuario=usuario if (usuario and usuario.is_authenticated) else None,
        empresa=empresa,
        total_arquivos=total,
        status="pendente",
    )


def enfileirar(lote: LoteProcessamento, caminho: Path, nome_original: str, meta: dict) -> ArquivoProcessamento:
    """Coloca um PDF ja validado na fila."""
    return ArquivoProcessamento.objects.create(
        lote=lote,
        nome_original=nome_original[:255],
        caminho=str(caminho),
        sha256=meta.get("sha256", ""),
        tamanho_bytes=meta.get("tamanho_bytes", 0),
        paginas=meta.get("paginas", 0),
        status="pendente",
    )


def registrar_recusa(lote: LoteProcessamento, nome_original: str, motivo: str) -> ArquivoProcessamento:
    """
    Registra um arquivo que nem chegou a entrar na fila.

    Um PDF recusado na validacao aparece no lote com o motivo, em vez de
    sumir silenciosamente — quem enviou precisa saber por que aquele arquivo
    especifico nao foi processado.
    """
    return ArquivoProcessamento.objects.create(
        lote=lote,
        nome_original=nome_original[:255],
        caminho="",
        status="recusado",
        mensagem=motivo[:500],
        finalizado_em=timezone.now(),
    )


@transaction.atomic
def reivindicar_proximo(identificacao_worker: str) -> ArquivoProcessamento | None:
    """
    Pega o proximo arquivo da fila, garantindo exclusividade.

    SKIP LOCKED e o que permite rodar varios workers em paralelo sem que dois
    peguem o mesmo arquivo: quem chega depois pula as linhas ja travadas em
    vez de ficar esperando.
    """
    agora = timezone.now()

    # Sem agendamento (primeira tentativa) ou com a janela de backoff vencida.
    pronto = Q(proxima_tentativa_em__isnull=True) | Q(proxima_tentativa_em__lte=agora)

    arquivo = (
        ArquivoProcessamento.objects
        .select_for_update(skip_locked=True)
        .filter(pronto, status="pendente")
        .order_by("id")
        .first()
    )

    if not arquivo:
        return None

    arquivo.status = "processando"
    arquivo.iniciado_em = agora
    arquivo.tentativas += 1
    arquivo.mensagem = f"Processando em {identificacao_worker}"
    arquivo.save(update_fields=["status", "iniciado_em", "tentativas", "mensagem"])

    LoteProcessamento.objects.filter(pk=arquivo.lote_id, status="pendente").update(status="processando")
    return arquivo


def concluir(arquivo: ArquivoProcessamento, resumo: dict) -> None:
    arquivo.status = "concluido"
    arquivo.resumo = resumo or {}
    arquivo.finalizado_em = timezone.now()
    arquivo.mensagem = _mensagem_resumo(resumo)[:500]
    arquivo.save(update_fields=["status", "resumo", "finalizado_em", "mensagem"])
    _atualizar_lote(arquivo.lote_id)


def falhar(arquivo: ArquivoProcessamento, erro: str) -> None:
    """
    Marca falha e decide entre reagendar ou desistir.

    Enquanto houver tentativa disponivel o arquivo volta para 'pendente' com
    um horario futuro; esgotadas as tentativas, vira 'erro' definitivo. Em
    nenhum dos casos o lote inteiro para.
    """
    erro = (erro or "erro desconhecido")[:500]

    if arquivo.tentativas < arquivo.max_tentativas:
        espera = BACKOFF_BASE_SEGUNDOS * (arquivo.tentativas ** 2)
        arquivo.status = "pendente"
        arquivo.proxima_tentativa_em = timezone.now() + timedelta(seconds=espera)
        arquivo.mensagem = f"Tentativa {arquivo.tentativas} falhou ({erro}). Nova tentativa em {espera}s."
        arquivo.save(update_fields=["status", "proxima_tentativa_em", "mensagem"])
        log.warning("Arquivo %s reagendado em %ss: %s", arquivo.id, espera, erro)
    else:
        arquivo.status = "erro"
        arquivo.finalizado_em = timezone.now()
        arquivo.mensagem = f"Falhou apos {arquivo.tentativas} tentativa(s): {erro}"
        arquivo.save(update_fields=["status", "finalizado_em", "mensagem"])
        log.error("Arquivo %s descartado apos %s tentativas: %s", arquivo.id, arquivo.tentativas, erro)

    _atualizar_lote(arquivo.lote_id)


def _mensagem_resumo(resumo: dict) -> str:
    if not resumo:
        return "Processado."
    partes = [
        f"{resumo.get('gravadas', 0)} gravada(s)",
        f"{resumo.get('novas', 0)} nova(s)",
    ]
    if resumo.get("ignoradas"):
        partes.append(f"{resumo['ignoradas']} ignorada(s)")
    return ", ".join(partes)


def _atualizar_lote(lote_id: int) -> None:
    """Fecha o lote quando nenhum arquivo dele continua na fila."""
    lote = LoteProcessamento.objects.filter(pk=lote_id).first()
    if not lote:
        return

    pendentes = lote.arquivos.filter(status__in=["pendente", "processando"]).count()
    if pendentes:
        return

    com_erro = lote.arquivos.filter(status__in=["erro", "recusado"]).count()
    lote.status = "concluido_com_erros" if com_erro else "concluido"
    lote.finalizado_em = timezone.now()
    lote.save(update_fields=["status", "finalizado_em"])


def destravar_coletas_orfas(minutos: int = 30) -> int:
    """
    Libera arquivos presos em 'processando' por um worker que morreu.

    Sem isso, um worker morto por SIGKILL (deploy, falta de memoria, reboot)
    deixa o arquivo travado para sempre e ninguem mais o processa.
    """
    limite = timezone.now() - timedelta(minutes=minutos)
    presos = ArquivoProcessamento.objects.filter(status="processando", iniciado_em__lt=limite)
    total = presos.count()
    if total:
        presos.update(
            status="pendente",
            proxima_tentativa_em=timezone.now(),
            mensagem="Reenfileirado: worker anterior nao concluiu.",
        )
        log.warning("%s arquivo(s) reenfileirado(s) apos worker orfao.", total)
    return total
