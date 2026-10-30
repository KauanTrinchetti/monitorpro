"""
Restauracao de backup, com ensaio a seco.

Um backup que nunca foi restaurado nao e um backup — e um arquivo. Este
comando existe para que a restauracao seja exercitada de verdade, e nao
descoberta como quebrada no dia em que for necessaria.

Ensaio (nao grava nada, so confere o arquivo):
    python manage.py restaurar_backup backups/backup_monitor_XXXX.json.gz --ensaio

Restauracao real:
    python manage.py restaurar_backup backups/backup_monitor_XXXX.json.gz --confirmar
"""

import gzip
import json
import tempfile
from collections import Counter
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from monitor.models import BackupRegistro
from monitor.seguranca import sha256_arquivo


class Command(BaseCommand):
    help = "Restaura (ou ensaia a restauracao de) um backup gerado por backup_dados."

    def add_arguments(self, parser):
        parser.add_argument("arquivo", help="Caminho do backup .json ou .json.gz")
        parser.add_argument("--ensaio", action="store_true",
                            help="Apenas valida o arquivo; nao escreve no banco.")
        parser.add_argument("--confirmar", action="store_true",
                            help="Exigido para gravar de fato. Sobrescreve dados existentes.")

    def handle(self, *args, **opcoes):
        caminho = Path(opcoes["arquivo"])
        if not caminho.is_file():
            raise CommandError(f"Arquivo nao encontrado: {caminho}")

        ensaio = opcoes["ensaio"]
        if not ensaio and not opcoes["confirmar"]:
            raise CommandError(
                "Restauracao sobrescreve dados existentes. Rode com --ensaio para "
                "conferir o arquivo, ou com --confirmar para executar de verdade."
            )

        self.stdout.write("=" * 64)
        self.stdout.write(f"{'ENSAIO' if ensaio else 'RESTAURACAO'}: {caminho.name}")
        self.stdout.write("=" * 64)

        # --- 1. Integridade -------------------------------------------------
        checksum_atual = sha256_arquivo(caminho)
        self.stdout.write(f"SHA-256 atual: {checksum_atual}")

        registro = BackupRegistro.objects.filter(arquivo=str(caminho)).first()
        if registro and registro.detalhes:
            try:
                esperado = json.loads(registro.detalhes).get("sha256")
            except json.JSONDecodeError:
                esperado = None

            if esperado and esperado != checksum_atual:
                raise CommandError(
                    f"CHECKSUM DIVERGENTE.\n"
                    f"  esperado: {esperado}\n"
                    f"  atual:    {checksum_atual}\n"
                    f"O arquivo foi alterado ou corrompido desde a geracao. "
                    f"Nao restaure sem investigar."
                )
            if esperado:
                self.stdout.write(self.style.SUCCESS("Checksum confere com o registrado na geracao."))
        else:
            self.stdout.write(self.style.WARNING(
                "Sem checksum registrado para comparar (backup anterior a esta versao)."
            ))

        # --- 2. Conteudo ----------------------------------------------------
        try:
            if caminho.suffix == ".gz":
                with gzip.open(caminho, "rb") as arquivo:
                    conteudo = arquivo.read()
            else:
                conteudo = caminho.read_bytes()
            dados = json.loads(conteudo)
        except (OSError, gzip.BadGzipFile, json.JSONDecodeError) as erro:
            raise CommandError(f"Backup ilegivel: {erro}") from erro

        if not isinstance(dados, list):
            raise CommandError("Formato inesperado: o dump deveria ser uma lista de registros.")

        contagem = Counter(item.get("model", "?") for item in dados)
        self.stdout.write(f"\n{len(dados)} registro(s) em {len(contagem)} modelo(s):")
        for modelo, quantidade in sorted(contagem.items()):
            self.stdout.write(f"  {quantidade:>6}  {modelo}")

        if ensaio:
            self.stdout.write(self.style.SUCCESS(
                "\nEnsaio concluido: o arquivo esta integro e restauravel. Nada foi gravado."
            ))
            return

        # --- 3. Restauracao -------------------------------------------------
        # loaddata precisa de um arquivo .json descomprimido no disco.
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as temporario:
            temporario.write(conteudo)
            caminho_temporario = Path(temporario.name)

        try:
            # Tudo numa transacao: se um registro falhar, o banco volta ao
            # estado anterior em vez de ficar meio restaurado.
            with transaction.atomic():
                call_command("loaddata", str(caminho_temporario), verbosity=1)

            BackupRegistro.objects.filter(arquivo=str(caminho)).update(status="restaurado")
            self.stdout.write(self.style.SUCCESS(f"\nRestauracao concluida: {len(dados)} registro(s)."))
        finally:
            caminho_temporario.unlink(missing_ok=True)
