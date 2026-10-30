"""
robo_cpfl.py
============
Robô que lê o último e-mail de uma caixa Gmail, baixa o PDF de
conta CPFL em anexo, extrai os dados e salva no PostgreSQL.

Variáveis de ambiente necessárias (arquivo .env na raiz do projeto):
    EMAIL_CONTA   — endereço Gmail usado pelo robô
    SENHA_APP     — senha de app do Google (não a senha da conta)
    EMAIL_DESTINO — endereço que recebe o alerta após cada fatura
    DB_HOST       — host do PostgreSQL (padrão: localhost)
    DB_NAME       — nome do banco (padrão: sistema_prestadores)
    DB_USER       — usuário do banco (padrão: postgres)
    DB_PASSWORD   — senha do banco
"""

import imaplib
import email
import logging
import os
import re
import unicodedata
import pdfplumber
import psycopg2
import smtplib
from psycopg2.extras import Json
from email.utils import getaddresses
from email.message import EmailMessage
from datetime import datetime
from pathlib import Path
from shutil import move

from dotenv import load_dotenv

from monitor.seguranca import (
    PdfInvalido,
    pdf_tem_camada_de_texto,
    gravar_arquivo_seguro,
    mascarar_email,
    mascarar_instalacao,
    mascarar_nome,
    sha256_arquivo,
    validar_pdf,
)

# =====================================================================
# LOGGING
# =====================================================================
# Nao usa basicConfig: sob o Django quem manda e a configuracao LOGGING do
# settings, que rotaciona arquivo e separa o canal de seguranca.
log = logging.getLogger("robo")


# =====================================================================
# CONFIGURAÇÕES (lidas do ambiente, nunca do código-fonte)
# =====================================================================
load_dotenv()


def _config_obrigatoria(nome: str) -> str:
    """
    Le uma variavel obrigatoria no momento do uso, nao na importacao.

    Antes era `os.environ["EMAIL_CONTA"]` no topo do modulo. Como
    monitor/views.py importa este arquivo, uma variavel faltando derrubava a
    aplicacao inteira no boot — o painel nao subia por causa de config do robo.
    Agora a falha fica contida na execucao da coleta.
    """
    valor = os.environ.get(nome, "").strip()
    if not valor:
        raise RuntimeError(
            f"Variavel de ambiente {nome} nao configurada. "
            f"Veja .env.example para o formato esperado."
        )
    return valor


DB_HOST     = os.getenv("DB_HOST",     "localhost")
DB_NAME     = os.getenv("DB_NAME",     "sistema_prestadores")
DB_USER     = os.getenv("DB_USER",     "postgres")

BASE_DIR = Path(__file__).resolve().parent

# Diretorio unico e controlado para anexos baixados. Antes o PDF era gravado
# em os.getcwd(), que varia conforme de onde o processo foi iniciado.
PDFS_RECEBIDOS_DIR = Path(os.getenv("PDFS_RECEBIDOS_DIR", BASE_DIR / "pdfs_recebidos"))
PDFS_FALHA_DIR = Path(os.getenv("PDFS_FALHA_DIR", BASE_DIR / "pdfs_falha"))


# =====================================================================
# MÓDULO DE EXTRAÇÃO DE DADOS DO PDF
# =====================================================================

# Palavras do cabeçalho/rodapé da própria CPFL — nunca são nome de cliente
_CABECALHO_CPFL = re.compile(
    r'(?:COMPANHIA|PAULISTA|FORCA|FORÇA|LUZ|ENERGIA|ELETRICA|ELÉTRICA|'
    r'ELETRONICA|ELETRÔNICA|FISCAL|DANF3E|DOCUMENTO|AUXILIAR|NOTA)',
    re.IGNORECASE,
)

# Textos que definitivamente NÃO são nome de cliente
_BLACKLIST_NOME = re.compile(
    r'(?:rua|av\.|avenida|r\.\s|cep|bairro|loteamento|jd\b|jardim|vila|lot\b|'
    r'cpf|cnpj|www\.|http|\.com|\.br|cpfl|nota\s*fiscal|energia\s*el[ée]trica|'
    r'\d{4,}|classificac|convencional|bifasico|bifásico|tensão|instalação)',
    re.IGNORECASE,
)

# Linha composta APENAS de letras maiúsculas, acentuadas ou não, e espaços.
# Exige pelo menos duas palavras (≥ 1 espaço interno) para evitar siglas.
_APENAS_MAIUSCULAS = re.compile(
    r'^[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇÀÜ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇÀÜ\s]{4,}$'
)

_TEM_ESPACO_INTERNO = re.compile(r'\S\s+\S')

# Palavras que, mesmo em caixa-alta, não são nomes de cliente
_PALAVRAS_EXCLUIR_NOME = re.compile(
    r'^(?:COMPANHIA|PAULISTA|FORCA|FORÇA|LUZ|ENERGIA|ELETRICA|ELÉTRICA|'
    r'ELETRONICA|ELETRÔNICA|FISCAL|DANF3E|DOCUMENTO|AUXILIAR|NOTA|'
    r'JARDIM|LOTEAMENTO|JD\s|LOT\s|VILA\s)',
    re.IGNORECASE,
)

# Número da instalação (UC – Unidade Consumidora)
_PADROES_INSTALACAO = [
    r"Nº\s*da\s*Instalação\s*[:\-]?\s*(\d{6,})",
    r"Instalação\s*[:\-]?\s*(\d{6,})",
    r"UC\s*[:\-]?\s*(\d{6,})",
]

# Consumo em kWh
_PADROES_CONSUMO = [
    r"Consumo\s*\(kWh\)\s*(\d{1,6}(?:\.\d{3})*)",
    r"kWh\s*(\d{1,6}(?:\.\d{3})*)\s*kWh",
    r"Energia Elétrica\s*kWh\s*(\d{1,6}(?:\.\d{3})*)",
]

# Data de leitura (ex: 10/04/2026 a 10/05/2026)
_PADROES_LEITURA = [
    r"Leitura\s*anterior\s*(\d{2}/\d{2}/\d{4}).*?Leitura\s*atual\s*(\d{2}/\d{2}/\d{4})",
    r"Período\s*de\s*leitura\s*:\s*(\d{2}/\d{2}/\d{4})\s+a\s+(\d{2}/\d{2}/\d{4})",
]

# Padrões de vencimento (ordem: mais específico → mais genérico)
_PADROES_VENCIMENTO = [
    r"Data\s+de\s+Vencimento\s*\n?\s*(\d{2}/\d{2}/\d{4})",
    r"[A-Z]{3}/\d{4}\s+(\d{2}/\d{2}/\d{4})\s+R\$",
    r"(?:data\s+de\s+)?vencimento\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
    r"VENC[^\d]*(\d{2}/\d{2}/\d{4})",
    r"vence\s+em\s+(\d{2}/\d{2}/\d{4})",
    r"Total\s+a\s+[Pp]agar.*?(\d{2}/\d{2}/\d{4})",
]

# Padrões de valor (ordem: mais específico → mais genérico)
_PADROES_VALOR = [
    r"Total\s+a\s+[Pp]agar\s*\(?R?\$?\)?\s*R?\$?\s*(\d{1,3}(?:\.\d{3})*,\d{2})",
    r"Total\s+a\s+[Pp]agar\s*[\n\r]+\s*(\d{1,3}(?:\.\d{3})*,\d{2})",
    r"TOTAL\s+A\s+PAGAR[^\d]*(\d{1,3}(?:\.\d{3})*,\d{2})",
    r"Total\s+Consolidado\s+(\d{1,3}(?:\.\d{3})*,\d{2})",
    r"[A-Z]{3}/\d{4}\s+\d{2}/\d{2}/\d{4}\s+R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})",
    r"R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})",
]


# ── Helpers de extração ───────────────────────────────────────────────────────

def _extrair_nome(linhas_layout: list[str], texto_total: str) -> str:
    """
    Estratégia 1 — nome inline com CPF visível (formato antigo).
    Estratégia 2 — primeira linha só com maiúsculas após o cabeçalho (formato DANF3E).
    """
    # Estratégia 1
    m = re.search(
        r"^(.+?)\s{3,}CPF\s*:",
        texto_total,
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        cand = m.group(1).strip()
        if (
            len(cand) > 5
            and not _BLACKLIST_NOME.search(cand)
            and not _CABECALHO_CPFL.search(cand)
        ):
            return cand

    # Estratégia 2: busca nas linhas 6–20 do layout
    for linha in linhas_layout[6:20]:
        stripped = linha.strip()
        if (
            _APENAS_MAIUSCULAS.match(stripped)
            and _TEM_ESPACO_INTERNO.search(stripped)
            and not _PALAVRAS_EXCLUIR_NOME.match(stripped)
            and not _BLACKLIST_NOME.search(stripped)
            and len(stripped) > 8
        ):
            return stripped

    return "Não encontrado"


def _normalizar_texto(texto: str) -> str:
    texto = texto.replace("\xa0", " ")
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(ch for ch in texto if not unicodedata.combining(ch))


def _numero_pt_para_float(valor: str) -> float:
    valor = valor.strip()
    if "," in valor:
        return float(valor.replace(".", "").replace(",", "."))
    if valor.count(".") >= 2:
        partes = valor.split(".")
        if len(partes[-1]) != 3:
            return float("".join(partes[:-1]) + "." + partes[-1])
        return float(valor.replace(".", ""))
    if "." in valor and len(valor.rsplit(".", 1)[-1]) == 3:
        return float(valor.replace(".", ""))
    return float(valor)


def _formatar_consumo(valor: float) -> str:
    if valor.is_integer():
        return str(int(valor))
    return f"{valor:.2f}".replace(".", ",")


_ELEKTRO_MAIN_MAP = {
    "(cid:0)": "3",
    "(cid:1)": "5",
    "(cid:2)": "6",
    "(cid:3)": "0",
    "(cid:4)": "1",
    "(cid:5)": "B",
    "(cid:6)": "R",
    "(cid:7)": "K",
    "(cid:8)": " ",
    "(cid:9)": "A",
    "(cid:10)": "M",
    "(cid:11)": "I",
    "(cid:12)": "E",
    "(cid:13)": "N",
    "(cid:14)": "T",
    "(cid:15)": "L",
    "(cid:16)": "-",
    "(cid:17)": "O",
    "(cid:18)": "C",
    "(cid:19)": "S",
    "(cid:20)": ".",
    "(cid:21)": "A",
    "(cid:22)": "V",
    "(cid:23)": " ",
    "(cid:24)": "5",
    "(cid:25)": "-",
    "(cid:26)": "B",
    "(cid:27)": "T",
    "(cid:28)": "O",
    "(cid:29)": "I",
    "(cid:30)": ",",
    "(cid:31)": "0",
    "E": "/",
    "F": "2",
    "G": "4",
    "H": "9",
    "I": "7",
    "J": "8",
    "K": "$",
    "L": ",",
    "4": "a",
    "9": "i",
    "<": "o",
}

_ELEKTRO_SMALL_NUMBER_MAP = {
    " ": "1",
    "1": "2",
    "2": "8",
    "(": "4",
    ")": "7",
    "*": "6",
    "-": "/",
    "D": "9",
    "’": "3",
    "(cid:23)": " ",
    "(cid:24)": "5",
    "(cid:31)": "0",
}

_ELEKTRO_HISTORY_LABEL_MAP = {
    **_ELEKTRO_SMALL_NUMBER_MAP,
    "!": "R",
    "#": "L",
    "$": "S",
    "&": "E",
    "+": "N",
    ",": "J",
    ".": "F",
    "M": "U",
    "O": "M",
    "P": "D",
    "Q": "Z",
    "R": "G",
    "(cid:21)": "A",
    "(cid:22)": "V",
    "(cid:26)": "B",
    "(cid:27)": "T",
    "(cid:28)": "O",
    "(cid:29)": "I",
}

_ELEKTRO_MEDIDOR_MAP = {
    **_ELEKTRO_SMALL_NUMBER_MAP,
    "(cid:27)": "T",
}


def _texto_char(ch: dict) -> str:
    return ch.get("text", "")


def _decode_chars(chars: list[dict], mapa: dict[str, str]) -> str:
    return "".join(
        mapa.get(_texto_char(ch), _texto_char(ch))
        for ch in sorted(chars, key=lambda item: float(item.get("x0", 0)))
    )


def _chars_intervalo(pagina, x_min: float, x_max: float, y_min: float, y_max: float) -> list[dict]:
    return [
        ch
        for ch in getattr(pagina, "chars", [])
        if x_min <= float(ch.get("x0", 0)) <= x_max
        and y_min <= float(ch.get("top", 0)) <= y_max
    ]


def _decode_intervalo(pagina, x_min: float, x_max: float, y_min: float, y_max: float, mapa: dict[str, str]) -> str:
    texto = _decode_chars(_chars_intervalo(pagina, x_min, x_max, y_min, y_max), mapa)
    return re.sub(r"\s+", " ", texto).strip()


def _tokens_intervalo(pagina, x_min: float, x_max: float, y_min: float, y_max: float) -> list[str]:
    tokens = []
    for ch in sorted(_chars_intervalo(pagina, x_min, x_max, y_min, y_max), key=lambda item: float(item.get("x0", 0))):
        texto = _texto_char(ch)
        if texto and not texto.isspace():
            tokens.append(texto)
    return tokens


def _apenas_digitos(texto: str) -> str:
    return re.sub(r"[^0-9]", "", texto or "")


def _normalizar_moeda_extraida(texto: str) -> str:
    texto = (texto or "").replace("R$", "").strip()
    m = re.search(r"\d{1,3}(?:\.\d{3})*,\d{2}", texto)
    return m.group(0) if m else texto


def _digitos_moeda(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")


def _grupos_por_x(chars: list[dict], distancia: float = 7.0) -> list[list[dict]]:
    grupos = []
    grupo_atual = []
    ultimo_x = None

    for ch in sorted(chars, key=lambda item: float(item.get("x0", 0))):
        x0 = float(ch.get("x0", 0))
        if ultimo_x is None or x0 - ultimo_x <= distancia:
            grupo_atual.append(ch)
        else:
            if grupo_atual:
                grupos.append(grupo_atual)
            grupo_atual = [ch]
        ultimo_x = float(ch.get("x1", x0))

    if grupo_atual:
        grupos.append(grupo_atual)
    return grupos


def _grupos_elektro_por_linha(
    pagina,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    distancia: float = 8.0,
    tolerancia_y: float = 1.5,
) -> list[list[dict]]:
    linhas: list[list[dict]] = []
    for ch in sorted(_chars_intervalo(pagina, x_min, x_max, y_min, y_max), key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0)))):
        top = float(ch.get("top", 0))
        for linha in linhas:
            if abs(float(linha[0].get("top", 0)) - top) <= tolerancia_y:
                linha.append(ch)
                break
        else:
            linhas.append([ch])

    grupos: list[list[dict]] = []
    for linha in linhas:
        grupos.extend(_grupos_por_x(linha, distancia=distancia))
    return grupos


def _completar_mapa_sem_conflito(mapa: dict[str, str], tokens: list[str], texto: str) -> dict[str, str] | None:
    if len(tokens) != len(texto):
        return None

    novo_mapa = dict(mapa)
    reverso = {valor: chave for chave, valor in novo_mapa.items()}
    for token, caractere in zip(tokens, texto):
        if token in novo_mapa and novo_mapa[token] != caractere:
            return None
        if caractere in reverso and reverso[caractere] != token:
            return None
        novo_mapa[token] = caractere
        reverso[caractere] = token
    return novo_mapa


def _inferir_mapa_pequeno_elektro(pagina, valor: str, vencimento: str) -> dict[str, str]:
    mapa: dict[str, str] = {}

    # A Elektro usa fontes embarcadas que trocam os codigos dos numeros em cada conta.
    # O valor total aparece tambem na tabela pequena, entao ele serve como chave da pagina.
    for grupo in _grupos_elektro_por_linha(pagina, 360, 565, 150, 225):
        tokens = [_texto_char(ch) for ch in grupo]
        candidato = _completar_mapa_sem_conflito(mapa, tokens, valor)
        if candidato and "." in candidato.values() and "," in candidato.values():
            mapa = candidato

    reverso = {valor_mapeado: token for token, valor_mapeado in mapa.items()}
    token_ponto = reverso.get(".")
    token_virgula = reverso.get(",")
    for grupo in _grupos_elektro_por_linha(pagina, 370, 405, 160, 190):
        tokens = [_texto_char(ch) for ch in grupo]
        if (
            len(tokens) >= 8
            and token_ponto
            and token_virgula
            and tokens[1] == token_ponto
            and tokens[5] == token_virgula
            and tokens[6] == tokens[7]
        ):
            mapa.setdefault(tokens[6], "0")

    ano = re.search(r"/(\d{4})$", vencimento or "")
    if ano:
        for intervalo in ((188, 232, 191, 205), (188, 232, 211, 224)):
            tokens = [_texto_char(ch) for ch in sorted(_chars_intervalo(pagina, *intervalo), key=lambda item: float(item.get("x0", 0)))]
            referencia = "??/??/" + ano.group(1)
            for indice, caractere in enumerate(referencia):
                if caractere == "?" or indice >= len(tokens):
                    continue
                mapa.setdefault(tokens[indice], caractere)

    if mapa.get(" ") == "1":
        for token, valor_mapeado in _ELEKTRO_SMALL_NUMBER_MAP.items():
            mapa.setdefault(token, valor_mapeado)
    elif mapa.get(" ") == "2" and mapa.get("N") == ".":
        for token, valor_mapeado in {
            "!": "0",
            "(cid:24)": "1",
            " ": "2",
            "’": "3",
            "C": "4",
            "(": "5",
            "1": "6",
            "0": "7",
            "/": "8",
            ")": "9",
            "N": ".",
            "(cid:31)": ",",
            "+": "/",
        }.items():
            mapa.setdefault(token, valor_mapeado)
    elif mapa.get("(cid:25)") == "1" and mapa.get("‘") == ".":
        for token, valor_mapeado in {
            "-": "0",
            "(cid:25)": "1",
            "#": "2",
            "+": "3",
            "\"": "4",
            ",": "5",
            "5": "6",
            "!": "7",
            "4": "8",
            ".": "9",
            "‘": ".",
            " ": ",",
            "0": "/",
        }.items():
            mapa.setdefault(token, valor_mapeado)

    return mapa or _ELEKTRO_SMALL_NUMBER_MAP


def _valor_historico_elektro(texto: str) -> str:
    numero = _apenas_digitos(texto)
    if not numero:
        return ""
    return str(int(numero))


def _formatar_valor_inteiro_pt(valor: int) -> str:
    return f"{valor:,}".replace(",", ".")


def _data_pt_valida(texto: str) -> bool:
    try:
        datetime.strptime(texto, "%d/%m/%Y")
        return True
    except (TypeError, ValueError):
        return False


def _meses_historico_elektro(data_referencia: str) -> list[str]:
    if not _data_pt_valida(data_referencia):
        return []

    data = datetime.strptime(data_referencia, "%d/%m/%Y")
    nomes = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
    meses = []
    mes = data.month
    ano = data.year
    for _ in range(13):
        meses.append(f"{nomes[mes - 1]} {str(ano)[-2:]}")
        mes -= 1
        if mes == 0:
            mes = 12
            ano -= 1
    return meses


def _listar_instalacoes_cadastradas() -> list[str]:
    conexao = None
    cursor = None
    try:
        conexao = _conectar_banco()
        cursor = conexao.cursor()
        cursor.execute(
            """
            SELECT instalacao
              FROM monitor_unidadeconsumidora
             WHERE ativa = true;
            """
        )
        return [str(linha[0]) for linha in cursor.fetchall() if linha and linha[0]]
    except Exception as e:
        log.debug("Nao foi possivel listar instalacoes cadastradas para Elektro: %s", e)
        return []
    finally:
        if cursor:
            cursor.close()
        if conexao:
            conexao.close()


def _mapa_elektro_por_instalacao(pagina, instalacoes: list[str]) -> dict[str, str]:
    tokens = _tokens_intervalo(pagina, 390, 510, 25, 55)
    if not tokens:
        return {}

    mapas = []
    for instalacao in instalacoes:
        codigo = _apenas_digitos(instalacao)
        if len(codigo) != len(tokens):
            continue

        mapa = {}
        reverso = {}
        valido = True
        for token, digito in zip(tokens, codigo):
            if token in mapa and mapa[token] != digito:
                valido = False
                break
            if digito in reverso and reverso[digito] != token:
                valido = False
                break
            mapa[token] = digito
            reverso[digito] = token

        if valido:
            mapas.append(mapa)

    return mapas[0] if len(mapas) == 1 else {}


def _completar_mapa_por_texto(
    mapa: dict[str, str],
    tokens: list[str],
    texto_referencia: str,
) -> dict[str, str]:
    if not texto_referencia or len(tokens) != len(texto_referencia):
        return mapa

    novo_mapa = dict(mapa)
    reverso = {valor: chave for chave, valor in novo_mapa.items()}
    for token, caractere in zip(tokens, texto_referencia):
        if token in novo_mapa and novo_mapa[token] != caractere:
            return mapa
        if caractere in reverso and reverso[caractere] != token:
            return mapa
        novo_mapa[token] = caractere
        reverso[caractere] = token
    return novo_mapa


def _extrair_valor_elektro(pagina, mapa: dict[str, str]) -> str:
    digitos = []
    for token in _tokens_intervalo(pagina, 495, 558, 108, 124):
        valor = mapa.get(token, token if token.isdigit() else "")
        if valor.isdigit():
            digitos.append(valor)

    numero = "".join(digitos)
    if len(numero) < 3:
        return ""

    inteiro = numero[:-2] or "0"
    centavos = numero[-2:]
    inteiro_formatado = f"{int(inteiro):,}".replace(",", ".")
    return f"{inteiro_formatado},{centavos}"


def _extrair_historico_elektro(
    pagina,
    mapa_pequeno: dict[str, str] | None = None,
    data_referencia: str = "",
) -> list[dict]:
    mapa_pequeno = mapa_pequeno or _ELEKTRO_SMALL_NUMBER_MAP
    valores = [
        _valor_historico_elektro(_decode_chars(grupo, mapa_pequeno))
        for grupo in _grupos_por_x(_chars_intervalo(pagina, 38, 285, 314, 322), distancia=5)
    ]
    texto_meses = _decode_chars(
        _chars_intervalo(pagina, 38, 285, 321, 330),
        {**_ELEKTRO_HISTORY_LABEL_MAP, **mapa_pequeno},
    ).replace(" ", "")
    meses = re.findall(r"(?:JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)/\d{2}", texto_meses)
    meses_fallback = _meses_historico_elektro(data_referencia)

    linhas = []
    for indice, valor in enumerate(valores):
        if not valor:
            continue
        mes_label = ""
        if indice < len(meses):
            mes_raw = meses[indice].replace("/", " ")
            m = re.search(r"(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)\s*(\d{2})", mes_raw)
            if m:
                mes_label = f"{m.group(1)} {m.group(2)}"
        if not mes_label and indice < len(meses_fallback):
            mes_label = meses_fallback[indice]
        if not mes_label:
            continue
        linhas.append({
            "mes": mes_label,
            "valor": valor,
            "dias": "-",
        })

    if not linhas:
        return []
    return [{
        "titulo": "Consumo total",
        "unidade": "kWh",
        "tipo": "energia",
        "linhas": linhas,
    }]


def _extrair_nome_elektro(pagina, mapa_principal: dict[str, str], nome_referencia: str = "") -> str:
    if nome_referencia:
        chars_nome = sorted(_chars_intervalo(pagina, 38, 230, 55, 68), key=lambda item: float(item.get("x0", 0)))
        mapa_nome = _completar_mapa_sem_conflito({}, [_texto_char(ch) for ch in chars_nome], nome_referencia)
        if mapa_nome:
            nome = _decode_chars(chars_nome, mapa_nome)
            if nome:
                return nome

    return _decode_intervalo(pagina, 38, 230, 55, 68, mapa_principal) or "Cliente Elektro"


def _extrair_medidor_elektro(pagina, mapa_pequeno: dict[str, str]) -> str:
    chars = sorted(_chars_intervalo(pagina, 38, 82, 145, 160), key=lambda item: float(item.get("x0", 0)))
    tokens = [_texto_char(ch) for ch in chars]
    if not tokens:
        return ""

    mapa = {**_ELEKTRO_MEDIDOR_MAP, **mapa_pequeno}
    if len(tokens) >= 2 and tokens[0] == "(cid:21)" and tokens[1] == "#":
        mapa.update({"(cid:21)": "A", "#": "R"})
    elif len(tokens) >= 2 and tokens[1] in {"(cid:27)", "(cid:28)"}:
        mapa[tokens[1]] = "T"

    return re.sub(r"\s+", "", _decode_chars(chars, mapa))


def _extrair_fatura_elektro_pagina(
    pagina,
    mapa_numeros: dict[str, str] | None = None,
    nome_referencia: str = "",
) -> dict | None:
    mapa_principal = {**_ELEKTRO_MAIN_MAP, **(mapa_numeros or {})}
    instalacao = _apenas_digitos(_decode_intervalo(pagina, 390, 510, 25, 55, mapa_principal))
    vencimento = _decode_intervalo(pagina, 390, 465, 108, 124, mapa_principal)
    valor = _normalizar_moeda_extraida(_decode_intervalo(pagina, 495, 558, 108, 124, mapa_principal))
    valor_por_digitos = _extrair_valor_elektro(pagina, mapa_numeros or {})
    if (
        re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}", valor_por_digitos or "")
        and len(_digitos_moeda(valor_por_digitos)) > len(_digitos_moeda(valor))
    ) or not re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}", valor or ""):
        valor = valor_por_digitos

    if not re.fullmatch(r"\d{6,}", instalacao or ""):
        return None
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", vencimento or ""):
        return None
    if not re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}", valor or ""):
        return None

    mapa_pequeno = _inferir_mapa_pequeno_elektro(pagina, valor, vencimento)
    nome = _extrair_nome_elektro(pagina, mapa_principal, nome_referencia)
    medidor = _extrair_medidor_elektro(pagina, mapa_pequeno)
    leitura_anterior = _apenas_digitos(_decode_intervalo(pagina, 132, 162, 184, 195, mapa_pequeno))
    leitura_atual = _apenas_digitos(_decode_intervalo(pagina, 164, 195, 184, 195, mapa_pequeno))
    data_anterior = _decode_intervalo(pagina, 188, 232, 191, 205, mapa_pequeno)
    data_atual = _decode_intervalo(pagina, 188, 232, 211, 224, mapa_pequeno)
    if not _data_pt_valida(data_anterior):
        data_anterior = ""
    if not _data_pt_valida(data_atual):
        data_atual = ""
    historico = _extrair_historico_elektro(pagina, mapa_pequeno, data_atual)

    consumo = ""
    if historico and historico[0]["linhas"]:
        consumo = historico[0]["linhas"][0]["valor"]
    elif leitura_anterior and leitura_atual:
        try:
            diferenca = int(leitura_atual) - int(leitura_anterior)
            if diferenca > 0:
                consumo = str(diferenca)
        except ValueError:
            consumo = ""

    periodo = f"{data_anterior} a {data_atual}" if data_anterior and data_atual else "Nao encontrado"
    consumo_exibicao = _formatar_valor_inteiro_pt(int(consumo)) if consumo.isdigit() else (consumo or "Nao encontrado")

    if periodo == "Nao encontrado" or consumo_exibicao == "Nao encontrado":
        return None

    leituras = []
    if consumo and leitura_anterior and leitura_atual:
        leituras.append({
            "medidor": medidor or instalacao,
            "grandeza": "Energia Ativa - kWh",
            "posto": "Unico",
            "leitura_anterior": leitura_anterior,
            "leitura_atual": leitura_atual,
            "constante": "1",
            "consumo": consumo_exibicao,
            "tipo": "energia",
        })

    return {
        "nome": nome,
        "vencimento": vencimento,
        "valor": valor,
        "instalacao": instalacao,
        "consumo_kwh": consumo_exibicao,
        "data_leitura": periodo,
        "leituras_medidor": leituras,
        "historico_medidor": historico,
    }


def _extrair_faturas_elektro_pdf(pdf) -> list[dict]:
    if not pdf.pages:
        return []

    primeira = _extrair_fatura_elektro_pagina(pdf.pages[0])
    if not primeira:
        return []

    instalacoes = _listar_instalacoes_cadastradas()
    vencimento_referencia = primeira.get("vencimento", "")
    nome_referencia = primeira.get("nome", "")
    faturas = []
    chaves = set()
    for pagina in pdf.pages:
        mapa_pagina = _mapa_elektro_por_instalacao(pagina, instalacoes)
        if mapa_pagina:
            mapa_pagina = _completar_mapa_por_texto(
                mapa_pagina,
                _tokens_intervalo(pagina, 390, 465, 108, 124),
                vencimento_referencia,
            )
            dados = _extrair_fatura_elektro_pagina(pagina, mapa_pagina, nome_referencia)
        elif pagina.page_number == 1:
            dados = _extrair_fatura_elektro_pagina(pagina)
        else:
            dados = None
        if not dados:
            continue

        chave = (dados["instalacao"], dados["vencimento"], dados["valor"])
        if chave in chaves:
            continue

        chaves.add(chave)
        faturas.append(dados)

    return faturas


def _palavras_pagina(pagina) -> list[dict]:
    try:
        return pagina.extract_words(x_tolerance=1, y_tolerance=2) or []
    except TypeError:
        return pagina.extract_words() or []


def _linhas_por_y(
    palavras: list[dict],
    top_min: float,
    top_max: float,
    x_min: float = 0,
    x_max: float = 999,
    tolerancia: float = 2.0,
) -> list[list[dict]]:
    linhas: list[dict] = []
    filtradas = [
        palavra
        for palavra in palavras
        if top_min <= float(palavra.get("top", 0)) <= top_max
        and x_min <= float(palavra.get("x0", 0)) <= x_max
    ]

    for palavra in sorted(filtradas, key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0)))):
        top = float(palavra.get("top", 0))
        for linha in linhas:
            if abs(linha["top"] - top) <= tolerancia:
                linha["palavras"].append(palavra)
                linha["top"] = (linha["top"] + top) / 2
                break
        else:
            linhas.append({"top": top, "palavras": [palavra]})

    return [
        sorted(linha["palavras"], key=lambda item: float(item.get("x0", 0)))
        for linha in sorted(linhas, key=lambda item: item["top"])
    ]


def _texto_intervalo(linha: list[dict], x_min: float, x_max: float) -> str:
    partes = []
    for palavra in linha:
        x0 = float(palavra.get("x0", 0))
        x1 = float(palavra.get("x1", x0))
        centro = (x0 + x1) / 2
        if x_min <= centro <= x_max:
            partes.append(palavra.get("text", ""))
    return re.sub(r"\s+", " ", " ".join(partes)).strip()


def _normalizar_grandeza(texto: str) -> str:
    normalizado = _normalizar_texto(texto).lower()
    normalizado = re.sub(r"[^a-z0-9]+", "", normalizado)
    if "energiaativa" in normalizado:
        return "Energia Ativa - kWh"
    if "demandaativa" in normalizado or normalizado.startswith("demanda"):
        return "Demanda Ativa - kW"
    if "energiareativa" in normalizado or "reativa" in normalizado:
        return "Energia Reativa - kVA"
    return ""


def _normalizar_posto(texto: str) -> str:
    normalizado = _normalizar_texto(texto).lower()
    if "fora" in normalizado:
        return "Fora Ponta"
    if "ponta" in normalizado:
        return "Ponta"
    return texto.strip() or "-"


def _normalizar_numero_tabela(texto: str) -> str:
    candidatos = re.findall(r"\d+(?:[.,]\d{1,5})*", texto or "")
    return candidatos[-1] if candidatos else "-"


def _extrair_leituras_medidor_pdf(pdf) -> list[dict]:
    leituras = []
    vistos = set()

    for pagina in pdf.pages:
        palavras = _palavras_pagina(pagina)
        for linha in _linhas_por_y(palavras, 520, min(float(pagina.height), 760), 35, 285, 1.6):
            medidor = _normalizar_numero_tabela(_texto_intervalo(linha, 40, 75))
            if not re.fullmatch(r"\d{6,}", medidor):
                continue

            grandeza = _normalizar_grandeza(_texto_intervalo(linha, 76, 123))
            if not grandeza:
                continue

            posto = _normalizar_posto(_texto_intervalo(linha, 124, 154))
            leitura_anterior = _normalizar_numero_tabela(_texto_intervalo(linha, 158, 183))
            leitura_atual = _normalizar_numero_tabela(_texto_intervalo(linha, 188, 213))
            constante = _normalizar_numero_tabela(_texto_intervalo(linha, 214, 239))
            consumo = _normalizar_numero_tabela(_texto_intervalo(linha, 244, 267))
            chave = (medidor, grandeza, posto, leitura_anterior, leitura_atual, constante, consumo)
            if chave in vistos:
                continue
            vistos.add(chave)
            leituras.append({
                "medidor": medidor,
                "grandeza": grandeza,
                "posto": posto,
                "leitura_anterior": leitura_anterior,
                "leitura_atual": leitura_atual,
                "constante": constante,
                "consumo": consumo,
                "tipo": "energia" if "kWh" in grandeza else "demanda",
            })

    return leituras[:12]


def _normalizar_mes_historico(texto: str) -> str:
    normalizado = _normalizar_texto(texto).upper()
    normalizado = re.sub(r"[^A-Z0-9]", "", normalizado)
    normalizado = normalizado.replace("MAL", "MAI").replace("MA1", "MAI")
    normalizado = normalizado.replace("AG0", "AGO").replace("0UT", "OUT")
    meses = "JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ"
    m = re.search(rf"({meses})(\d{{2}})", normalizado)
    if not m:
        return ""
    return f"{m.group(1)} {m.group(2)}"


def _normalizar_valor_historico(texto: str) -> str:
    texto = texto or ""
    compacto = re.sub(r"[^0-9,.]", "", texto)
    if re.fullmatch(r"\d+(?:[.,]\d+)?", compacto):
        if "," in compacto:
            inteiro, decimal = compacto.split(",", 1)
            return f"{inteiro},{decimal[:2].ljust(2, '0')}"
        return f"{compacto},00"

    candidatos = re.findall(r"\d{1,6}(?:[.,]\d{1,3})*(?:,\d{1,2})?", texto or "")
    if not candidatos:
        return ""
    valor = candidatos[0]
    if "," not in valor and "." not in valor:
        return f"{valor},00"
    return valor


def _normalizar_dias_historico(texto: str):
    compacto = re.sub(r"[^0-9]", "", texto or "")
    if 1 <= len(compacto) <= 2:
        return int(compacto)
    m = re.search(r"\b(\d{1,2})\b", texto or "")
    return int(m.group(1)) if m else "-"


def _pagina_tem_quadro_historico(palavras: list[dict]) -> bool:
    for linha in _linhas_por_y(palavras, 160, 280, 90, 490, 2.8):
        texto = _normalizar_texto(" ".join(palavra.get("text", "") for palavra in linha)).upper()
        if "CONSUMO" in texto and "PONTA" in texto and "DIAS" in texto and "FAT" in texto:
            return True
    return False


def _extrair_historico_medidor_pdf(pdf) -> list[dict]:
    configuracoes = [
        {
            "titulo": "Consumo Ponta",
            "unidade": "kWh",
            "tipo": "energia",
            "mes": (108, 134),
            "valor": (158, 185),
            "dias": (195, 212),
        },
        {
            "titulo": "Consumo Fora de Ponta",
            "unidade": "kWh",
            "tipo": "energia",
            "mes": (238, 265),
            "valor": (287, 315),
            "dias": (326, 342),
        },
        {
            "titulo": "Demanda",
            "unidade": "kW",
            "tipo": "demanda",
            "mes": (369, 396),
            "valor": (424, 448),
            "dias": (458, 474),
        },
    ]
    historico = [
        {"titulo": cfg["titulo"], "unidade": cfg["unidade"], "tipo": cfg["tipo"], "linhas": []}
        for cfg in configuracoes
    ]
    vistos = set()

    for pagina in pdf.pages:
        palavras = _palavras_pagina(pagina)
        if not _pagina_tem_quadro_historico(palavras):
            continue

        for linha in _linhas_por_y(palavras, 190, min(float(pagina.height), 430), 90, 490, 1.3):
            for indice, cfg in enumerate(configuracoes):
                mes = _normalizar_mes_historico(_texto_intervalo(linha, *cfg["mes"]))
                valor = _normalizar_valor_historico(_texto_intervalo(linha, *cfg["valor"]))
                dias = _normalizar_dias_historico(_texto_intervalo(linha, *cfg["dias"]))
                chave = (cfg["titulo"], mes)
                if not mes or not valor or chave in vistos:
                    continue
                vistos.add(chave)
                historico[indice]["linhas"].append({
                    "mes": mes,
                    "valor": valor,
                    "dias": dias,
                })

    return [grupo for grupo in historico if grupo["linhas"]]


def _extrair_instalacao(texto_total: str) -> str:
    texto_normalizado = _normalizar_texto(texto_total)
    for padrao in [
        r"N(?:o|umero)?\s*da\s*Instalacao\s*[:\-]?\s*(\d{6,})",
        r"Instalacao\s*[:\-]?\s*(\d{6,})",
        r"UC\s*[:\-]?\s*(\d{6,})",
    ]:
        m = re.search(padrao, texto_normalizado, re.IGNORECASE)
        if m:
            return m.group(1)

    m = re.search(
        r"^\s*(\d{6,})\s+\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}\s+\d{1,3}\s*$",
        texto_normalizado,
        re.MULTILINE,
    )
    if m:
        return m.group(1)

    m = re.search(r"\b(\d{6,})\s+INSTALACAO\s+[A-Z]{3}/\d{4}\b", texto_normalizado, re.IGNORECASE)
    if m:
        return m.group(1)

    for padrao in _PADROES_INSTALACAO:
        m = re.search(padrao, texto_total, re.IGNORECASE)
        if m:
            return m.group(1)
    return "Não encontrado"


def _extrair_consumo(texto_total: str) -> str:
    texto_normalizado = _normalizar_texto(texto_total)
    leituras_ativas = {}

    padrao_tusd_cpfl = re.compile(
        r"Consumo\s+(Ponta|Fora\s+Ponta)\s*\[?KWh\]?\s*-\s*TUSD\s*"
        r"[A-Z]{3}/?\d{2}\s+kWh\s+(\d{1,6}(?:[.,]\d{1,4})*)",
        re.IGNORECASE,
    )
    for tarifa, consumo in padrao_tusd_cpfl.findall(texto_normalizado):
        chave = re.sub(r"\s+", " ", tarifa.strip().lower())
        leituras_ativas[chave] = _numero_pt_para_float(consumo)

    if "ponta" in leituras_ativas and "fora ponta" in leituras_ativas:
        return _formatar_consumo(leituras_ativas["ponta"] + leituras_ativas["fora ponta"])

    padrao_leitura_ativa = re.compile(
        r"Energia\s+Ativa\s*-\s*kWh\s+(Ponta|Fora\s+Ponta)\s+"
        r"\d+\s+\d+\s+[\d.,]+\s+(\d{1,6}(?:\.\d{3})*(?:,\d+)?)",
        re.IGNORECASE,
    )

    for tarifa, consumo in padrao_leitura_ativa.findall(texto_normalizado):
        chave = re.sub(r"\s+", " ", tarifa.strip().lower())
        leituras_ativas[chave] = _numero_pt_para_float(consumo)

    if "ponta" in leituras_ativas and "fora ponta" in leituras_ativas:
        return _formatar_consumo(leituras_ativas["ponta"] + leituras_ativas["fora ponta"])

    m = re.search(
        r"Consumo\s+Uso\s+Sistema\s+\[?KWh\]?\s*-\s*TUSD\s+[A-Z]{3}/\d{2}\s+(\d{1,6}(?:\.\d{3})*,\d+)\s*kWh",
        texto_normalizado,
        re.IGNORECASE,
    )
    if m:
        return _formatar_consumo(_numero_pt_para_float(m.group(1)))

    m = re.search(
        r"Consumo\s+Uso\s+Sistema\s+\[?KWh\]?\s*-\s*TUSD\s+[A-Z]{3}/\d{2}\s+kWh\s+(\d{1,6}(?:\.\d{3})*,\d+)",
        texto_normalizado,
        re.IGNORECASE,
    )
    if m:
        return _formatar_consumo(_numero_pt_para_float(m.group(1)))

    m = re.search(
        r"Consumo\s+kWh\b.*?\bAtiva\s+\d+\s+\d+\s+[\d.,]+\s+(\d{1,6}(?:\.\d{3})*(?:,\d+)?)\s+\d{2}/\d{2}/\d{4}",
        texto_normalizado,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return _formatar_consumo(_numero_pt_para_float(m.group(1)))

    for padrao in _PADROES_CONSUMO:
        m = re.search(padrao, texto_total, re.IGNORECASE)
        if m:
            return m.group(1).replace(".", "")  # remove separador de milhar
    return "Não encontrado"


def _extrair_leitura(texto_total: str) -> str:
    texto_normalizado = _normalizar_texto(texto_total)
    m = re.search(
        r"\b\d{6,}\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+\d{1,3}\b",
        texto_normalizado,
    )
    if m:
        return f"{m.group(2)} a {m.group(1)}"

    m = re.search(
        r"Consumo\s+kWh\b.*?(\d{2}/\d{2}/\d{4})\s*(\d{2}/\d{2}/\d{4}).*?\bAtiva\b",
        texto_normalizado,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return f"{m.group(2)} a {m.group(1)}"

    for padrao in _PADROES_LEITURA:
        m = re.search(padrao, texto_total, re.IGNORECASE | re.DOTALL)
        if m:
            return f"{m.group(1)} a {m.group(2)}"
    return "Não encontrado"


def _extrair_vencimento(texto_total: str) -> str:
    for padrao in _PADROES_VENCIMENTO:
        m = re.search(padrao, texto_total, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1)
    return "Não encontrado"


def _extrair_valor(texto_total: str) -> str:
    for padrao in _PADROES_VALOR:
        m = re.search(padrao, texto_total, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
    return "Não encontrado"


def extrair_dados_cpfl(caminho_pdf: str | Path) -> dict:
    """
    Abre o PDF e extrai os campos relevantes da fatura CPFL.
    Lê no máximo as 2 primeiras páginas — os dados sempre aparecem nelas.
    """
    resultado = {
        "nome":        "Não encontrado",
        "vencimento":  "Não encontrado",
        "valor":       "Não encontrado",
        "instalacao":  "Não encontrado",
        "consumo_kwh": "Não encontrado",
        "data_leitura":"Não encontrado",
    }

    resultado["leituras_medidor"] = []
    resultado["historico_medidor"] = []

    with pdfplumber.open(caminho_pdf) as pdf:
        faturas_elektro = _extrair_faturas_elektro_pdf(pdf)
        if faturas_elektro:
            return faturas_elektro[0]

        campos_principais = ["nome", "vencimento", "valor", "instalacao", "consumo_kwh", "data_leitura"]
        for pagina in pdf.pages[: min(2, len(pdf.pages))]:
            texto_layout = pagina.extract_text(layout=True)  or ""
            texto_linear = pagina.extract_text(layout=False) or ""
            texto_palavras = " ".join(
                palavra.get("text", "")
                for palavra in (pagina.extract_words() or [])
            )
            linhas_layout = texto_layout.split("\n")
            # Combina os dois modos para maximizar chances de match
            texto_total = "\n".join([texto_layout, texto_linear, texto_palavras])

            extratores = {
                "nome":         lambda: _extrair_nome(linhas_layout, texto_total),
                "instalacao":   lambda: _extrair_instalacao(texto_total),
                "consumo_kwh":  lambda: _extrair_consumo(texto_total),
                "data_leitura": lambda: _extrair_leitura(texto_total),
                "vencimento":   lambda: _extrair_vencimento(texto_total),
                "valor":        lambda: _extrair_valor(texto_total),
            }

            for campo, fn in extratores.items():
                if resultado[campo] == "Não encontrado":
                    resultado[campo] = fn()

            if all(resultado[campo] != "Não encontrado" for campo in campos_principais):
                break

        resultado["leituras_medidor"] = _extrair_leituras_medidor_pdf(pdf)
        resultado["historico_medidor"] = _extrair_historico_medidor_pdf(pdf)

    return resultado


def extrair_faturas_pdf(caminho_pdf: str | Path) -> list[dict]:
    with pdfplumber.open(caminho_pdf) as pdf:
        faturas_elektro = _extrair_faturas_elektro_pdf(pdf)
        if faturas_elektro:
            return faturas_elektro

    return [extrair_dados_cpfl(caminho_pdf)]


# =====================================================================
# MÓDULO DE BANCO DE DADOS
# =====================================================================

def _conectar_banco():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=_config_obrigatoria("DB_PASSWORD"),
        connect_timeout=10,
    )


def salvar_fatura_completa(dados: dict, pdf_meta: dict | None = None) -> tuple[int | None, bool]:
    """
    Grava a fatura e seus detalhes numa unica transacao.

    Substitui o par salvar_no_banco() + salvar_detalhes_fatura(), que tinha
    dois problemas:

    1. Race condition: fazia SELECT e depois INSERT sem lock. Duas execucoes
       simultaneas do robo liam "nao existe" ao mesmo tempo e inseriam a
       mesma fatura duas vezes. Agora e um unico INSERT ... ON CONFLICT, que
       o Postgres resolve atomicamente (depende da constraint unica criada
       pela migration 0007).

    2. Commit dividido: a fatura commitava, os detalhes commitavam depois. Se
       a segunda etapa falhasse, a fatura ficava no banco sem detalhes, o
       robo devolvia sucesso=False e o e-mail voltava para reprocessamento —
       gerando divergencia entre o que estava gravado e o que o painel dizia.
       Agora ou as duas escritas entram, ou nenhuma entra.

    Retorna (id_da_fatura, era_nova).
    """
    conexao = None
    cursor = None
    try:
        data_formatada = datetime.strptime(dados["vencimento"], "%d/%m/%Y").date()
        valor_float = float(str(dados["valor"]).replace(".", "").replace(",", "."))
        instalacao = dados["instalacao"]

        conexao = _conectar_banco()
        cursor = conexao.cursor()

        # xmax = 0 e o jeito do Postgres de dizer "esta linha foi inserida
        # agora", em oposicao a "ja existia e foi atualizada". Evita um
        # SELECT extra so para descobrir se a fatura e nova.
        cursor.execute(
            """
            INSERT INTO faturas_cpfl
                (cliente_nome, data_vencimento, valor_pagar,
                 instalacao, consumo_kwh, periodo_leitura)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (instalacao, data_vencimento) DO UPDATE
               SET cliente_nome    = EXCLUDED.cliente_nome,
                   valor_pagar     = EXCLUDED.valor_pagar,
                   consumo_kwh     = EXCLUDED.consumo_kwh,
                   periodo_leitura = EXCLUDED.periodo_leitura
            RETURNING id, (xmax = 0) AS inserida;
            """,
            (
                dados["nome"],
                data_formatada,
                valor_float,
                instalacao,
                dados["consumo_kwh"],
                dados["data_leitura"],
            ),
        )
        fatura_id, era_nova = cursor.fetchone()

        leituras = dados.get("leituras_medidor") or []
        historico = dados.get("historico_medidor") or []
        pdf_meta = pdf_meta or {}

        cursor.execute(
            """
            INSERT INTO monitor_faturacpfldetalhe
                (fatura_id, instalacao, data_vencimento,
                 leituras_medidor, historico_medidor,
                 pdf_sha256, pdf_nome_original, pdf_tamanho_bytes, pdf_paginas,
                 criado_em, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (fatura_id) DO UPDATE
               SET instalacao        = EXCLUDED.instalacao,
                   data_vencimento   = EXCLUDED.data_vencimento,
                   leituras_medidor  = EXCLUDED.leituras_medidor,
                   historico_medidor = EXCLUDED.historico_medidor,
                   pdf_sha256        = EXCLUDED.pdf_sha256,
                   pdf_nome_original = EXCLUDED.pdf_nome_original,
                   pdf_tamanho_bytes = EXCLUDED.pdf_tamanho_bytes,
                   pdf_paginas       = EXCLUDED.pdf_paginas,
                   atualizado_em     = NOW();
            """,
            (
                fatura_id,
                instalacao or "",
                data_formatada,
                Json(leituras),
                Json(historico),
                pdf_meta.get("sha256", ""),
                pdf_meta.get("nome_original", "")[:255],
                pdf_meta.get("tamanho_bytes", 0),
                pdf_meta.get("paginas", 0),
            ),
        )

        conexao.commit()
        log.info(
            "Fatura %s gravada (instalacao %s): %s leitura(s), %s grupo(s) de historico.",
            "nova" if era_nova else "atualizada",
            mascarar_instalacao(instalacao),
            len(leituras),
            len(historico),
        )
        return fatura_id, bool(era_nova)

    except ValueError as e:
        if conexao:
            conexao.rollback()
        log.error("Erro ao converter dados para o banco: %s", e)
        return None, False
    except psycopg2.Error as e:
        if conexao:
            conexao.rollback()
        log.error("Erro no PostgreSQL ao gravar fatura: %s", e)
        return None, False
    finally:
        if cursor:
            cursor.close()
        if conexao:
            conexao.close()


def _buscar_unidade_por_instalacao(instalacao: str) -> dict | None:
    if not instalacao or "encontrado" in instalacao.lower():
        return None

    conexao = None
    cursor = None
    try:
        conexao = _conectar_banco()
        cursor = conexao.cursor()
        cursor.execute(
            """
            SELECT id, empresa_id, nome, instalacao, email_automacao
              FROM monitor_unidadeconsumidora
             WHERE instalacao = %s
               AND ativa = true
             LIMIT 1;
            """,
            (instalacao,),
        )
        linha = cursor.fetchone()
        if not linha:
            return None
        return {
            "id": linha[0],
            "empresa_id": linha[1],
            "nome": linha[2],
            "instalacao": linha[3],
            "email_automacao": linha[4] or "",
        }
    except psycopg2.Error as e:
        log.error("Erro ao consultar unidade consumidora: %s", e)
        return None
    finally:
        if cursor:
            cursor.close()
        if conexao:
            conexao.close()


def _registrar_alerta_operacional(
    tipo: str,
    titulo: str,
    descricao: str,
    severidade: str = "medium",
    empresa_id: int | None = None,
    unidade_id: int | None = None,
) -> None:
    conexao = None
    cursor = None
    try:
        conexao = _conectar_banco()
        cursor = conexao.cursor()
        cursor.execute(
            """
            INSERT INTO monitor_alertaoperacional
                (empresa_id, unidade_id, fatura_id, tipo, titulo, descricao,
                 severidade, resolvido, criado_em)
            VALUES (%s, %s, NULL, %s, %s, %s, %s, false, NOW());
            """,
            (empresa_id, unidade_id, tipo, titulo, descricao, severidade),
        )
        conexao.commit()
    except psycopg2.Error as e:
        log.error("Erro ao registrar alerta operacional: %s", e)
    finally:
        if cursor:
            cursor.close()
        if conexao:
            conexao.close()


def _normalizar_email(endereco: str) -> str:
    return (endereco or "").strip().lower()


def _email_alias_compativel(unidade: dict, destinatarios: list[str]) -> bool:
    alias = _normalizar_email(unidade.get("email_automacao", ""))
    if not alias or not destinatarios:
        return True
    return alias in {_normalizar_email(destinatario) for destinatario in destinatarios}


# =====================================================================
# MÓDULO DE E-MAIL
# =====================================================================

def enviar_alerta_email(nome: str, vencimento: str, valor: str) -> None:
    """Envia um e-mail de alerta após registrar uma nova fatura."""
    try:
        conta = _config_obrigatoria("EMAIL_CONTA")
        senha = _config_obrigatoria("SENHA_APP")
        destino = _config_obrigatoria("EMAIL_DESTINO")
    except RuntimeError as e:
        log.error("Alerta nao enviado: %s", e)
        return

    msg = EmailMessage()
    msg["Subject"] = f"Fatura CPFL Registrada: {nome}"
    msg["From"]    = conta
    msg["To"]      = destino

    msg.set_content(
        f"Olá!\n\n"
        f"O robô acaba de registrar uma nova fatura da CPFL.\n\n"
        f"Cliente   : {nome}\n"
        f"Vencimento: {vencimento}\n"
        f"Valor     : R$ {valor}\n\n"
        f"Acesse o painel para visualizar o gráfico e os detalhes.\n"
    )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
            smtp.login(conta, senha)
            smtp.send_message(msg)
            # O corpo do e-mail leva o nome real (vai para o gestor); o log
            # fica com a versao mascarada.
            log.info("Alerta enviado para %s.", mascarar_email(destino))
    except Exception as e:
        log.error("Erro ao enviar e-mail de alerta: %s", e)


# =====================================================================
# MÓDULO IMAP — baixar PDF do último e-mail
# =====================================================================

def _extrair_destinatarios_mensagem(msg) -> list[str]:
    cabecalhos = [
        "Delivered-To",
        "X-Original-To",
        "Envelope-To",
        "To",
        "Cc",
        "Resent-To",
        "X-Forwarded-To",
    ]
    enderecos = []
    for cabecalho in cabecalhos:
        for _nome, endereco in getaddresses(msg.get_all(cabecalho, [])):
            endereco = _normalizar_email(endereco)
            if endereco and endereco not in enderecos:
                enderecos.append(endereco)
    return enderecos


def _baixar_pdf_da_mensagem(mail: imaplib.IMAP4_SSL, uid: bytes) -> tuple[Path | None, dict]:
    """
    Baixa o primeiro anexo PDF valido da mensagem `uid`.

    Duas correcoes de seguranca em relacao a versao anterior:

    1. Path traversal (era o risco mais grave do sistema). O codigo antigo
       fazia `Path(os.getcwd()) / parte.get_filename()`. O nome do anexo e
       escolhido por quem envia o e-mail, e qualquer pessoa pode escrever
       para o alias da automacao. Um anexo chamado "../../core/settings.py"
       gravava fora do diretorio e sobrescrevia codigo da aplicacao. Agora o
       nome passa por nome_arquivo_seguro() e a gravacao e confinada a
       PDFS_RECEBIDOS_DIR, com verificacao de contencao do caminho.

    2. Validacao de conteudo. Antes bastava o nome terminar em ".pdf" para o
       arquivo ir direto ao parser. Agora o conteudo e conferido por magic
       bytes, tamanho e numero de paginas antes de qualquer parsing caro.
    """
    metadados = {"destinatarios": [], "pdf": {}, "tinha_anexo_pdf": False}

    _status, dados_email = mail.fetch(uid, "(RFC822)")
    if not dados_email or not dados_email[0]:
        log.warning("Nao foi possivel buscar a mensagem %s.", uid)
        return None, metadados

    msg = email.message_from_bytes(dados_email[0][1])
    metadados["destinatarios"] = _extrair_destinatarios_mensagem(msg)
    log.info(
        "Destinatarios da mensagem: %s",
        ", ".join(mascarar_email(e) for e in metadados["destinatarios"]) or "nenhum",
    )

    for parte in msg.walk():
        if parte.get_content_maintype() == "multipart":
            continue
        if parte.get("Content-Disposition") is None:
            continue

        nome_anexo = parte.get_filename()
        if not nome_anexo or not nome_anexo.lower().endswith(".pdf"):
            continue

        # A partir daqui a mensagem e candidata a fatura. A distincao importa
        # no orquestrador: "nao tinha anexo" e coisa diferente de "tinha, mas
        # foi recusado".
        metadados["tinha_anexo_pdf"] = True

        conteudo = parte.get_payload(decode=True)
        if not conteudo:
            continue

        # Confere o tamanho antes de tocar o disco: um anexo absurdo nao
        # chega a ser gravado.
        limite = int(os.getenv("PDF_TAMANHO_MAXIMO_MB", "25")) * 1024 * 1024
        if len(conteudo) > limite:
            log.warning(
                "Anexo recusado: %.1f MB excede o limite de %d MB.",
                len(conteudo) / 1024 / 1024,
                limite // 1024 // 1024,
            )
            continue

        caminho_pdf = gravar_arquivo_seguro(PDFS_RECEBIDOS_DIR, nome_anexo, conteudo)
        log.info("Anexo gravado como %s.", caminho_pdf.name)

        try:
            info = validar_pdf(caminho_pdf)
        except PdfInvalido as erro:
            log.warning("Anexo recusado na validacao: %s", erro)
            _guardar_pdf_falha(caminho_pdf, motivo="invalido")
            continue

        info["nome_original"] = nome_anexo[:255]
        metadados["pdf"] = info
        log.info(
            "PDF valido: %s pagina(s), %.1f KB, sha256 %s...",
            info["paginas"],
            info["tamanho_bytes"] / 1024,
            info["sha256"][:12],
        )
        return caminho_pdf, metadados

    if metadados["tinha_anexo_pdf"]:
        log.warning("A mensagem trazia PDF, mas nenhum passou na validacao.")
    else:
        log.debug("Mensagem sem anexo PDF.")
    return None, metadados


def _mover_email_para_processados(
    mail: imaplib.IMAP4_SSL,
    uid: bytes,
) -> None:
    """Cria a pasta 'processados' (se necessário) e move o e-mail."""
    try:
        mail.create("processados")
        log.info("Pasta 'processados' criada.")
    except imaplib.IMAP4.error as e:
        # A pasta provavelmente já existe — não é um erro fatal
        log.debug("Não foi possível criar pasta 'processados': %s", e)

    try:
        mail.copy(uid, "processados")
        mail.store(uid, "+FLAGS", "\\Deleted")
        mail.expunge()
        log.info("E-mail movido para a pasta 'processados'.")
    except imaplib.IMAP4.error as e:
        log.error("Erro ao mover e-mail para 'processados': %s", e)


def _mover_email_para_pendentes(
    mail: imaplib.IMAP4_SSL,
    uid: bytes,
) -> None:
    """Cria a pasta 'pendentes' e move o e-mail que falhou na leitura."""
    try:
        mail.create("pendentes")
        log.info("Pasta 'pendentes' criada.")
    except imaplib.IMAP4.error as e:
        log.debug("Nao foi possivel criar pasta 'pendentes': %s", e)

    try:
        mail.copy(uid, "pendentes")
        mail.store(uid, "+FLAGS", "\\Deleted")
        mail.expunge()
        log.info("E-mail movido para a pasta 'pendentes'.")
    except imaplib.IMAP4.error as e:
        log.error("Erro ao mover e-mail para 'pendentes': %s", e)


def _guardar_pdf_falha(caminho_pdf: Path, motivo: str = "falha_leitura") -> Path | None:
    try:
        PDFS_FALHA_DIR.mkdir(parents=True, exist_ok=True)
        nome_seguro = re.sub(r"[^A-Za-z0-9_.-]+", "_", caminho_pdf.name)
        destino = PDFS_FALHA_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{motivo}_{nome_seguro}"
        move(str(caminho_pdf), str(destino))
        log.warning("PDF com falha salvo para analise: %s", destino)
        return destino
    except OSError as e:
        log.warning("Nao foi possivel guardar o PDF com falha: %s", e)
        return None


# =====================================================================
# ORQUESTRADOR PRINCIPAL
# =====================================================================

def pdf_ja_processado(sha256: str) -> int | None:
    """
    Retorna o id da fatura ja gravada a partir deste mesmo arquivo, se houver.

    Cache de analise: o mesmo PDF reenviado (encaminhamento duplicado, e-mail
    reprocessado) nao passa de novo pelo parser, que e a parte cara. Tambem
    serve de deteccao de adulteracao: mesmo nome e tamanho com hash diferente
    significa que o conteudo mudou.
    """
    if not sha256:
        return None

    conexao = None
    cursor = None
    try:
        conexao = _conectar_banco()
        cursor = conexao.cursor()
        cursor.execute(
            "SELECT fatura_id FROM monitor_faturacpfldetalhe WHERE pdf_sha256 = %s LIMIT 1;",
            (sha256,),
        )
        linha = cursor.fetchone()
        return linha[0] if linha else None
    except psycopg2.Error as e:
        log.debug("Nao foi possivel consultar o cache de PDF: %s", e)
        return None
    finally:
        if cursor:
            cursor.close()
        if conexao:
            conexao.close()


def processar_pdf(caminho_pdf: Path, metadados_email: dict | None = None) -> dict:
    """
    Extrai, valida e persiste todas as faturas contidas em um PDF.

    Cada fatura do arquivo e tratada isoladamente: uma conta ilegivel no meio
    de um PDF com 12 faturas nao impede a gravacao das outras 11.

    Retorna um resumo com contagens e a lista de motivos de recusa, para o
    worker exibir progresso por arquivo.
    """
    metadados_email = metadados_email or {}
    pdf_meta = metadados_email.get("pdf") or {}
    resumo = {
        "sucesso": False,
        "reconhecidas": 0,
        "gravadas": 0,
        "novas": 0,
        "ignoradas": 0,
        "motivos": [],
        "sha256": pdf_meta.get("sha256", ""),
        # Empresas cujas faturas entraram nesta leitura. Sem isso, a coleta
        # automatica gerava um log sem empresa nenhuma — e a tela de Coletas
        # de cada cliente ficava vazia, mesmo tendo sido as faturas dele que
        # o robo acabara de processar.
        #
        # Lista, e nao set: este dicionario e gravado em campo JSON
        # (ArquivoProcessamento.resumo) e set nao e serializavel.
        "empresas": [],
    }

    # Se este arquivo exato ja foi processado, nao vale repetir o parsing.
    ja_existe = pdf_ja_processado(pdf_meta.get("sha256", ""))
    if ja_existe:
        log.info("PDF ja processado anteriormente (fatura #%s). Pulando parsing.", ja_existe)
        resumo.update(sucesso=True, ignoradas=1)
        resumo["motivos"].append(f"Arquivo identico ja processado (fatura #{ja_existe}).")
        return resumo

    log.info("Analisando o PDF...")
    try:
        faturas = extrair_faturas_pdf(caminho_pdf)
    except Exception as erro:
        log.error("Falha no parsing do PDF: %s", erro)
        resumo["motivos"].append(f"Erro de leitura: {erro}")
        return resumo

    if not faturas:
        if not pdf_tem_camada_de_texto(caminho_pdf):
            motivo = (
                "PDF digitalizado: o arquivo e uma imagem da conta, sem texto "
                "selecionavel. Peca ao cliente o PDF original enviado pela "
                "distribuidora, em vez da conta escaneada."
            )
        else:
            motivo = (
                "Nenhuma fatura reconhecida. O arquivo tem texto, mas o layout "
                "nao corresponde aos formatos conhecidos."
            )
        log.warning(motivo)
        resumo["motivos"].append(motivo)
        return resumo

    resumo["reconhecidas"] = len(faturas)
    if len(faturas) > 1:
        log.info("PDF com multiplas faturas reconhecidas: %s conta(s).", len(faturas))

    destinatarios = metadados_email.get("destinatarios", [])
    # Resolvido sob demanda e uma vez so: abrir o PDF para contar caracteres
    # custa caro e nao faz sentido quando a leitura deu certo.
    sem_texto = None

    for indice, dados in enumerate(faturas, start=1):
        # Dados pessoais mascarados: o log rotaciona em arquivo e costuma ser
        # copiado para suporte. Nome e instalacao completos ficam no banco.
        log.info(
            "Fatura %s/%s | cliente %s | instalacao %s | venc %s | R$ %s | %s kWh",
            indice,
            len(faturas),
            mascarar_nome(dados["nome"]),
            mascarar_instalacao(dados["instalacao"]),
            dados["vencimento"],
            dados["valor"],
            dados["consumo_kwh"],
        )

        if "encontrado" in str(dados["vencimento"]).lower() or "encontrado" in str(dados["valor"]).lower():
            # extrair_faturas_pdf() sempre devolve ao menos um registro, com os
            # campos preenchidos como "Nao encontrado". Entao e aqui, e nao na
            # lista vazia, que se descobre que a leitura falhou — e onde vale
            # dizer por que, ja que escaneado e layout desconhecido pedem
            # providencias diferentes.
            if sem_texto is None:
                sem_texto = not pdf_tem_camada_de_texto(caminho_pdf)

            if sem_texto:
                motivo = (
                    f"Fatura {indice}: PDF digitalizado, sem texto selecionavel. "
                    f"Peca ao cliente o PDF original da distribuidora, no lugar "
                    f"da conta escaneada."
                )
            else:
                motivo = (
                    f"Fatura {indice}: vencimento ou valor ilegivel. O arquivo "
                    f"tem texto, mas o layout nao corresponde aos formatos conhecidos."
                )

            log.warning(motivo)
            resumo["ignoradas"] += 1
            resumo["motivos"].append(motivo)
            continue

        unidade = _buscar_unidade_por_instalacao(dados["instalacao"])
        if not unidade:
            descricao = (
                f"O robo leu uma fatura de energia, mas a instalacao {dados['instalacao']} "
                f"nao esta cadastrada em nenhuma unidade consumidora. Cliente no PDF: {dados['nome']}."
            )
            _registrar_alerta_operacional(
                tipo="instalacao_nao_cadastrada",
                titulo="Instalacao de energia nao cadastrada",
                descricao=descricao,
                severidade="high",
            )
            log.warning(
                "Instalacao %s nao cadastrada.", mascarar_instalacao(dados["instalacao"])
            )
            resumo["ignoradas"] += 1
            resumo["motivos"].append(
                f"Fatura {indice}: instalacao {dados['instalacao']} nao cadastrada."
            )
            continue

        if not _email_alias_compativel(unidade, destinatarios):
            descricao = (
                f"A fatura da instalacao {dados['instalacao']} foi vinculada pela instalacao "
                f"a unidade {unidade['nome']}, mas chegou para {', '.join(destinatarios) or 'destinatario nao identificado'} "
                f"e o alias cadastrado e {unidade['email_automacao'] or 'nao informado'}."
            )
            _registrar_alerta_operacional(
                tipo="alias_divergente",
                titulo="E-mail da automacao diferente da instalacao",
                descricao=descricao,
                severidade="medium",
                empresa_id=unidade["empresa_id"],
                unidade_id=unidade["id"],
            )
            log.warning("Alias de automacao divergente na instalacao %s.",
                        mascarar_instalacao(dados["instalacao"]))

        fatura_id, era_nova = salvar_fatura_completa(dados, pdf_meta)
        if not fatura_id:
            resumo["ignoradas"] += 1
            resumo["motivos"].append(f"Fatura {indice}: falha ao gravar no banco.")
            continue

        resumo["gravadas"] += 1
        resumo["sucesso"] = True
        empresa_id = unidade.get("empresa_id")
        if empresa_id and empresa_id not in resumo["empresas"]:
            resumo["empresas"].append(empresa_id)
        if era_nova:
            resumo["novas"] += 1
            enviar_alerta_email(dados["nome"], dados["vencimento"], dados["valor"])

    log.info(
        "PDF concluido: %s reconhecida(s), %s gravada(s), %s nova(s), %s ignorada(s).",
        resumo["reconhecidas"], resumo["gravadas"], resumo["novas"], resumo["ignoradas"],
    )
    return resumo


# Mantido por compatibilidade com codigo que chamava o nome antigo.
def _processar_fatura(caminho_pdf: Path, metadados_email: dict | None = None) -> tuple[bool, dict]:
    resumo = processar_pdf(caminho_pdf, metadados_email)
    return resumo["sucesso"], resumo


def executar_extracao(limite_mensagens: int = 25, ao_progredir=None) -> bool:
    """
    Le a caixa de entrada e processa ate `limite_mensagens` e-mails.

    Antes processava apenas o ultimo e-mail (`ids_emails[-1]`) por execucao:
    com 30 faturas na caixa, era preciso disparar o robo 30 vezes. Agora
    percorre o lote, e cada mensagem fica isolada em seu proprio try — um PDF
    corrompido registra a falha e o laco segue para o proximo.

    `ao_progredir(indice, total, resumo)` e chamado apos cada mensagem, para
    o worker publicar progresso por arquivo.
    """
    log.info("Iniciando o Robo Leitor de faturas...")
    mail = None
    houve_sucesso = False
    ignoradas = 0

    try:
        conta = _config_obrigatoria("EMAIL_CONTA")
        senha = _config_obrigatoria("SENHA_APP")
    except RuntimeError as e:
        log.error("Coleta nao iniciada: %s", e)
        return False

    try:
        log.info("Conectando a caixa de entrada...")
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(conta, senha)
        mail.select("inbox")

        _status, mensagens = mail.search(None, "ALL")
        ids_emails = mensagens[0].split()
        if not ids_emails:
            log.warning("Nenhum e-mail encontrado na caixa de entrada.")
            return False

        # Mais recentes primeiro, respeitando o teto do lote.
        lote = list(reversed(ids_emails))[:limite_mensagens]
        log.info("Processando %s de %s mensagem(ns) na caixa.", len(lote), len(ids_emails))

        for indice, uid in enumerate(lote, start=1):
            caminho_pdf = None
            resumo = {"sucesso": False, "motivos": []}
            try:
                caminho_pdf, metadados_email = _baixar_pdf_da_mensagem(mail, uid)
                if not caminho_pdf:
                    if metadados_email.get("tinha_anexo_pdf"):
                        # Trazia PDF e ele foi recusado: e uma falha de leitura
                        # de verdade, vai para 'pendentes' para alguem olhar.
                        resumo["motivos"].append("PDF anexo recusado na validacao.")
                        _mover_email_para_pendentes(mail, uid)
                    else:
                        # Sem anexo nenhum: nao e e-mail de fatura. Antes, tudo
                        # que caisse na caixa — alerta do Google, boletim,
                        # aviso de entrega — era jogado em 'pendentes', tirando
                        # a mensagem da caixa de entrada sem motivo.
                        resumo["motivos"].append("Mensagem sem anexo PDF; ignorada.")
                        ignoradas += 1
                    continue

                resumo = processar_pdf(caminho_pdf, metadados_email)

                if resumo["sucesso"]:
                    houve_sucesso = True
                    _mover_email_para_processados(mail, uid)
                else:
                    _mover_email_para_pendentes(mail, uid)

            except Exception as erro:
                # Isolamento por mensagem: uma falha aqui nao aborta o lote.
                log.exception("Falha ao processar a mensagem %s: %s", indice, erro)
                resumo["motivos"].append(str(erro))
            finally:
                _descartar_pdf(caminho_pdf, resumo.get("sucesso", False))
                if ao_progredir:
                    try:
                        ao_progredir(indice, len(lote), resumo)
                    except Exception:
                        log.debug("Callback de progresso falhou.", exc_info=True)

    except imaplib.IMAP4.error as e:
        log.error("Erro de autenticacao/IMAP: %s", e)
    except Exception as e:
        log.exception("Erro inesperado: %s", e)
    finally:
        if ignoradas:
            log.info("%s mensagem(ns) sem anexo PDF deixadas na caixa de entrada.", ignoradas)
        if mail:
            try:
                mail.logout()
                log.info("Conexao com o e-mail encerrada.")
            except Exception:
                pass

    return houve_sucesso


def _descartar_pdf(caminho_pdf: Path | None, sucesso: bool) -> None:
    """
    Remove o PDF temporario, guardando copia so quando a leitura falhou.

    Em caso de sucesso o arquivo e apagado: o conteudo ja esta no banco e
    manter fatura de cliente em disco amplia a superficie exposta sem ganho.
    """
    if not caminho_pdf:
        return

    caminho_pdf = Path(caminho_pdf)
    if not caminho_pdf.exists():
        return

    if not sucesso:
        # move() ja tira o arquivo do diretorio de recebidos.
        if _guardar_pdf_falha(caminho_pdf):
            return

    try:
        caminho_pdf.unlink()
    except OSError as e:
        log.warning("Nao foi possivel remover o PDF temporario: %s", e)


# =====================================================================
# ENTRY POINT
# =====================================================================

if __name__ == "__main__":
    # Execucao direta (fora do manage.py) precisa configurar o Django antes,
    # porque monitor.seguranca depende de django.conf.settings.
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    django.setup()

    resultado = executar_extracao()
    log.info("Concluido com sucesso." if resultado else "Finalizado com falha.")
