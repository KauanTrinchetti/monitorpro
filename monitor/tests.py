"""
Testes do MonitorPro.

Rodar antes de cada deploy:
    python manage.py test monitor

Cobrem tres frentes:
  1. Renderizacao — nenhuma tag de template pode vazar como texto na pagina.
  2. Seguranca — cada defesa realmente bloqueia o ataque correspondente.
  3. Cabecalhos — CSP e companhia presentes nas respostas.
"""

import csv
import io
import re
import tempfile
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings

from .models import (AlertaOperacional, AuditoriaEvento, BackupRegistro, Empresa, FaturaCpfl,
                     PerfilUsuario, RoboExecucaoLog, UnidadeConsumidora)
from .seguranca import (
    DestinoInvalido,
    PdfInvalido,
    caminho_dentro_de,
    enviar_sem_redirecionar,
    gravar_arquivo_seguro,
    limpar_detalhes,
    mascarar_documento,
    mascarar_email,
    mascarar_nome,
    nome_arquivo_seguro,
    sha256_bytes,
    validar_pdf,
    validar_url_webhook,
)

User = get_user_model()


class RenderizacaoTest(TestCase):
    """
    Nenhuma tag de template pode chegar ao navegador como texto.

    Existe por causa de um bug real: comentarios `{# ... #}` escritos em duas
    linhas nao sao reconhecidos pelo Django (a sintaxe so vale dentro de uma
    linha) e apareciam no topo da tela. Conferir apenas o status HTTP nao
    pegava — a pagina devolvia 200 com o comentario visivel.
    """

    ROTAS = [
        "/login/", "/", "/relatorio/", "/financeiro/", "/historico/",
        "/alertas/", "/conformidade/", "/coletas/", "/logs/", "/unidades/",
        "/unidades/nova/", "/configuracoes/", "/faturas/upload/",
    ]

    SUSPEITOS = [
        (r"\{#", "comentario {# #} nao processado"),
        (r"\{%\s*(comment|endcomment|if|for|block|url|static|csrf)", "tag {% %} nao processada"),
        (r"\{\{\s*\w", "variavel {{ }} nao processada"),
        (r"\bendcomment\b", "endcomment literal"),
    ]

    def setUp(self):
        cache.clear()
        self.empresa = Empresa.objects.create(nome="Empresa de Teste")
        self.usuario = User.objects.create_user("tester", password="Senha-De-Teste-2026!")
        PerfilUsuario.objects.create(usuario=self.usuario, empresa=self.empresa, papel="admin")
        self.client.force_login(self.usuario)

    def test_nenhuma_tag_de_template_vaza_no_html(self):
        for rota in self.ROTAS:
            with self.subTest(rota=rota):
                resposta = self.client.get(rota, follow=True)
                html = resposta.content.decode("utf-8", "ignore")

                for padrao, descricao in self.SUSPEITOS:
                    achado = re.search(padrao, html)
                    if achado:
                        trecho = html[max(0, achado.start() - 40):achado.start() + 80]
                        self.fail(f"{rota}: {descricao}\n  ...{trecho.strip()}...")

    def test_todas_as_rotas_respondem(self):
        for rota in self.ROTAS:
            with self.subTest(rota=rota):
                resposta = self.client.get(rota, follow=True)
                self.assertLess(resposta.status_code, 400, f"{rota} devolveu {resposta.status_code}")


class CabecalhosDeSegurancaTest(TestCase):

    def test_cabecalhos_presentes(self):
        resposta = self.client.get("/login/")
        for cabecalho in [
            "Content-Security-Policy",
            "X-Frame-Options",
            "X-Content-Type-Options",
            "Referrer-Policy",
            "Permissions-Policy",
        ]:
            self.assertIn(cabecalho, resposta.headers, f"{cabecalho} ausente")

    def test_csp_nao_permite_script_inline(self):
        csp = self.client.get("/login/").headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        # 'unsafe-inline' em script-src anularia a defesa contra XSS.
        script_src = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
        self.assertNotIn("unsafe-inline", script_src)

    def test_tela_autenticada_nao_fica_em_cache(self):
        empresa = Empresa.objects.create(nome="Empresa Cache")
        usuario = User.objects.create_user("cache_user", password="Senha-De-Teste-2026!")
        PerfilUsuario.objects.create(usuario=usuario, empresa=empresa, papel="leitura")
        self.client.force_login(usuario)

        resposta = self.client.get("/alertas/")
        self.assertIn("no-store", resposta.headers.get("Cache-Control", ""))


class NomeArquivoSeguroTest(TestCase):
    """
    O nome do anexo/upload e escolhido por quem envia. Sem tratamento, um nome
    como "../../core/settings.py" grava fora do diretorio de destino.
    """

    ATAQUES = [
        "../../../core/settings.py",
        "..\\..\\..\\Windows\\System32\\evil.pdf",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "....//....//settings.py",
        "fatura.pdf\x00.exe",
        "CON.pdf",
        "NUL",
    ]

    def test_travessia_de_diretorio_neutralizada(self):
        for ataque in self.ATAQUES:
            with self.subTest(ataque=ataque):
                seguro = nome_arquivo_seguro(ataque)
                self.assertNotIn("/", seguro)
                self.assertNotIn("\\", seguro)
                self.assertNotIn("..", seguro)
                self.assertTrue(seguro.endswith(".pdf"))
                self.assertEqual(Path(seguro).name, seguro)

    def test_gravacao_fica_confinada_ao_diretorio_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "recebidos"
            destino = gravar_arquivo_seguro(base, "../../fuga.pdf", b"%PDF-1.4 x")
            self.assertTrue(caminho_dentro_de(base, destino), f"escapou para {destino}")

    def test_nomes_diferentes_para_o_mesmo_nome_de_origem(self):
        # Sufixo aleatorio impede que dois anexos homonimos se sobrescrevam.
        self.assertNotEqual(nome_arquivo_seguro("fatura.pdf"), nome_arquivo_seguro("fatura.pdf"))


class ValidacaoPdfTest(TestCase):
    """A extensao do nome nao prova nada; o conteudo real e que vale."""

    def gravar(self, nome, conteudo):
        caminho = Path(self.tmp.name) / nome
        caminho.write_bytes(conteudo)
        return caminho

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_executavel_renomeado_para_pdf_e_recusado(self):
        caminho = self.gravar("malware.pdf", b"MZ\x90\x00\x03" + b"\x00" * 200)
        with self.assertRaises(PdfInvalido):
            validar_pdf(caminho)

    def test_arquivo_vazio_recusado(self):
        with self.assertRaises(PdfInvalido):
            validar_pdf(self.gravar("vazio.pdf", b""))

    def test_arquivo_acima_do_limite_recusado(self):
        caminho = self.gravar("grande.pdf", b"%PDF-" + b"A" * (2 * 1024 * 1024))
        with self.assertRaises(PdfInvalido):
            validar_pdf(caminho, tamanho_maximo=1024 * 1024)

    def test_pdf_corrompido_recusado(self):
        caminho = self.gravar("quebrado.pdf", b"%PDF-1.4\nlixo que nao e pdf\n")
        with self.assertRaises(PdfInvalido):
            validar_pdf(caminho)


class WebhookSsrfTest(TestCase):
    """urlopen aceita file:// e segue redirects — o destino precisa ser filtrado."""

    PROIBIDOS = [
        "file:///C:/Windows/win.ini",
        "http://169.254.169.254/latest/meta-data/",   # metadata service da nuvem
        "http://127.0.0.1:8000/admin/",
        "http://localhost/",
        "https://127.0.0.1/",
        "gopher://evil.com/",
        "http://[::1]/",
        "",
    ]

    @override_settings(DEBUG=False)
    def test_destinos_internos_e_esquemas_perigosos_bloqueados(self):
        for url in self.PROIBIDOS:
            with self.subTest(url=url):
                with self.assertRaises((DestinoInvalido, Exception)):
                    validar_url_webhook(url)

    def test_webhook_nao_segue_redirect(self):
        # A validacao olha o destino informado. Se a chamada seguisse um 302,
        # um host publico poderia mandar a requisicao para dentro da rede
        # (169.254.169.254, por exemplo) depois de validado.
        visitados = []

        class Servidor(BaseHTTPRequestHandler):
            def do_POST(self):
                visitados.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(302)
                self.send_header("Location", "/interno")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *args):
                pass

        servidor = HTTPServer(("127.0.0.1", 0), Servidor)
        Thread(target=servidor.serve_forever, daemon=True).start()
        self.addCleanup(servidor.server_close)
        self.addCleanup(servidor.shutdown)

        url = f"http://127.0.0.1:{servidor.server_port}/webhook"
        requisicao = urllib.request.Request(url, data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as contexto:
            enviar_sem_redirecionar(requisicao, timeout=5)

        self.assertEqual(contexto.exception.code, 302)
        self.assertEqual(visitados, ["/webhook"])


class MascaramentoTest(TestCase):
    """Log e auditoria nao guardam dado pessoal nem segredo em claro."""

    def test_nome_email_e_documento_mascarados(self):
        self.assertNotIn("SILVA", mascarar_nome("MARIA DA SILVA SOUZA"))
        self.assertNotIn("contato", mascarar_email("contato@empresa.com.br"))
        mascarado = mascarar_documento("123.456.789-00")
        self.assertTrue(mascarado.endswith("900"))
        self.assertNotIn("123", mascarado)

    def test_segredos_removidos_da_auditoria(self):
        limpo = limpar_detalhes({
            "usuario": "joao",
            "password": "segredo123",
            "detalhe": {"token": "abc", "ok": 1},
        })
        self.assertEqual(limpo["password"], "[removido]")
        self.assertEqual(limpo["detalhe"]["token"], "[removido]")
        self.assertEqual(limpo["usuario"], "joao")


class RateLimitTest(TestCase):

    def setUp(self):
        cache.clear()

    def test_brute_force_de_login_e_bloqueado(self):
        bloqueou_em = None
        for tentativa in range(1, 16):
            resposta = self.client.post(
                "/login/", {"username": "naoexiste", "password": "errada"}, follow=True
            )
            corpo = resposta.content.decode("utf-8", "ignore")
            if "Muitas tentativas" in corpo:
                bloqueou_em = tentativa
                break

        self.assertIsNotNone(bloqueou_em, "brute force nao foi bloqueado em 15 tentativas")
        self.assertLessEqual(bloqueou_em, 12)

    def test_x_forwarded_for_forjado_nao_reseta_o_limite(self):
        # Sem proxy confiavel configurado, o cabecalho tem de ser ignorado —
        # senao basta troca-lo a cada tentativa para burlar o limite.
        for i in range(12):
            self.client.post("/login/", {"username": "alvo", "password": "x"},
                             HTTP_X_FORWARDED_FOR=f"1.2.3.{i}", follow=True)

        resposta = self.client.post("/login/", {"username": "alvo", "password": "x"},
                                    HTTP_X_FORWARDED_FOR="9.9.9.9", follow=True)
        self.assertIn("Muitas tentativas", resposta.content.decode("utf-8", "ignore"))

    def test_texto_digitado_como_usuario_nao_fica_gravado(self):
        # Quem erra de campo digita a senha no lugar do usuario. Esse texto
        # nao pode parar em claro nem na auditoria nem na chave do cache —
        # as duas ficam gravadas no banco.
        digitado = "MinhaSenhaNoCampoErrado"
        for _ in range(10):
            self.client.post("/login/", {"username": digitado, "password": "x"}, follow=True)

        self.assertTrue(AuditoriaEvento.objects.filter(acao="limite_excedido").exists())
        resumos = " ".join(AuditoriaEvento.objects.values_list("resumo", flat=True)).lower()
        self.assertNotIn(digitado.lower(), resumos)

        with connection.cursor() as cursor:
            cursor.execute(f"SELECT cache_key FROM {settings.CACHES['default']['LOCATION']}")
            chaves = " ".join(linha[0] for linha in cursor.fetchall()).lower()
        self.assertNotIn(digitado.lower(), chaves)


class ChecksumTest(TestCase):

    def test_hash_distingue_conteudo_e_e_estavel(self):
        original = sha256_bytes(b"%PDF-1.4 conteudo original")
        adulterado = sha256_bytes(b"%PDF-1.4 conteudo adulterado")

        self.assertNotEqual(original, adulterado)
        self.assertEqual(original, sha256_bytes(b"%PDF-1.4 conteudo original"))
        self.assertEqual(len(original), 64)


class SenhaTest(TestCase):

    def test_hasher_forte_em_uso(self):
        from django.contrib.auth.hashers import identify_hasher, make_password

        algoritmo = identify_hasher(make_password("uma-senha-de-teste")).algorithm
        # Argon2id e o preferido; PBKDF2 e o piso aceitavel quando o pacote
        # argon2-cffi nao esta instalado.
        self.assertIn(algoritmo, ["argon2", "pbkdf2_sha256"])


class IsolamentoEntreEmpresasTest(TestCase):
    """
    Um cliente nunca pode ver dado de outro.

    Existe porque a lista de backups vazava: `BackupRegistro.objects.all()`
    era exibida a qualquer administrador, e o arquivo de backup contem
    Empresa, PerfilUsuario, UnidadeConsumidora e detalhes de fatura de
    *todas* as empresas. O admin de um cliente podia ate dispara-lo.
    """

    def setUp(self):
        cache.clear()

        self.empresa_a = Empresa.objects.create(nome="Empresa A")
        self.empresa_b = Empresa.objects.create(nome="Empresa B")

        self.admin_a = User.objects.create_user("admin_a", password="Senha-De-Teste-2026!")
        PerfilUsuario.objects.create(usuario=self.admin_a, empresa=self.empresa_a, papel="admin")

        self.admin_b = User.objects.create_user("admin_b", password="Senha-De-Teste-2026!")
        PerfilUsuario.objects.create(usuario=self.admin_b, empresa=self.empresa_b, papel="admin")

        self.superusuario = User.objects.create_superuser("root", password="Senha-De-Teste-2026!", email="")

        UnidadeConsumidora.objects.create(empresa=self.empresa_a, nome="Unidade A", instalacao="1111111")
        UnidadeConsumidora.objects.create(empresa=self.empresa_b, nome="Unidade B", instalacao="2222222")

        RoboExecucaoLog.objects.create(empresa=self.empresa_a, status="sucesso", mensagem="coleta A")
        RoboExecucaoLog.objects.create(empresa=self.empresa_b, status="sucesso", mensagem="coleta B")

        AlertaOperacional.objects.create(
            empresa=self.empresa_a, tipo="teste", titulo="Alerta A", descricao="so da empresa A"
        )

        BackupRegistro.objects.create(arquivo="backup_do_sistema.json.gz", tamanho_bytes=1)

    def test_unidades_de_outra_empresa_nao_aparecem(self):
        self.client.force_login(self.admin_a)
        nomes = [u["nome"] for u in self.client.get("/unidades/").context["unidades"]]
        self.assertIn("Unidade A", nomes)
        self.assertNotIn("Unidade B", nomes)

    def test_coletas_de_outra_empresa_nao_aparecem(self):
        self.client.force_login(self.admin_a)
        empresas = {log.empresa_id for log in self.client.get("/coletas/").context["logs"]}
        self.assertNotIn(self.empresa_b.id, empresas)

    def test_alertas_de_outra_empresa_nao_aparecem(self):
        self.client.force_login(self.admin_b)
        corpo = self.client.get("/alertas/").content.decode("utf-8", "ignore")
        self.assertNotIn("so da empresa A", corpo)

    def test_administrador_de_cliente_nao_ve_a_lista_de_backups(self):
        # O backup e do sistema inteiro; nem a existencia dele interessa ao
        # administrador de um cliente.
        self.client.force_login(self.admin_a)
        resposta = self.client.get("/logs/")
        self.assertEqual(list(resposta.context["backups"]), [])
        self.assertNotIn("backup_do_sistema.json.gz", resposta.content.decode("utf-8", "ignore"))

    def test_administrador_de_cliente_nao_consegue_gerar_backup(self):
        self.client.force_login(self.admin_a)
        antes = BackupRegistro.objects.count()
        self.client.post("/backup/", {"operation_token": "qualquer"}, follow=True)
        self.assertEqual(BackupRegistro.objects.count(), antes, "admin de cliente gerou backup do sistema")

    def test_superusuario_continua_vendo_os_backups(self):
        self.client.force_login(self.superusuario)
        self.assertTrue(self.client.get("/logs/").context["backups"])


class TabelaDeFaturasTest(TestCase):
    """
    Um clone novo do repositorio precisa subir so com `migrate`.

    A tabela `faturas_cpfl` e `managed = False` e nenhuma migration a criava:
    ela existia apenas no banco onde o sistema nasceu, e um ambiente novo
    quebrava na primeira tela com "relation faturas_cpfl does not exist".
    """

    def test_migrations_criam_a_tabela_com_a_constraint_unica(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.faturas_cpfl')")
            self.assertIsNotNone(cursor.fetchone()[0], "migrate nao criou faturas_cpfl")
            cursor.execute(
                "SELECT 1 FROM pg_constraint WHERE conname = 'uq_fatura' "
                "AND conrelid = 'faturas_cpfl'::regclass"
            )
            self.assertIsNotNone(cursor.fetchone(), "faturas_cpfl sem UNIQUE (instalacao, data_vencimento)")


class LoginDoAdminTest(TestCase):
    """
    O login proprio do admin do Django nao passa pelo rate limit nem pela
    auditoria da tela /login/. Era uma segunda porta para forca bruta contra
    justamente as contas mais privilegiadas do sistema.
    """

    def setUp(self):
        cache.clear()
        self.chefe = User.objects.create_superuser("chefe", password="Senha-De-Teste-2026!", email="")

    def test_login_do_admin_redireciona_para_a_tela_com_rate_limit(self):
        resposta = self.client.get("/admin/login/")
        self.assertRedirects(resposta, "/login/", fetch_redirect_response=False)

    def test_post_no_login_do_admin_nao_autentica(self):
        self.client.post("/admin/login/", {"username": "chefe", "password": "Senha-De-Teste-2026!"})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_superusuario_logado_continua_acessando_o_admin(self):
        self.client.force_login(self.chefe)
        self.assertEqual(self.client.get("/admin/").status_code, 200)


class ExportacaoComTextoMaliciosoTest(TestCase):
    """
    Nome de empresa, de unidade e de titular sao texto livre — o de titular
    vem de dentro do PDF, que qualquer remetente controla. Nenhum deles pode
    virar formula na planilha nem marcacao no relatorio em PDF.
    """

    def setUp(self):
        cache.clear()
        self.empresa = Empresa.objects.create(nome="Empresa Exportacao")
        usuario = User.objects.create_user("exportador", password="Senha-De-Teste-2026!")
        PerfilUsuario.objects.create(usuario=usuario, empresa=self.empresa, papel="admin")
        self.client.force_login(usuario)

    def criar_fatura(self, nome_unidade, titular):
        UnidadeConsumidora.objects.create(empresa=self.empresa, nome=nome_unidade, instalacao="3333333")
        FaturaCpfl.objects.create(
            cliente_nome=titular,
            data_vencimento=date(2026, 9, 10),
            valor_pagar=Decimal("150.00"),
            instalacao="3333333",
            consumo_kwh="120",
            periodo_leitura="01/08/2026 a 31/08/2026",
        )

    def test_csv_neutraliza_injecao_de_formula(self):
        formula = '=HYPERLINK("https://exemplo.invalid";"Clique aqui")'
        self.criar_fatura(formula, "+CMD|' /C calc'!A0")

        corpo = self.client.post("/exportar-csv/").content.decode("utf-8-sig")
        linha = next(csv.DictReader(io.StringIO(corpo), delimiter=";"))

        # O apostrofo inicial faz a planilha exibir o valor como texto.
        self.assertEqual(linha["Unidade"], "'" + formula)
        self.assertEqual(linha["Cliente"], "'+CMD|' /C calc'!A0")

    def test_pdf_nao_transforma_nome_em_link(self):
        self.criar_fatura('Loja <a href="https://exemplo.invalid">pague aqui</a>', "TITULAR TESTE")

        resposta = self.client.post("/exportar_pdf/")

        self.assertEqual(resposta.status_code, 200)
        # Um <a href> interpretado vira anotacao de link (/URI) no PDF.
        self.assertNotIn(b"/URI", resposta.content)

    def test_pdf_com_marcacao_quebrada_nao_derruba_a_exportacao(self):
        self.criar_fatura("Galpao <b & <font", "TITULAR <i>TESTE")

        resposta = self.client.post("/exportar_pdf/")

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.content.startswith(b"%PDF-"))


class PopularDemoTest(TestCase):
    """
    Os dados de demonstracao alimentam as capturas de tela publicadas. O
    comando nunca pode rodar em producao nem misturar ficcao com fatura real.
    """

    def popular(self, *argumentos):
        call_command("popular_demo", *argumentos, stdout=io.StringIO())

    @override_settings(DEBUG=False)
    def test_recusa_rodar_sem_debug(self):
        with self.assertRaises(CommandError):
            self.popular()

    @override_settings(DEBUG=True)
    def test_recusa_banco_que_ja_tem_dado_real(self):
        Empresa.objects.create(nome="Cliente de Verdade")
        with self.assertRaises(CommandError):
            self.popular()
        self.assertFalse(User.objects.filter(username="demo").exists())

    @override_settings(DEBUG=True)
    def test_cria_somente_dados_ficticios_e_recria_sem_duplicar(self):
        self.popular()
        total = FaturaCpfl.objects.count()

        self.assertGreater(total, 0)
        self.assertFalse(FaturaCpfl.objects.exclude(instalacao__startswith="9000").exists())
        self.assertFalse(UnidadeConsumidora.objects.exclude(email_automacao__endswith="@example.com").exists())

        self.popular("--recriar")
        self.assertEqual(FaturaCpfl.objects.count(), total)
