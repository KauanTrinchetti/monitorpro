"""
Popula o banco com dados ficticios, para demonstracao e capturas de tela.

Nada aqui pertence a pessoa ou empresa real:
  - empresas e unidades com nomes genericos, sem CNPJ nem CPF;
  - instalacoes na faixa 9000xxx, reservada a demonstracao neste projeto;
  - e-mails em example.com, dominio reservado pela RFC 2606;
  - IPs da auditoria em 192.0.2.0/24, 198.51.100.0/24 e 203.0.113.0/24,
    faixas reservadas para documentacao pela RFC 5737.

O comando se protege contra uso no lugar errado:
  - so roda com DEBUG=True;
  - recusa banco que tenha empresa, unidade ou fatura que nao seja de
    demonstracao: ficcao nunca se mistura com fatura de cliente;
  - as senhas dos usuarios de demonstracao sao geradas na hora e exibidas
    uma unica vez. Nenhuma senha fica escrita no repositorio.

Uso:
    python manage.py popular_demo
    python manage.py popular_demo --recriar
"""

import random
import secrets
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from monitor.models import (
    AlertaOperacional,
    ArquivoProcessamento,
    AuditoriaEvento,
    BackupRegistro,
    ColetaControle,
    Empresa,
    FaturaCpfl,
    FaturaCpflDetalhe,
    LoteProcessamento,
    PerfilUsuario,
    RegistroAlteracao,
    RoboExecucaoLog,
    UnidadeConsumidora,
)
from monitor.seguranca import mascarar_instalacao, mascarar_nome

User = get_user_model()

PREFIXO_DEMO = "9000"

# Consumo relativo por mes. Verao com ar-condicionado puxa a conta para cima;
# inverno derruba. E o que faz o grafico e o relatorio parecerem reais.
SAZONALIDADE = {
    1: 1.18, 2: 1.20, 3: 1.12, 4: 1.00, 5: 0.92, 6: 0.88,
    7: 0.86, 8: 0.90, 9: 0.97, 10: 1.05, 11: 1.10, 12: 1.16,
}

TARIFA_KWH = {
    "comercial": Decimal("0.98"),
    "residencial": Decimal("0.92"),
    "industrial": Decimal("0.79"),
}

EMPRESAS = [
    {
        "nome": "Grão Dourado Padarias",
        "titular": "GRAO DOURADO PADARIAS",
        "email_alerta": "financeiro.padaria@example.com",
        "unidades": [
            {"nome": "Loja Centro", "sufixo": "101", "tipo": "comercial", "kwh": 2900,
             "limite": 20, "vence_em": 4},
            {"nome": "Loja Shopping", "sufixo": "102", "tipo": "comercial", "kwh": 2300,
             "limite": 15, "vence_em": -12, "ultima_variacao": 1.38},
            {"nome": "Centro de Produção", "sufixo": "103", "tipo": "industrial", "kwh": 9800,
             "limite": 20, "vence_em": -8, "industrial": True},
        ],
    },
    {
        "nome": "Clínica Bem-Estar",
        "titular": "CLINICA BEM ESTAR",
        "email_alerta": "administrativo.clinica@example.com",
        "unidades": [
            {"nome": "Unidade Matriz", "sufixo": "201", "tipo": "comercial", "kwh": 3600,
             "limite": 20, "vence_em": 6},
            {"nome": "Unidade Norte", "sufixo": "202", "tipo": "comercial", "kwh": 1700,
             "limite": 15, "vence_em": -15, "ultima_variacao": 1.21},
        ],
    },
    {
        "nome": "Condomínio Parque das Águas",
        "titular": "COND RESIDENCIAL PARQUE DAS AGUAS",
        "email_alerta": "sindico.condominio@example.com",
        "unidades": [
            {"nome": "Áreas comuns", "sufixo": "301", "tipo": "residencial", "kwh": 1300,
             "limite": 25, "vence_em": -5},
            {"nome": "Bombas e piscina", "sufixo": "302", "tipo": "residencial", "kwh": 2100,
             "limite": 25, "vence_em": -3, "ultima_variacao": 0.74},
        ],
    },
]

USUARIOS = [
    {"username": "demo", "first_name": "Conta", "last_name": "Demonstração",
     "superusuario": True},
    {"username": "gestor.padaria", "first_name": "Gestor", "last_name": "Padaria",
     "empresa": "Grão Dourado Padarias", "papel": "admin"},
    {"username": "leitura.clinica", "first_name": "Leitura", "last_name": "Clínica",
     "empresa": "Clínica Bem-Estar", "papel": "leitura"},
]

NOMES_DEMO = {empresa["nome"] for empresa in EMPRESAS}
USUARIOS_DEMO = {usuario["username"] for usuario in USUARIOS}

MESES_HISTORICO = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]


def _menos_meses(dia, meses):
    """Mesma data `meses` antes, com o dia limitado a 28 para caber em fevereiro."""
    total = dia.year * 12 + (dia.month - 1) - meses
    return date(total // 12, total % 12 + 1, min(dia.day, 28))


def _brl(valor):
    return Decimal(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _milhar(numero):
    return f"{int(numero):,}".replace(",", ".")


class Command(BaseCommand):
    help = "Cria dados ficticios de demonstracao (somente com DEBUG=True e banco sem dados reais)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--recriar",
            action="store_true",
            help="Apaga os dados de demonstracao existentes e cria de novo.",
        )

    def handle(self, *args, **opcoes):
        if not settings.DEBUG:
            raise CommandError(
                "popular_demo so roda com DEBUG=True. Dados ficticios nao entram "
                "em banco de producao."
            )

        self._recusar_banco_com_dados_reais()

        ja_tem_demo = Empresa.objects.filter(nome__in=NOMES_DEMO).exists()
        if ja_tem_demo and not opcoes["recriar"]:
            raise CommandError("Os dados de demonstracao ja existem. Use --recriar para gerar de novo.")

        self.aleatorio = random.Random(2026)
        self.agora = timezone.now()
        self.hoje = timezone.localdate()

        with transaction.atomic():
            if ja_tem_demo:
                self._apagar_demo()
            senhas = self._criar_usuarios()
            unidades = self._criar_empresas_e_unidades()
            faturas = self._criar_faturas(unidades)
            self._criar_alertas()
            self._criar_execucoes_do_robo()
            self._criar_lote_de_upload(faturas)
            self._criar_auditoria(unidades)
            self._criar_backup()

        self._relatar(senhas, len(unidades), len(faturas))

    # ------------------------------------------------------------------
    # Protecoes
    # ------------------------------------------------------------------

    def _recusar_banco_com_dados_reais(self):
        """Aborta se o banco tiver qualquer registro de cliente de verdade."""
        reais = {
            "empresa(s)": Empresa.objects.exclude(nome__in=NOMES_DEMO).count(),
            "unidade(s)": UnidadeConsumidora.objects.exclude(instalacao__startswith=PREFIXO_DEMO).count(),
            "fatura(s)": FaturaCpfl.objects.exclude(instalacao__startswith=PREFIXO_DEMO).count(),
        }
        encontrados = [f"{quantidade} {nome}" for nome, quantidade in reais.items() if quantidade]
        if encontrados:
            raise CommandError(
                "Este banco ja tem dados que nao sao de demonstracao ("
                + ", ".join(encontrados)
                + "). Use um banco separado, por exemplo DB_NAME=monitorpro_demo."
            )

    def _apagar_demo(self):
        """
        Limpa a demonstracao anterior.

        So chega aqui depois de _recusar_banco_com_dados_reais(): o banco nao
        tem empresa, unidade nem fatura real, entao limpar as tabelas de
        operacao nao alcanca dado de ninguem.
        """
        FaturaCpflDetalhe.objects.filter(instalacao__startswith=PREFIXO_DEMO).delete()
        FaturaCpfl.objects.filter(instalacao__startswith=PREFIXO_DEMO).delete()
        for modelo in (ArquivoProcessamento, LoteProcessamento, AlertaOperacional,
                       RoboExecucaoLog, AuditoriaEvento, RegistroAlteracao, BackupRegistro):
            modelo.objects.all().delete()
        ColetaControle.objects.all().delete()
        Empresa.objects.filter(nome__in=NOMES_DEMO).delete()
        User.objects.filter(username__in=USUARIOS_DEMO).delete()

    # ------------------------------------------------------------------
    # Cadastros
    # ------------------------------------------------------------------

    def _criar_usuarios(self):
        senhas = {}
        for dados in USUARIOS:
            senha = secrets.token_urlsafe(12)
            if dados.get("superusuario"):
                usuario = User.objects.create_superuser(dados["username"], email="", password=senha)
            else:
                usuario = User.objects.create_user(dados["username"], password=senha)
            usuario.first_name = dados["first_name"]
            usuario.last_name = dados["last_name"]
            usuario.save(update_fields=["first_name", "last_name"])
            senhas[dados["username"]] = senha
        self.usuario_demo = User.objects.get(username="demo")
        return senhas

    def _criar_empresas_e_unidades(self):
        unidades = []
        self.empresas = {}
        for dados in EMPRESAS:
            empresa = Empresa.objects.create(nome=dados["nome"], email_alerta=dados["email_alerta"])
            self.empresas[empresa.nome] = empresa
            for item in dados["unidades"]:
                instalacao = PREFIXO_DEMO + item["sufixo"]
                unidade = UnidadeConsumidora.objects.create(
                    empresa=empresa,
                    nome=item["nome"],
                    instalacao=instalacao,
                    email_automacao=f"faturas+{instalacao}@example.com",
                    tipo=item["tipo"],
                    limite_aumento_percentual=Decimal(item["limite"]),
                    email_alerta=dados["email_alerta"],
                )
                unidade.config = item
                unidade.titular = dados["titular"]
                unidades.append(unidade)

        for dados in USUARIOS:
            if dados.get("empresa"):
                PerfilUsuario.objects.create(
                    usuario=User.objects.get(username=dados["username"]),
                    empresa=self.empresas[dados["empresa"]],
                    papel=dados["papel"],
                )
        return unidades

    # ------------------------------------------------------------------
    # Faturas
    # ------------------------------------------------------------------

    def _criar_faturas(self, unidades):
        faturas = []
        for unidade in unidades:
            config = unidade.config
            ultima = self.hoje + timedelta(days=config["vence_em"])
            consumo_anterior = None
            for recuo in range(11, -1, -1):
                vencimento = _menos_meses(ultima, recuo)
                consumo = config["kwh"] * SAZONALIDADE[vencimento.month] * self.aleatorio.uniform(0.95, 1.05)
                if recuo == 0 and "ultima_variacao" in config and consumo_anterior:
                    consumo = consumo_anterior * config["ultima_variacao"]
                consumo = int(round(consumo))
                fatura = self._gravar_fatura(unidade, vencimento, consumo)
                faturas.append(fatura)
                consumo_anterior = consumo
        return faturas

    def _gravar_fatura(self, unidade, vencimento, consumo):
        config = unidade.config
        # Bandeira tarifaria amarela/vermelha no fim do periodo seco.
        bandeira = Decimal("1.09") if vencimento.month in (8, 9, 10) else Decimal("1.00")
        valor = Decimal(consumo) * TARIFA_KWH[config["tipo"]] * bandeira
        if config.get("industrial"):
            valor += Decimal("1850.00")  # parcela de demanda contratada
        else:
            valor += Decimal("38.50")  # contribuicao de iluminacao publica

        fim = vencimento - timedelta(days=12)
        inicio = fim - timedelta(days=self.aleatorio.choice([29, 30, 31, 32]))
        fatura = FaturaCpfl.objects.create(
            cliente_nome=unidade.titular,
            data_vencimento=vencimento,
            valor_pagar=_brl(valor),
            instalacao=unidade.instalacao,
            consumo_kwh=str(consumo),
            periodo_leitura=f"{inicio:%d/%m/%Y} a {fim:%d/%m/%Y}",
            pago=vencimento < self.hoje,
        )

        FaturaCpflDetalhe.objects.create(
            fatura_id=fatura.id,
            instalacao=unidade.instalacao,
            data_vencimento=vencimento,
            leituras_medidor=self._leituras(unidade, consumo),
            historico_medidor=self._historico(unidade, vencimento) if config.get("industrial") else [],
            pdf_sha256=secrets.token_hex(32),
            pdf_nome_original=f"fatura_{unidade.instalacao}_{vencimento:%Y_%m}.pdf",
            pdf_tamanho_bytes=self.aleatorio.randint(380, 1400) * 1024,
            pdf_paginas=self.aleatorio.choice([2, 2, 3, 4]),
        )
        return fatura

    def _leituras(self, unidade, consumo):
        medidor = "91" + unidade.instalacao
        anterior = self.aleatorio.randint(40_000, 90_000)
        if not unidade.config.get("industrial"):
            return [{
                "medidor": medidor, "grandeza": "Energia Ativa - kWh", "posto": "Único",
                "leitura_anterior": _milhar(anterior), "leitura_atual": _milhar(anterior + consumo),
                "constante": "1", "consumo": _milhar(consumo), "tipo": "energia",
            }]

        ponta = int(consumo * 0.12)
        fora_ponta = consumo - ponta
        return [
            {"medidor": medidor, "grandeza": "Energia Ativa - kWh", "posto": "Ponta",
             "leitura_anterior": _milhar(anterior // 40), "leitura_atual": _milhar(anterior // 40 + ponta // 40),
             "constante": "40", "consumo": _milhar(ponta), "tipo": "energia"},
            {"medidor": medidor, "grandeza": "Energia Ativa - kWh", "posto": "Fora Ponta",
             "leitura_anterior": _milhar(anterior // 40 * 7), "leitura_atual": _milhar(anterior // 40 * 7 + fora_ponta // 40),
             "constante": "40", "consumo": _milhar(fora_ponta), "tipo": "energia"},
            {"medidor": medidor, "grandeza": "Demanda Ativa - kW", "posto": "Fora Ponta",
             "leitura_anterior": "0", "leitura_atual": "1,54",
             "constante": "40", "consumo": "61,6", "tipo": "demanda"},
        ]

    def _historico(self, unidade, vencimento):
        """Quadro de historico impresso na fatura industrial: 12 meses por posto."""
        grupos = {
            "ponta": {"titulo": "Consumo Ponta", "unidade": "kWh", "tipo": "energia", "linhas": []},
            "fora": {"titulo": "Consumo Fora de Ponta", "unidade": "kWh", "tipo": "energia", "linhas": []},
            "demanda": {"titulo": "Demanda", "unidade": "kW", "tipo": "demanda", "linhas": []},
        }
        for recuo in range(12):
            mes = _menos_meses(vencimento, recuo + 1)
            rotulo = f"{MESES_HISTORICO[mes.month - 1]} {mes:%y}"
            total = unidade.config["kwh"] * SAZONALIDADE[mes.month]
            dias = self.aleatorio.choice([29, 30, 31, 32])
            grupos["ponta"]["linhas"].append({"mes": rotulo, "valor": f"{_milhar(total * 0.12)},00", "dias": dias})
            grupos["fora"]["linhas"].append({"mes": rotulo, "valor": f"{_milhar(total * 0.88)},00", "dias": dias})
            demanda = 58 + 9 * SAZONALIDADE[mes.month] + self.aleatorio.uniform(-2, 2)
            grupos["demanda"]["linhas"].append({"mes": rotulo, "valor": f"{demanda:.2f}".replace(".", ","), "dias": dias})
        return list(grupos.values())

    # ------------------------------------------------------------------
    # Operacao: alertas, robo, upload, auditoria, backup
    # ------------------------------------------------------------------

    def _no_passado(self, **delta):
        return self.agora - timedelta(**delta)

    def _datar(self, objeto, campo, quando):
        """auto_now_add ignora o valor passado no create; corrige depois."""
        type(objeto).objects.filter(pk=objeto.pk).update(**{campo: quando})

    def _criar_alertas(self):
        clinica = self.empresas["Clínica Bem-Estar"]
        norte = UnidadeConsumidora.objects.get(instalacao=PREFIXO_DEMO + "202")
        # Mesmos titulos e textos que o robo grava (robo_leitor_cpfl.py).
        alertas = [
            (None, None, "instalacao_nao_cadastrada", "Instalacao de energia nao cadastrada",
             "O robo leu uma fatura de energia, mas a instalacao 9000999 nao esta cadastrada "
             "em nenhuma unidade consumidora. Cliente no PDF: MERCADO BOA COMPRA.", "high", {"hours": 3}),
            (clinica, norte, "alias_divergente", "E-mail da automacao diferente da instalacao",
             f"A fatura da instalacao {norte.instalacao} foi vinculada pela instalacao a unidade "
             f"{norte.nome}, mas chegou para contas@example.com e o alias cadastrado e "
             f"{norte.email_automacao}.", "medium", {"days": 1}),
        ]
        for empresa, unidade, tipo, titulo, descricao, severidade, quando in alertas:
            alerta = AlertaOperacional.objects.create(
                empresa=empresa, unidade=unidade, tipo=tipo, titulo=titulo,
                descricao=descricao, severidade=severidade,
            )
            self._datar(alerta, "criado_em", self._no_passado(**quando))

    def _criar_execucoes_do_robo(self):
        padaria = self.empresas["Grão Dourado Padarias"]
        # Mensagens iguais as do comando coletar_faturas. Nos detalhes, o dado
        # pessoal aparece mascarado, como no log de verdade.
        execucoes = [
            (None, "sucesso", "Faturas lidas com sucesso",
             f"3 mensagem(ns), 3 fatura(s) gravada(s). Titular {mascarar_nome('GRAO DOURADO PADARIAS')}, "
             f"instalacao {mascarar_instalacao(PREFIXO_DEMO + '101')}.", {"minutes": 40}),
            (padaria, "sucesso", "Faturas lidas com sucesso",
             f"Instalacao {mascarar_instalacao(PREFIXO_DEMO + '101')}: fatura gravada.", {"minutes": 40}),
            (None, "aviso", "Coleta sem novas faturas",
             "Fatura 1: PDF digitalizado, sem texto selecionavel. Peca o PDF original da distribuidora.",
             {"hours": 5}),
            (None, "aviso", "Coleta sem novas faturas",
             f"Fatura 1: instalacao {mascarar_instalacao('9000999')} nao cadastrada.", {"hours": 3}),
            (None, "erro", "Erro critico na coleta",
             "TimeoutError: tempo esgotado ao conectar no servidor IMAP. Nova tentativa na proxima execucao.",
             {"hours": 9}),
            (None, "aviso", "Coleta sem novas faturas", "", {"hours": 11}),
            (None, "sucesso", "Faturas lidas com sucesso",
             "2 mensagem(ns), 2 fatura(s) gravada(s).", {"days": 1, "hours": 2}),
        ]
        for empresa, status, mensagem, detalhes, quando in execucoes:
            inicio = self._no_passado(**quando)
            log = RoboExecucaoLog.objects.create(
                empresa=empresa, status=status, mensagem=mensagem, detalhes=detalhes,
                finalizado_em=inicio + timedelta(seconds=self.aleatorio.randint(8, 70)),
            )
            self._datar(log, "iniciado_em", inicio)

        ColetaControle.objects.create(
            chave="global",
            em_andamento=False,
            iniciado_em=self._no_passado(minutes=41),
            finalizado_em=self._no_passado(minutes=40),
            mensagem="Faturas lidas com sucesso",
        )

    def _criar_lote_de_upload(self, faturas):
        padaria = self.empresas["Grão Dourado Padarias"]
        criado = self._no_passado(hours=2, minutes=15)
        lote = LoteProcessamento.objects.create(
            origem="upload", status="concluido_com_erros", usuario=self.usuario_demo,
            empresa=padaria, total_arquivos=5, finalizado_em=criado + timedelta(minutes=2),
        )
        self._datar(lote, "criado_em", criado)

        duplicada = faturas[0]
        arquivos = [
            ("fatura_loja_centro_outubro.pdf", "concluido", 2, 412, "1 fatura(s) gravada(s)."),
            ("consolidado_padaria_3_unidades.pdf", "concluido", 9, 1380, "3 fatura(s) gravada(s)."),
            ("../../core/settings.pdf", "concluido", 2, 398,
             "1 fatura(s) gravada(s). Nome do arquivo saneado antes da gravacao."),
            ("fatura_protegida.pdf", "recusado", 0, 220,
             "PDF protegido por senha; nao e possivel extrair dados."),
            ("fatura_repetida.pdf", "recusado", 2, 405,
             f"Arquivo identico ja processado (fatura #{duplicada.id})."),
        ]
        for nome, status, paginas, kb, mensagem in arquivos:
            ArquivoProcessamento.objects.create(
                lote=lote, nome_original=nome, caminho="", sha256=secrets.token_hex(32),
                tamanho_bytes=kb * 1024, paginas=paginas, status=status, tentativas=1 if status == "concluido" else 0,
                mensagem=mensagem, iniciado_em=criado, finalizado_em=criado + timedelta(minutes=1),
            )

    def _criar_auditoria(self, unidades):
        padaria = self.empresas["Grão Dourado Padarias"]
        clinica = self.empresas["Clínica Bem-Estar"]
        gestor = User.objects.get(username="gestor.padaria")
        leitura = User.objects.get(username="leitura.clinica")
        shopping = next(u for u in unidades if u.instalacao == PREFIXO_DEMO + "102")

        eventos = [
            (self.usuario_demo, None, "login_sucesso", "Login realizado com sucesso", "192.0.2.10", {"minutes": 5}),
            (None, None, "limite_excedido", "Limite de tentativas de login por origem", "203.0.113.45", {"minutes": 52}),
            (None, None, "login_falha", "Tentativa de login invalida", "203.0.113.45", {"minutes": 53}),
            (None, None, "login_falha", "Tentativa de login invalida", "203.0.113.45", {"minutes": 54}),
            (None, None, "login_falha", "Tentativa de login invalida", "203.0.113.45", {"minutes": 55}),
            (leitura, clinica, "acesso_negado", "Tentativa de acessar configuracoes sem permissao administrativa",
             "198.51.100.23", {"hours": 1, "minutes": 30}),
            (self.usuario_demo, None, "upload_faturas", "Upload de 5 arquivo(s): 3 na fila, 2 recusado(s)",
             "192.0.2.10", {"hours": 2, "minutes": 15}),
            (gestor, padaria, "unidade_editada", f"Unidade {shopping.nome} atualizada", "198.51.100.7", {"hours": 4}),
            (gestor, padaria, "exportacao_pdf", "Relatorio PDF exportado com 36 fatura(s)", "198.51.100.7", {"hours": 4, "minutes": 10}),
            (gestor, padaria, "exportacao_csv", "Relatorio CSV exportado com 36 fatura(s)", "198.51.100.7", {"hours": 4, "minutes": 12}),
            (self.usuario_demo, None, "backup_criado", "Backup JSON criado pelo painel", "192.0.2.10", {"hours": 20}),
            (None, None, "expurgo_arquivos", "Expurgo de arquivos de fatura: 6 removido(s)", None, {"days": 2}),
        ]
        for usuario, empresa, acao, resumo, ip, quando in eventos:
            evento = AuditoriaEvento.objects.create(
                usuario=usuario, empresa=empresa, acao=acao, resumo=resumo, ip=ip,
                user_agent="Mozilla/5.0 (demonstracao)" if ip else "",
            )
            self._datar(evento, "criado_em", self._no_passado(**quando))

        # A alteracao que acompanha o evento "Unidade atualizada": o antes e o
        # depois de cada campo, que e o que permite contestar uma mudanca.
        for campo, antes, depois in [
            ("limite_aumento_percentual", "20.00", "15.00"),
            ("email_alerta", "-", "financeiro.padaria@example.com"),
        ]:
            registro = RegistroAlteracao.objects.create(
                usuario=gestor, empresa=padaria, modelo="UnidadeConsumidora", objeto_id=str(shopping.id),
                rotulo_objeto=shopping.nome, operacao="alteracao", campo=campo,
                valor_anterior=antes, valor_novo=depois, ip="198.51.100.7",
            )
            self._datar(registro, "criado_em", self._no_passado(hours=4))

    def _criar_backup(self):
        quando = timezone.make_aware(datetime.combine(self.hoje - timedelta(days=1), time(3, 0)))
        backup = BackupRegistro.objects.create(
            arquivo=f"backups/backup_monitor_{quando:%Y%m%d_%H%M%S}.json.gz",
            tamanho_bytes=48_913,
            detalhes="Backup comprimido com SHA-256 registrado. Ensaio de restauracao aprovado.",
        )
        self._datar(backup, "criado_em", quando)

    # ------------------------------------------------------------------

    def _relatar(self, senhas, total_unidades, total_faturas):
        self.stdout.write("=" * 66)
        self.stdout.write(self.style.SUCCESS("Dados de demonstracao criados"))
        self.stdout.write("=" * 66)
        self.stdout.write(f"{len(EMPRESAS)} empresas, {total_unidades} unidades, {total_faturas} faturas, todas ficticias.")
        self.stdout.write("\nUsuarios (senhas geradas agora; anote, elas nao serao exibidas de novo):")
        for usuario in USUARIOS:
            papel = "superusuario" if usuario.get("superusuario") else f"{usuario['papel']} de {usuario['empresa']}"
            self.stdout.write(f"  {usuario['username']:<16} {senhas[usuario['username']]:<20} {papel}")
        self.stdout.write("=" * 66)
