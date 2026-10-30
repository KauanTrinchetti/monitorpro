"""
Executa a coleta IMAP fora do ciclo de requisicao HTTP.

Substitui a chamada sincrona que existia dentro da view `coletar_agora`.

Agendamento sugerido (Agendador de Tarefas do Windows, a cada 1h):
    python manage.py coletar_faturas

Para atender apenas solicitacoes feitas pelo painel:
    python manage.py coletar_faturas --somente-solicitadas
"""

import logging

from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.utils import timezone

from monitor.models import ArquivoProcessamento, ColetaControle, LoteProcessamento, RoboExecucaoLog

log = logging.getLogger("monitor")

CHAVE_GLOBAL = "global"


class Command(BaseCommand):
    help = "Le a caixa de entrada, extrai as faturas e grava no banco."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limite",
            type=int,
            default=25,
            help="Maximo de mensagens processadas nesta execucao.",
        )
        parser.add_argument(
            "--somente-solicitadas",
            action="store_true",
            help="Roda apenas se houver solicitacao pendente feita pelo painel.",
        )

    def handle(self, *args, **opcoes):
        from monitor.views import finalizar_coleta_com_trava, liberar_coleta_travada

        liberar_coleta_travada()

        lote = self._reivindicar_lote()
        if opcoes["somente_solicitadas"] and not lote:
            self.stdout.write("Nenhuma coleta solicitada. Nada a fazer.")
            return

        controle = ColetaControle.objects.filter(chave=CHAVE_GLOBAL).first()
        registro = RoboExecucaoLog.objects.filter(pk=controle.log_id).first() if controle and controle.log_id else None

        if not registro:
            registro = RoboExecucaoLog.objects.create(
                status="processando",
                mensagem="Coleta automatica em andamento",
            )

        self.stdout.write(self.style.SUCCESS(f"Coleta iniciada (lote #{lote.id if lote else '-'})."))

        # Importado aqui, nao no topo: o modulo do robo exige configuracao de
        # e-mail e nao deve derrubar `manage.py help` quando ela faltar.
        from robo_leitor_cpfl import executar_extracao

        empresas_afetadas = set()

        def ao_progredir(indice, total, resumo):
            """Publica o andamento por mensagem, para o painel exibir."""
            close_old_connections()
            empresas_afetadas.update(resumo.get("empresas") or ())
            if not lote:
                return
            ArquivoProcessamento.objects.create(
                lote=lote,
                nome_original=f"Mensagem {indice} de {total}",
                caminho="",
                status="concluido" if resumo.get("sucesso") else "erro",
                mensagem="; ".join(resumo.get("motivos", []))[:500] or "Processada.",
                resumo=resumo,
                finalizado_em=timezone.now(),
            )
            LoteProcessamento.objects.filter(pk=lote.pk).update(total_arquivos=indice)
            self.stdout.write(f"  [{indice}/{total}] {'ok' if resumo.get('sucesso') else 'falha'}")

        houve_sucesso = False
        erro_fatal = ""
        try:
            houve_sucesso = executar_extracao(
                limite_mensagens=opcoes["limite"],
                ao_progredir=ao_progredir,
            )
        except Exception as erro:
            erro_fatal = f"{type(erro).__name__}: {erro}"
            log.exception("Coleta falhou: %s", erro)
        finally:
            close_old_connections()

            if erro_fatal:
                registro.status = "erro"
                registro.mensagem = "Erro critico na coleta"
                registro.detalhes = erro_fatal[:2000]
            elif houve_sucesso:
                registro.status = "sucesso"
                registro.mensagem = "Faturas lidas com sucesso"
            else:
                registro.status = "aviso"
                registro.mensagem = "Coleta sem novas faturas"

            registro.finalizado_em = timezone.now()
            registro.save(update_fields=["status", "mensagem", "detalhes", "finalizado_em"])
            finalizar_coleta_com_trava(registro, registro.mensagem)
            self._fechar_lote(lote, erro_fatal)
            self._registrar_por_empresa(registro, empresas_afetadas)

        estilo = self.style.SUCCESS if houve_sucesso else self.style.WARNING
        self.stdout.write(estilo(f"Coleta encerrada: {registro.mensagem}"))

    @staticmethod
    @transaction.atomic
    def _reivindicar_lote():
        """Pega uma solicitacao pendente do painel, se houver."""
        lote = (
            LoteProcessamento.objects
            .select_for_update(skip_locked=True)
            .filter(origem="imap", status="pendente")
            .order_by("id")
            .first()
        )
        if lote:
            lote.status = "processando"
            lote.save(update_fields=["status"])
        return lote

    def _registrar_por_empresa(self, registro, empresas_afetadas):
        """
        Cria um log de coleta para cada empresa cujas faturas foram lidas.

        O log principal continua sem empresa: ele representa a execucao do
        robo como um todo, e so o superusuario o ve. Estes aqui sao a copia
        que cada cliente enxerga na tela de Coletas — antes, uma coleta
        automatica nao aparecia para ninguem.
        """
        if not empresas_afetadas:
            return

        for empresa_id in empresas_afetadas:
            RoboExecucaoLog.objects.create(
                empresa_id=empresa_id,
                status=registro.status,
                mensagem=registro.mensagem,
                detalhes=registro.detalhes,
                finalizado_em=registro.finalizado_em,
            )
        self.stdout.write(f"  log de coleta replicado para {len(empresas_afetadas)} empresa(s).")

    @staticmethod
    def _fechar_lote(lote, erro_fatal):
        if not lote:
            return
        lote.refresh_from_db()
        com_erro = lote.arquivos.filter(status__in=["erro", "recusado"]).exists()
        if erro_fatal:
            lote.status = "erro"
        elif com_erro:
            lote.status = "concluido_com_erros"
        else:
            lote.status = "concluido"
        lote.finalizado_em = timezone.now()
        lote.save(update_fields=["status", "finalizado_em"])
