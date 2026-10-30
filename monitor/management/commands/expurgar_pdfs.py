"""
Expurgo de arquivos de fatura que ficaram em disco.

Fatura de cliente e dado pessoal. Manter esses arquivos indefinidamente no
sistema de arquivos amplia a superficie exposta sem beneficio: o que interessa
ja esta no banco, com controle de acesso. Os PDFs de falha existem para
diagnostico, e diagnostico tem prazo.

Por padrao apenas lista o que seria removido. Apagar exige --confirmar.

Agendamento sugerido (semanal):
    python manage.py expurgar_pdfs --dias 90 --confirmar
"""

import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from monitor.models import AuditoriaEvento

log = logging.getLogger("monitor")

# Extensoes que podem conter dado de fatura: o PDF em si e as paginas
# renderizadas como imagem durante a depuracao do leitor.
EXTENSOES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


class Command(BaseCommand):
    help = "Remove PDFs e imagens de fatura mais antigos que o prazo de retencao."

    def add_arguments(self, parser):
        parser.add_argument("--dias", type=int, default=90,
                            help="Prazo de retencao em dias. Padrao: 90.")
        parser.add_argument("--confirmar", action="store_true",
                            help="Apaga de fato. Sem esta flag, apenas lista.")
        parser.add_argument("--incluir-tmp", action="store_true",
                            help="Inclui tmp/pdfs (artefatos de depuracao do leitor).")

    def handle(self, *args, **opcoes):
        dias = opcoes["dias"]
        confirmar = opcoes["confirmar"]
        base = Path(settings.BASE_DIR)
        limite = timezone.now() - timedelta(days=dias)

        pastas = [
            Path(getattr(settings, "PDFS_FALHA_DIR", base / "pdfs_falha")),
            Path(getattr(settings, "PDFS_RECEBIDOS_DIR", base / "pdfs_recebidos")),
        ]
        if opcoes["incluir_tmp"]:
            pastas.append(base / "tmp" / "pdfs")

        self.stdout.write("=" * 66)
        self.stdout.write(f"{'EXPURGO' if confirmar else 'SIMULACAO'} — retencao de {dias} dias")
        self.stdout.write("=" * 66)

        total_arquivos = 0
        total_bytes = 0
        mantidos = 0

        for pasta in pastas:
            if not pasta.is_dir():
                continue

            self.stdout.write(f"\n{pasta.name}/")
            achou = False

            for arquivo in sorted(pasta.iterdir()):
                if not arquivo.is_file() or arquivo.suffix.lower() not in EXTENSOES:
                    continue

                achou = True
                modificado = timezone.datetime.fromtimestamp(
                    arquivo.stat().st_mtime, tz=timezone.get_current_timezone()
                )
                idade = (timezone.now() - modificado).days
                tamanho = arquivo.stat().st_size

                if modificado >= limite:
                    mantidos += 1
                    self.stdout.write(f"  mantem   {arquivo.name[:52]:52} {idade:>3}d")
                    continue

                total_arquivos += 1
                total_bytes += tamanho

                if confirmar:
                    try:
                        arquivo.unlink()
                        self.stdout.write(self.style.WARNING(
                            f"  APAGADO  {arquivo.name[:52]:52} {idade:>3}d"))
                    except OSError as erro:
                        self.stderr.write(f"  ERRO     {arquivo.name}: {erro}")
                        total_arquivos -= 1
                        total_bytes -= tamanho
                else:
                    self.stdout.write(self.style.WARNING(
                        f"  apagaria {arquivo.name[:52]:52} {idade:>3}d"))

            if not achou:
                self.stdout.write("  (vazia)")

        self.stdout.write("\n" + "=" * 66)
        resumo = (f"{total_arquivos} arquivo(s), {total_bytes / 1024 / 1024:.1f} MB | "
                  f"{mantidos} dentro do prazo")

        if confirmar and total_arquivos:
            # O expurgo apaga dado pessoal: precisa ficar registrado quem
            # apagou o que e quando.
            AuditoriaEvento.objects.create(
                acao="expurgo_arquivos",
                resumo=f"Expurgo de arquivos de fatura: {total_arquivos} removido(s)"[:255],
                alvo_tipo="expurgo",
                detalhes={
                    "arquivos_removidos": total_arquivos,
                    "bytes_liberados": total_bytes,
                    "retencao_dias": dias,
                },
            )
            self.stdout.write(self.style.SUCCESS(f"Removido: {resumo}"))
        elif confirmar:
            self.stdout.write("Nada a remover: " + resumo)
        else:
            self.stdout.write(f"Simulacao: {resumo}")
            if total_arquivos:
                self.stdout.write("Rode de novo com --confirmar para apagar.")
        self.stdout.write("=" * 66)
