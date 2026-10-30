"""
Utilitarios de seguranca compartilhados entre o painel e o robo de coleta.

Concentra aqui o que precisa de tratamento consistente nos dois lados:
identificacao de cliente, mascaramento de dado pessoal em log, validacao de
destino de webhook (SSRF), nome de arquivo seguro (path traversal) e
validacao de PDF antes do parsing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import secrets
import socket
import unicodedata
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

log_seguranca = logging.getLogger("monitor.seguranca")


# ==========================================================================
# Identificacao do cliente
# ==========================================================================

def ip_cliente(request) -> str | None:
    """
    IP de origem da requisicao.

    X-Forwarded-For so e considerado quando ATRAS_DE_PROXY_CONFIAVEL=True.
    Sem proxy na frente, qualquer cliente pode enviar o cabecalho que quiser:
    o rate limit por IP viraria burlavel (basta trocar o header a cada
    tentativa) e a trilha de auditoria registraria origem falsa.

    Com proxy confiavel, usa-se o ultimo salto da cadeia — o valor escrito
    pelo proprio proxy — e nao o primeiro, que o cliente controla.
    """
    if getattr(settings, "ATRAS_DE_PROXY_CONFIAVEL", False):
        encaminhado = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if encaminhado:
            saltos = [parte.strip() for parte in encaminhado.split(",") if parte.strip()]
            if saltos:
                candidato = saltos[-1]
                if _ip_valido(candidato):
                    return candidato

    remoto = request.META.get("REMOTE_ADDR")
    return remoto if remoto and _ip_valido(remoto) else None


def _ip_valido(valor: str) -> bool:
    try:
        ipaddress.ip_address(valor)
        return True
    except ValueError:
        return False


# ==========================================================================
# Mascaramento de dado pessoal para log
# ==========================================================================
# Nome de cliente, e-mail e documento nao entram em log de aplicacao em
# claro. O dado permanece integral no banco, onde ha controle de acesso;
# o arquivo de log costuma ser copiado, rotacionado e enviado para suporte.

def mascarar_nome(nome: str | None) -> str:
    """"MARIA DA SILVA SOUZA" -> "M****A D* S**** S****" """
    if not nome:
        return "-"
    partes = []
    for palavra in str(nome).split():
        if len(palavra) <= 2:
            partes.append(palavra[0] + "*" * (len(palavra) - 1))
        else:
            partes.append(f"{palavra[0]}{'*' * (len(palavra) - 2)}{palavra[-1]}")
    return " ".join(partes) or "-"


def mascarar_email(endereco: str | None) -> str:
    """"contato@empresa.com.br" -> "co****@empresa.com.br" """
    if not endereco or "@" not in str(endereco):
        return "-"
    usuario, _, dominio = str(endereco).partition("@")
    visivel = usuario[:2]
    return f"{visivel}{'*' * max(len(usuario) - 2, 1)}@{dominio}"


def mascarar_documento(documento: str | None) -> str:
    """CPF/CNPJ com apenas os 3 ultimos digitos visiveis."""
    digitos = re.sub(r"\D", "", str(documento or ""))
    if not digitos:
        return "-"
    return f"{'*' * max(len(digitos) - 3, 0)}{digitos[-3:]}"


def mascarar_instalacao(instalacao: str | None) -> str:
    """Numero de instalacao: mantem os 4 ultimos digitos para rastreio."""
    valor = str(instalacao or "").strip()
    if len(valor) <= 4:
        return valor or "-"
    return f"{'*' * (len(valor) - 4)}{valor[-4:]}"


# Chaves que nunca devem ser gravadas em AuditoriaEvento.detalhes nem em log.
_CAMPOS_PROIBIDOS = {
    "password", "senha", "passwd", "pwd", "secret", "token", "authorization",
    "api_key", "apikey", "senha_app", "db_password", "secret_key",
    "csrfmiddlewaretoken", "email_host_password",
}


def limpar_detalhes(detalhes: dict | None) -> dict:
    """
    Remove segredos de um dicionario antes de persistir em auditoria.

    Serve de rede de seguranca: mesmo que um campo sensivel chegue aqui por
    engano (ex.: alguem digitou a senha no campo de usuario), ele nao vai
    parar no banco em texto claro.
    """
    if not detalhes:
        return {}

    limpo = {}
    for chave, valor in detalhes.items():
        if str(chave).strip().lower() in _CAMPOS_PROIBIDOS:
            limpo[chave] = "[removido]"
        elif isinstance(valor, dict):
            limpo[chave] = limpar_detalhes(valor)
        elif isinstance(valor, str) and len(valor) > 500:
            limpo[chave] = valor[:500] + "...[truncado]"
        else:
            limpo[chave] = valor
    return limpo


# ==========================================================================
# Validacao de destino de webhook (SSRF)
# ==========================================================================

class DestinoInvalido(Exception):
    """URL de webhook recusada pela politica de saida."""


def validar_url_webhook(url: str) -> str:
    """
    Valida uma URL de webhook antes de qualquer requisicao de saida.

    Bloqueia:
      - esquemas que nao sejam http/https (file://, gopher://, ftp://...)
      - enderecos de loopback, rede privada, link-local e reservados
      - o metadata service das nuvens (169.254.169.254)

    Retorna a URL normalizada ou levanta DestinoInvalido.
    """
    if not url or not str(url).strip():
        raise DestinoInvalido("URL de webhook vazia.")

    url = str(url).strip()
    partes = urlparse(url)

    esquemas_ok = {"https"} if not settings.DEBUG else {"http", "https"}
    if partes.scheme.lower() not in esquemas_ok:
        raise DestinoInvalido(
            f"Esquema '{partes.scheme}' nao permitido. Use https://."
        )

    if not partes.hostname:
        raise DestinoInvalido("URL de webhook sem host.")

    try:
        resolvidos = socket.getaddrinfo(partes.hostname, partes.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as erro:
        raise DestinoInvalido(f"Nao foi possivel resolver o host: {erro}") from erro

    for familia, *_resto, sockaddr in resolvidos:
        endereco = ipaddress.ip_address(sockaddr[0])
        if (
            endereco.is_private
            or endereco.is_loopback
            or endereco.is_link_local
            or endereco.is_reserved
            or endereco.is_multicast
            or endereco.is_unspecified
        ):
            log_seguranca.warning(
                "Webhook recusado: host %s resolve para endereco interno %s",
                partes.hostname,
                endereco,
            )
            raise DestinoInvalido(
                "O webhook aponta para um endereco de rede interna. "
                "Configure um host publico."
            )

    return url


class _SemRedirecionamento(urllib.request.HTTPRedirectHandler):
    """Recusa qualquer redirect: a resposta 3xx vira HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def enviar_sem_redirecionar(requisicao, timeout):
    """
    Envia uma requisicao de saida sem seguir redirect.

    validar_url_webhook() confere o destino antes da chamada, mas o urlopen
    padrao segue redirects: um host publico que responda 302 apontando para
    http://169.254.169.254/ levaria a requisicao para dentro da rede depois de
    validada. Aqui o destino validado e o destino final.
    """
    return urllib.request.build_opener(_SemRedirecionamento).open(requisicao, timeout=timeout)


# ==========================================================================
# Nome de arquivo seguro (path traversal)
# ==========================================================================

_NOME_PERIGOSO = re.compile(r"[^A-Za-z0-9._-]+")

# Nomes reservados do Windows: criar "CON.pdf" ou "LPT1.pdf" causa
# comportamento indefinido no sistema de arquivos.
_RESERVADOS_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{n}" for n in range(1, 10)},
    *{f"LPT{n}" for n in range(1, 10)},
}


def nome_arquivo_seguro(nome_original: str | None, extensao_padrao: str = ".pdf") -> str:
    """
    Converte um nome de arquivo de origem nao confiavel em um nome inerte.

    O nome do anexo de e-mail e do upload e controlado por quem envia. Sem
    esse tratamento, um anexo chamado "../../core/settings.py" escreve fora
    do diretorio de destino e sobrescreve codigo da aplicacao.

    Descarta qualquer componente de diretorio, normaliza acentos, remove
    caracteres fora da lista permitida e forca a extensao esperada.
    """
    bruto = str(nome_original or "").strip()

    # PurePath cobre separadores dos dois sistemas; o replace trata o caso de
    # um nome com "\" chegando em um host Linux, onde "\" nao e separador.
    bruto = bruto.replace("\\", "/").split("/")[-1]

    # NFKD + descarte de combining chars: "faturá.pdf" -> "fatura.pdf"
    bruto = unicodedata.normalize("NFKD", bruto)
    bruto = "".join(ch for ch in bruto if not unicodedata.combining(ch))

    caule = Path(bruto).stem
    caule = _NOME_PERIGOSO.sub("_", caule).strip("._-")

    if not caule or caule.upper() in _RESERVADOS_WINDOWS:
        caule = "arquivo"

    caule = caule[:80]

    # Sufixo aleatorio: impede que dois anexos de mesmo nome se sobrescrevam
    # e que alguem preveja o caminho final do arquivo gravado.
    return f"{caule}_{secrets.token_hex(4)}{extensao_padrao}"


def caminho_dentro_de(base: Path, caminho: Path) -> bool:
    """
    Confirma que `caminho` esta contido em `base` apos resolver symlinks.

    Segunda barreira depois de nome_arquivo_seguro: se algum caminho for
    montado por outro trecho de codigo, ainda assim nao escapa do diretorio.
    """
    try:
        base_resolvida = base.resolve()
        alvo = caminho.resolve()
    except OSError:
        return False
    return base_resolvida == alvo or base_resolvida in alvo.parents


def gravar_arquivo_seguro(base: Path, nome_original: str | None, conteudo: bytes) -> Path:
    """Grava `conteudo` dentro de `base` usando um nome saneado."""
    base.mkdir(parents=True, exist_ok=True)
    destino = base / nome_arquivo_seguro(nome_original)

    if not caminho_dentro_de(base, destino):
        raise DestinoInvalido(f"Caminho de gravacao recusado: {destino}")

    destino.write_bytes(conteudo)
    try:
        destino.chmod(0o600)
    except OSError:
        # Sistemas de arquivos do Windows podem recusar chmod POSIX; a ACL do
        # diretorio continua valendo.
        pass
    return destino


# ==========================================================================
# Integridade e validacao de PDF
# ==========================================================================

ASSINATURA_PDF = b"%PDF-"


def sha256_arquivo(caminho: Path, bloco: int = 1024 * 1024) -> str:
    """
    SHA-256 do arquivo, lido em blocos de 1 MB.

    Streaming em vez de read() inteiro: um PDF de 500 MB nao pode virar 500 MB
    de RAM. Usado para deduplicar reprocessamento e detectar adulteracao.
    """
    digest = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for pedaco in iter(lambda: arquivo.read(bloco), b""):
            digest.update(pedaco)
    return digest.hexdigest()


def sha256_bytes(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


class PdfInvalido(Exception):
    """PDF recusado antes do parsing."""


def pdf_tem_camada_de_texto(caminho: Path) -> bool:
    """
    Diz se o PDF tem texto de verdade ou e apenas imagem digitalizada.

    Serve para transformar "nao consegui ler" em uma instrucao util. Sem isso,
    a fatura escaneada e a fatura de layout desconhecido produzem exatamente
    a mesma mensagem, e nada indica que uma tem solucao trivial (pedir o
    arquivo digital) e a outra exige mexer no extrator.

    Nao e chamada na validacao de entrada: so quando a extracao ja falhou,
    porque abrir o documento para contar caracteres custa caro.
    """
    try:
        import pdfplumber

        with pdfplumber.open(caminho) as pdf:
            for pagina in pdf.pages[:3]:
                if pagina.chars:
                    return True
        return False
    except Exception:
        # Na duvida, assume que tem texto: melhor dar a mensagem generica do
        # que afirmar que e escaneado sem ter certeza.
        return True


def validar_pdf(caminho: Path, tamanho_maximo: int | None = None, paginas_maximas: int | None = None) -> dict:
    """
    Valida um PDF antes de entregar ao parser.

    Ordem deliberada, do mais barato para o mais caro: existencia, tamanho,
    magic bytes e so entao abertura do documento. Assim um arquivo de 500 MB
    ou um .exe renomeado para .pdf e recusado sem custo de parsing.

    Retorna metadados (tamanho, paginas, sha256) ou levanta PdfInvalido.
    """
    tamanho_maximo = tamanho_maximo or getattr(settings, "PDF_TAMANHO_MAXIMO_BYTES", 25 * 1024 * 1024)
    paginas_maximas = paginas_maximas or getattr(settings, "PDF_PAGINAS_MAXIMAS", 60)

    caminho = Path(caminho)

    if not caminho.is_file():
        raise PdfInvalido("Arquivo nao encontrado.")

    tamanho = caminho.stat().st_size
    if tamanho == 0:
        raise PdfInvalido("Arquivo vazio.")
    if tamanho > tamanho_maximo:
        raise PdfInvalido(
            f"Arquivo com {tamanho / 1024 / 1024:.1f} MB excede o limite de "
            f"{tamanho_maximo / 1024 / 1024:.0f} MB."
        )

    # Magic bytes: a extensao do nome nao prova nada, quem envia escolhe o
    # nome. O conteudo real precisa comecar com %PDF-.
    with caminho.open("rb") as arquivo:
        cabecalho = arquivo.read(len(ASSINATURA_PDF))
    if cabecalho != ASSINATURA_PDF:
        raise PdfInvalido(
            "O conteudo do arquivo nao e um PDF (assinatura ausente), "
            "independentemente da extensao."
        )

    paginas = _contar_paginas(caminho)
    if paginas == 0:
        raise PdfInvalido("PDF sem paginas legiveis.")
    if paginas > paginas_maximas:
        raise PdfInvalido(
            f"PDF com {paginas} paginas excede o limite de {paginas_maximas}."
        )

    return {
        "tamanho_bytes": tamanho,
        "paginas": paginas,
        "sha256": sha256_arquivo(caminho),
    }


def _contar_paginas(caminho: Path) -> int:
    """Abre o PDF so o suficiente para contar paginas e detectar protecao."""
    # Import local: pdfminer e pesado e nem todo caminho de codigo precisa.
    from pdfminer.pdfdocument import PDFDocument, PDFPasswordIncorrect
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser, PDFSyntaxError

    try:
        with caminho.open("rb") as arquivo:
            documento = PDFDocument(PDFParser(arquivo))
            return sum(1 for _ in PDFPage.create_pages(documento))
    except PDFPasswordIncorrect as erro:
        raise PdfInvalido("PDF protegido por senha; nao e possivel extrair dados.") from erro
    except PDFSyntaxError as erro:
        raise PdfInvalido(f"PDF corrompido ou malformado: {erro}") from erro
    except Exception as erro:  # pdfminer levanta varias excecoes proprias
        raise PdfInvalido(f"Nao foi possivel ler o PDF: {erro}") from erro
