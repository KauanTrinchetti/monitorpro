"""
Backup automatizado com rotacao e verificacao de integridade.

Complementa o botao do painel, que gera backup sob demanda. Este comando
serve para agendamento e faz o que o botao nao fazia: apaga backups antigos
e grava o SHA-256 do arquivo, para que a restauracao possa provar que o
conteudo nao foi alterado desde a geracao.

Agendamento sugerido (diario, madrugada):
    python manage.py backup_dados --manter 14
"""

import gzip
import io
import json
import logging
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from monitor.models import BackupRegistro
from monitor.seguranca import sha256_arquivo

log = logging.getLogger("monitor")

# FaturaCpfl fica de fora: e managed=False e o dumpdata do Django nao lida
# bem com ela. As faturas sao cobertas pelo backup do proprio Postgres
# (pg_dump), que e o mecanismo correto para a tabela do robo.
MODELOS = [
    "monitor.Empresa",
    "monitor.PerfilUsuario",
    "monitor.UnidadeConsumidora",
    "monitor.AlertaOperacional",
    "monitor.RoboExecucaoLog",
    "monitor.AuditoriaEvento",
    "monitor.RegistroAlteracao",
    "monitor.BackupRegistro",
    "monitor.FaturaCpflDetalhe",
    "monitor.LoteProcessamento",
    "monitor.ArquivoProcessamento",
]


class Command(BaseCommand):
    help = "Gera backup JSON comprimido, com checksum e rotacao."

    def add_arguments(self, parser):
        parser.add_argument("--manter", type=int, default=14,
                            help="Quantos backups preservar. Padrao: 14.")
        parser.add_argument("--sem-compressao", action="store_true",
                            help="Grava .json em vez de .json.gz.")

    def handle(self, *args, **opcoes):
        pasta = Path(settings.BASE_DIR) / "backups"
        pasta.mkdir(exist_ok=True)

        carimbo = timezone.localtime().strftime("%Y%m%d_%H%M%S")
        comprimir = not opcoes["sem_compressao"]
        nome = f"backup_monitor_{carimbo}.json" + (".gz" if comprimir else "")
        caminho = pasta / nome

        self.stdout.write(f"Gerando {nome}...")

        buffer = io.StringIO()
        call_command("dumpdata", *MODELOS, indent=2, stdout=buffer)
        conteudo = buffer.getvalue().encode("utf-8")

        # Confere que o dump e JSON valido antes de considerar o backup bom.
        # Um arquivo truncado por disco cheio so seria descoberto na hora de
        # restaurar, que e o pior momento possivel.
        try:
            registros = json.loads(conteudo)
        except json.JSONDecodeError as erro:
            self.stderr.write(self.style.ERROR(f"Dump invalido, backup abortado: {erro}"))
            return

        if comprimir:
            with gzip.open(caminho, "wb", compresslevel=6) as arquivo:
                arquivo.write(conteudo)
        else:
            caminho.write_bytes(conteudo)

        checksum = sha256_arquivo(caminho)
        tamanho = caminho.stat().st_size

        BackupRegistro.objects.create(
            arquivo=str(caminho),
            tamanho_bytes=tamanho,
            status="criado",
            detalhes=json.dumps({
                "sha256": checksum,
                "registros": len(registros),
                "comprimido": comprimir,
                "modelos": len(MODELOS),
            }),
        )

        self.stdout.write(self.style.SUCCESS(
            f"OK: {len(registros)} registro(s), {tamanho / 1024:.1f} KB, sha256 {checksum[:16]}..."
        ))

        removidos = self._rotacionar(pasta, opcoes["manter"])
        if removidos:
            self.stdout.write(f"Rotacao: {removidos} backup(s) antigo(s) removido(s).")

    def _rotacionar(self, pasta: Path, manter: int) -> int:
        """Mantem apenas os N backups mais recentes."""
        arquivos = sorted(
            list(pasta.glob("backup_monitor_*.json")) + list(pasta.glob("backup_monitor_*.json.gz")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        removidos = 0
        for antigo in arquivos[manter:]:
            try:
                antigo.unlink()
                BackupRegistro.objects.filter(arquivo=str(antigo)).update(status="removido")
                removidos += 1
            except OSError as erro:
                log.warning("Nao foi possivel remover %s: %s", antigo, erro)
        return removidos
