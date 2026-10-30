"""
Worker da fila de PDFs.

Uso tipico (Agendador de Tarefas do Windows, a cada 5 minutos):
    python manage.py processar_fila --uma-passada

Uso como servico continuo:
    python manage.py processar_fila --concorrencia 3
"""

import logging
import multiprocessing
import os
import signal
import socket
import time
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from monitor import fila, worker_pdf
from monitor.models import ArquivoProcessamento

log = logging.getLogger("monitor")


def processar_arquivo_com_timeout(caminho_pdf: str, timeout: int) -> dict:
    """
    Roda o parsing num processo filho e o mata se passar do tempo.

    Processo, e nao thread, por dois motivos. O timeout so e real se der para
    matar quem travou — uma thread presa dentro do pdfplumber nao pode ser
    interrompida pelo Python. E um PDF malformado que estoure a memoria
    derruba apenas o filho; o worker principal segue com os demais arquivos.

    A funcao alvo vive em monitor.worker_pdf justamente porque, no modo spawn,
    o filho re-importa o modulo que a define: se fosse este arquivo, o import
    de models aconteceria antes do django.setup() do filho.
    """
    # spawn e o unico modo disponivel no Windows e evita herdar conexoes de
    # banco abertas; o filho abre as suas.
    contexto = multiprocessing.get_context("spawn")
    canal = contexto.Queue()
    processo = contexto.Process(target=worker_pdf.executar, args=(caminho_pdf, canal))

    processo.start()
    processo.join(timeout)

    if processo.is_alive():
        processo.terminate()
        processo.join(5)
        if processo.is_alive():
            processo.kill()
        return {"ok": False, "erro": f"Timeout: excedeu {timeout}s de processamento."}

    if canal.empty():
        return {"ok": False, "erro": f"Processo encerrou sem resultado (codigo {processo.exitcode})."}

    return canal.get()


class Command(BaseCommand):
    help = "Processa a fila de PDFs com concorrencia limitada, timeout e retry."

    def add_arguments(self, parser):
        parser.add_argument(
            "--concorrencia",
            type=int,
            default=max(1, (os.cpu_count() or 2) // 2),
            help="Quantos PDFs processar em paralelo. Padrao: metade dos nucleos.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=getattr(settings, "PDF_TIMEOUT_SEGUNDOS", 120),
            help="Tempo maximo por arquivo, em segundos.",
        )
        parser.add_argument(
            "--uma-passada",
            action="store_true",
            help="Esvazia a fila atual e encerra, em vez de rodar continuamente.",
        )
        parser.add_argument(
            "--intervalo",
            type=int,
            default=10,
            help="Segundos de espera quando a fila esta vazia (modo continuo).",
        )

    def handle(self, *args, **opcoes):
        concorrencia = max(1, opcoes["concorrencia"])
        timeout = opcoes["timeout"]
        uma_passada = opcoes["uma_passada"]
        intervalo = opcoes["intervalo"]

        identificacao = f"{socket.gethostname()}:{os.getpid()}"
        self._encerrando = False

        # Encerramento gracioso: para de pegar arquivo novo, mas deixa os que
        # ja estao em andamento terminarem. Sem isso, um Ctrl+C no meio do
        # lote deixaria arquivos travados em 'processando'.
        def _parar(signum, _frame):
            self.stdout.write(self.style.WARNING("\nSinal recebido; encerrando apos os arquivos em andamento..."))
            self._encerrando = True

        signal.signal(signal.SIGINT, _parar)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _parar)

        # Recupera o que ficou preso por um worker anterior que morreu.
        recuperados = fila.destravar_coletas_orfas()
        if recuperados:
            self.stdout.write(self.style.WARNING(f"{recuperados} arquivo(s) reenfileirado(s) de execucao anterior."))

        self.stdout.write(
            self.style.SUCCESS(
                f"Worker {identificacao} | concorrencia {concorrencia} | timeout {timeout}s"
            )
        )

        processados = 0
        with ThreadPoolExecutor(max_workers=concorrencia) as executor:
            em_andamento = set()

            while not self._encerrando:
                # Mantem o pool cheio sem estourar o limite de concorrencia.
                while len(em_andamento) < concorrencia and not self._encerrando:
                    close_old_connections()
                    arquivo = fila.reivindicar_proximo(identificacao)
                    if not arquivo:
                        break
                    em_andamento.add(executor.submit(self._trabalhar, arquivo.id, timeout))

                if not em_andamento:
                    if uma_passada:
                        break
                    time.sleep(intervalo)
                    continue

                concluidas = {f for f in em_andamento if f.done()}
                for futuro in concluidas:
                    try:
                        futuro.result()
                        processados += 1
                    except Exception as erro:
                        log.exception("Falha inesperada no worker: %s", erro)
                em_andamento -= concluidas

                if not concluidas:
                    time.sleep(0.5)

            # Aguarda o que ficou em andamento antes de sair.
            for futuro in em_andamento:
                try:
                    futuro.result()
                    processados += 1
                except Exception as erro:
                    log.exception("Falha ao finalizar: %s", erro)

        self.stdout.write(self.style.SUCCESS(f"Encerrado. {processados} arquivo(s) processado(s)."))

    def _trabalhar(self, arquivo_id: int, timeout: int) -> None:
        """Processa um arquivo. Toda excecao vira falha registrada, nunca crash do worker."""
        close_old_connections()
        arquivo = ArquivoProcessamento.objects.filter(pk=arquivo_id).first()
        if not arquivo:
            return

        caminho = Path(arquivo.caminho)
        if not caminho.is_file():
            fila.falhar(arquivo, "Arquivo nao encontrado no disco.")
            return

        self.stdout.write(f"  -> {arquivo.nome_original} (tentativa {arquivo.tentativas})")

        resultado = processar_arquivo_com_timeout(str(caminho), timeout)

        close_old_connections()
        arquivo.refresh_from_db()

        if resultado.get("ok"):
            resumo = resultado.get("resumo") or {}
            fila.concluir(arquivo, resumo)
            # PDF processado com sucesso nao fica em disco: o dado ja esta no
            # banco e manter fatura de cliente no sistema de arquivos amplia a
            # exposicao sem beneficio.
            self._remover(caminho)
            self.stdout.write(self.style.SUCCESS(f"     ok: {arquivo.mensagem}"))
        else:
            fila.falhar(arquivo, resultado.get("erro", "erro desconhecido"))
            self.stdout.write(self.style.ERROR(f"     falha: {arquivo.mensagem}"))

    @staticmethod
    def _remover(caminho: Path) -> None:
        try:
            caminho.unlink(missing_ok=True)
        except OSError as erro:
            log.warning("Nao foi possivel remover %s: %s", caminho, erro)
