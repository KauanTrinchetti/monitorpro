import csv
import io
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape as escapar_marcacao

from django.conf import settings
from django.core.cache import cache
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.http import require_POST

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import fila
from .forms import EmpresaForm, UnidadeConsumidoraForm, UploadFaturasForm
from .models import (
    AlertaOperacional,
    ArquivoProcessamento,
    AuditoriaEvento,
    BackupRegistro,
    ColetaControle,
    Empresa,
    FaturaCpfl,
    FaturaCpflDetalhe,
    LoteProcessamento,
    RegistroAlteracao,
    RoboExecucaoLog,
    UnidadeConsumidora,
)
from .seguranca import (
    DestinoInvalido,
    PdfInvalido,
    enviar_sem_redirecionar,
    gravar_arquivo_seguro,
    ip_cliente,
    limpar_detalhes,
    validar_pdf,
    validar_url_webhook,
)


MESES_PT = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
COLETA_CHAVE_GLOBAL = "global"


def nome_usuario(user):
    return user.get_full_name() or user.username


def empresa_usuario(user):
    if not user or not user.is_authenticated:
        return None
    if getattr(user, "is_superuser", False):
        return None
    perfil = getattr(user, "perfil_monitor", None)
    return perfil.empresa if perfil else None


def empresa_operacional(user, criar=False):
    """
    Empresa sobre a qual o usuario opera.

    `criar` e False por padrao de proposito. Antes esta funcao criava a
    empresa "Conta Principal" sempre que nao encontrasse nenhuma — inclusive
    ao simplesmente abrir a tela de configuracoes. Um GET gravava no banco, o
    que significa que um recarregar de pagina, um prefetch do navegador ou um
    rastreador logado podiam materializar registros sozinhos.

    Agora so quem vai de fato gravar (o POST) pede `criar=True`.
    """
    empresa = empresa_usuario(user)
    if empresa:
        return empresa

    if not user or not getattr(user, "is_authenticated", False):
        return None
    if not getattr(user, "is_superuser", False):
        return None

    empresa = Empresa.objects.filter(ativa=True).order_by("id").first()
    if empresa or not criar:
        return empresa

    return Empresa.objects.create(
        nome="Conta Principal",
        email_alerta=getattr(user, "email", "") or "",
    )


def unidades_visiveis(user):
    unidades = UnidadeConsumidora.objects.filter(ativa=True)
    if not user or not getattr(user, "is_authenticated", False):
        return unidades.none()
    if getattr(user, "is_superuser", False):
        return unidades.order_by("nome")

    empresa = empresa_usuario(user)
    if not empresa:
        return unidades.none()
    unidades = unidades.filter(empresa=empresa)
    return unidades.order_by("nome")


def unidades_gerenciaveis(user):
    unidades = UnidadeConsumidora.objects.select_related("empresa").all()
    if not user or not getattr(user, "is_authenticated", False):
        return unidades.none()
    if getattr(user, "is_superuser", False):
        return unidades.order_by("empresa__nome", "nome")

    empresa = empresa_usuario(user)
    if not empresa:
        return unidades.none()
    return unidades.filter(empresa=empresa).order_by("nome")


def unidade_map(user=None):
    """
    Instalacao -> unidade, resolvido uma vez por usuario.

    Varias funcoes de montagem precisam desse mapa na mesma requisicao
    (lista de faturas, alertas, comparativos, painel financeiro). Sem cache,
    a consulta de unidades rodava tres vezes so para desenhar a tela inicial.

    O cache vive no proprio objeto do usuario, que existe apenas durante a
    requisicao: nao ha risco de servir dados de uma empresa para outra, e
    nada precisa ser invalidado.
    """
    if user is None:
        return {}

    em_cache = getattr(user, "_cache_unidade_map", None)
    if em_cache is not None:
        return em_cache

    mapa = {unidade.instalacao: unidade for unidade in unidades_visiveis(user)}
    try:
        user._cache_unidade_map = mapa
    except AttributeError:
        # AnonymousUser aceita atributos, mas nao custa nada ser tolerante.
        pass
    return mapa


def papel_usuario(user):
    if not user or not getattr(user, "is_authenticated", False):
        return "leitura"
    if getattr(user, "is_superuser", False):
        return "admin"
    perfil = getattr(user, "perfil_monitor", None)
    return perfil.papel if perfil else "leitura"


def pode_administrar(user):
    return bool(user and getattr(user, "is_authenticated", False) and papel_usuario(user) == "admin")


def pode_operar(user):
    return bool(user and getattr(user, "is_authenticated", False) and papel_usuario(user) in ["admin", "operador"])


def pode_administrar_sistema(user):
    """
    Operacoes que atravessam a fronteira entre empresas.

    Ser administrador da propria empresa nao basta aqui. O backup, por
    exemplo, gera um arquivo com Empresa, PerfilUsuario, UnidadeConsumidora e
    detalhes de fatura de *todos* os clientes: permitir que o admin de um
    cliente o dispare entrega a ele os dados dos outros.
    """
    return bool(user and getattr(user, "is_authenticated", False)
                and getattr(user, "is_superuser", False))


def rotulo_papel(user):
    papeis = {
        "admin": "Administrador",
        "operador": "Operador",
        "leitura": "Leitura",
    }
    return papeis.get(papel_usuario(user), "Leitura")


def alertas_pendentes(user):
    """Quantidade de alertas nao resolvidos, para o selo do menu."""
    if not user or not getattr(user, "is_authenticated", False):
        return 0

    alertas = AlertaOperacional.objects.filter(resolvido=False)
    empresa = empresa_usuario(user)
    if empresa:
        alertas = alertas.filter(empresa=empresa)
    elif not getattr(user, "is_superuser", False):
        return 0
    return alertas.count()


def conta_contexto(user):
    empresa = empresa_usuario(user)
    return {
        "nome": nome_usuario(user),
        "codigo": getattr(user, "id", "") or "8841",
        "empresa": empresa.nome if empresa else "Todas as empresas",
        "papel": rotulo_papel(user),
        "pode_administrar": pode_administrar(user),
        "pode_operar": pode_operar(user),
        # O menu lateral exibia {{ faturas_pendentes }}, variavel que nenhuma
        # view enviava: o selo nunca aparecia, em nenhuma situacao.
        "alertas_pendentes": alertas_pendentes(user),
    }



OPERACAO_TOKEN_SESSION_KEY = "monitor_tokens_operacao"


def novo_token_operacao(request, nome):
    token = secrets.token_urlsafe(24)
    grupos = request.session.get(OPERACAO_TOKEN_SESSION_KEY, {})
    tokens = list(grupos.get(nome, []))[-4:]
    tokens.append(token)
    grupos[nome] = tokens
    request.session[OPERACAO_TOKEN_SESSION_KEY] = grupos
    request.session.modified = True
    return token


def consumir_token_operacao(request, nome):
    token = request.POST.get("operation_token", "")
    grupos = request.session.get(OPERACAO_TOKEN_SESSION_KEY, {})
    tokens = list(grupos.get(nome, []))
    if token and token in tokens:
        tokens.remove(token)
        grupos[nome] = tokens
        request.session[OPERACAO_TOKEN_SESSION_KEY] = grupos
        request.session.modified = True
        return True
    return False


def validar_permissao(request, permitido, resumo):
    if permitido:
        return True
    registrar_auditoria(request, "acesso_negado", resumo)
    messages.error(request, "Acesso negado para esta operacao.")
    return False


def limite_negocio(request, acao, limite, segundos, por_usuario=True, rotulo=None):
    """
    Contador de tentativas por janela deslizante.

    O contador vive no cache do banco (configurado em settings.CACHES), nao
    em memoria do processo. Com LocMemCache cada worker mantinha o proprio
    contador: um limite de 10 tentativas virava 10 x numero de workers, e
    reiniciar a aplicacao zerava tudo — protecao contra brute force
    praticamente inexistente.

    `por_usuario=False` conta pela chave da acao apenas, sem misturar a
    identidade de quem chama. Serve para limitar tentativas contra uma conta
    especifica, independentemente da origem.

    `rotulo` substitui a acao no texto da auditoria, para quando a chave
    carrega algo que nao deve ser exibido.
    """
    rotulo = rotulo or acao
    if por_usuario:
        usuario = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
        identidade = f"u{usuario.id}" if usuario else f"ip{ip_cliente(request) or 'anon'}"
        chave = f"monitor:limite:{acao}:{identidade}"
    else:
        chave = f"monitor:limite:{acao}"

    try:
        cache.add(chave, 0, timeout=segundos)
        atual = cache.incr(chave)
    except ValueError:
        # A chave expirou entre o add e o incr. Recomeca a janela em vez de
        # bloquear: e uma corrida normal, nao sinal de ataque.
        cache.set(chave, 1, timeout=segundos)
        atual = 1
    except Exception:
        # Cache indisponivel: nega a operacao. Falhar fechado e a escolha
        # certa aqui — sem contador nao ha protecao contra brute force.
        registrar_auditoria(request, "operacao_bloqueada", f"{rotulo} bloqueada por falha segura no controle de limite")
        return False

    if atual > limite:
        registrar_auditoria(
            request,
            "limite_excedido",
            f"Limite de {rotulo} excedido",
            detalhes={"limite": limite, "janela_segundos": segundos, "tentativas": atual},
        )
        return False
    return True


def validar_operacao_critica(request, nome_token, acao_limite, limite, segundos):
    if not consumir_token_operacao(request, nome_token):
        registrar_auditoria(request, "operacao_bloqueada", f"{acao_limite} bloqueada por token ausente, expirado ou ja utilizado")
        messages.warning(request, "Operacao expirada ou ja processada. Atualize a pagina e tente novamente.")
        return False

    if not limite_negocio(request, acao_limite, limite, segundos):
        messages.warning(request, "Limite temporario atingido para esta operacao. Aguarde um pouco antes de tentar novamente.")
        return False
    return True

def coleta_status_atual():
    return ColetaControle.objects.select_related("iniciado_por", "log").filter(chave=COLETA_CHAVE_GLOBAL).first()


def coleta_contexto(request=None):
    controle = coleta_status_atual()
    contexto = {
        "coleta_controle": controle,
        "coleta_em_andamento": bool(controle and controle.em_andamento),
        "token_coleta": "",
    }
    if request and getattr(request, "user", None) and request.user.is_authenticated and pode_operar(request.user) and not contexto["coleta_em_andamento"]:
        contexto["token_coleta"] = novo_token_operacao(request, "coleta")
    return contexto

def registrar_auditoria(request, acao, resumo, empresa=None, alvo_tipo="", alvo_id="", detalhes=None):
    usuario = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
    if empresa is None and usuario:
        empresa = empresa_usuario(usuario)

    try:
        AuditoriaEvento.objects.create(
            usuario=usuario,
            empresa=empresa,
            acao=acao,
            resumo=(resumo or "")[:255],
            alvo_tipo=alvo_tipo or "",
            alvo_id=str(alvo_id or ""),
            ip=ip_cliente(request),
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:255],
            # limpar_detalhes remove senha/token que tenham chegado aqui por
            # engano, para que a auditoria nunca guarde segredo em claro.
            detalhes=limpar_detalhes(detalhes),
        )
    except Exception:
        pass


def registrar_alteracoes(request, instancia, valores_anteriores, operacao="alteracao", rotulo=""):
    """
    Grava campo a campo o que mudou em um objeto.

    AuditoriaEvento diz que a unidade foi editada; isto diz que o limite de
    alerta saiu de 20% para 300%, quem fez e quando. E o que permite
    reconstruir o historico de um dado critico depois de uma mudanca indevida.
    """
    empresa = getattr(instancia, "empresa", None) or empresa_usuario(request.user)
    modelo = instancia.__class__.__name__
    endereco = ip_cliente(request)
    usuario = request.user if request.user.is_authenticated else None

    registros = []
    for campo, valor_antigo in (valores_anteriores or {}).items():
        valor_novo = getattr(instancia, campo, None)
        if str(valor_antigo) == str(valor_novo):
            continue
        registros.append(
            RegistroAlteracao(
                usuario=usuario,
                empresa=empresa,
                modelo=modelo,
                objeto_id=str(instancia.pk),
                rotulo_objeto=(rotulo or str(instancia))[:200],
                operacao=operacao,
                campo=campo,
                valor_anterior=str(valor_antigo)[:2000] if valor_antigo is not None else "",
                valor_novo=str(valor_novo)[:2000] if valor_novo is not None else "",
                ip=endereco,
            )
        )

    if registros:
        RegistroAlteracao.objects.bulk_create(registros)
    return len(registros)


def snapshot_campos(instancia, campos):
    """Fotografa os valores atuais antes de um save(), para comparar depois."""
    if not instancia or not instancia.pk:
        return {}
    return {campo: getattr(instancia, campo, None) for campo in campos}

# Uma coleta que passe disso quase certamente e trava orfa, nao execucao real.
COLETA_TEMPO_MAXIMO = timedelta(minutes=45)


def liberar_coleta_travada():
    """
    Solta a trava global deixada por uma coleta que nunca terminou.

    O `finally` que liberava a trava so roda se o processo continuar vivo. Um
    worker morto por timeout do proxy, deploy ou falta de memoria deixava
    `em_andamento=True` para sempre, e a partir dai ninguem mais conseguia
    coletar — era preciso editar o banco a mao.
    """
    limite = timezone.now() - COLETA_TEMPO_MAXIMO
    with transaction.atomic():
        controle = ColetaControle.objects.select_for_update().filter(
            chave=COLETA_CHAVE_GLOBAL,
            em_andamento=True,
            iniciado_em__lt=limite,
        ).first()
        if not controle:
            return False

        controle.em_andamento = False
        controle.finalizado_em = timezone.now()
        controle.mensagem = "Liberada automaticamente: execucao anterior nao finalizou."
        controle.save(update_fields=["em_andamento", "finalizado_em", "mensagem", "atualizado_em"])

        if controle.log_id:
            RoboExecucaoLog.objects.filter(pk=controle.log_id, status="processando").update(
                status="erro",
                mensagem="Coleta interrompida sem finalizacao",
                finalizado_em=timezone.now(),
            )
        return True


def iniciar_coleta_com_trava(user):
    liberar_coleta_travada()
    with transaction.atomic():
        controle, _ = ColetaControle.objects.select_for_update().get_or_create(chave=COLETA_CHAVE_GLOBAL)
        if controle.em_andamento:
            return None, controle

        log = RoboExecucaoLog.objects.create(
            empresa=empresa_usuario(user),
            status="processando",
            mensagem="Coleta em andamento",
        )
        agora = timezone.now()
        controle.em_andamento = True
        controle.iniciado_em = agora
        controle.finalizado_em = None
        controle.iniciado_por = user if getattr(user, "is_authenticated", False) else None
        controle.log = log
        controle.mensagem = "Coleta em andamento"
        controle.save(update_fields=[
            "em_andamento",
            "iniciado_em",
            "finalizado_em",
            "iniciado_por",
            "log",
            "mensagem",
            "atualizado_em",
        ])
        return log, controle


def finalizar_coleta_com_trava(log, mensagem):
    with transaction.atomic():
        controle = ColetaControle.objects.select_for_update().filter(chave=COLETA_CHAVE_GLOBAL).first()
        if not controle or controle.log_id != log.id:
            return
        controle.em_andamento = False
        controle.finalizado_em = timezone.now()
        controle.mensagem = mensagem
        controle.save(update_fields=["em_andamento", "finalizado_em", "mensagem", "atualizado_em"])


def formatar_moeda(valor):
    valor = valor or Decimal("0")
    return f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_valor_tabela(valor):
    return f"R$ {formatar_moeda(valor)}"


def consumo_para_int(consumo):
    if not consumo or consumo in ["Nao encontrado", "Não encontrado"]:
        return 0

    try:
        consumo_limpo = consumo.lower().replace("kwh", "").strip()
        consumo_limpo = consumo_limpo.replace(".", "").replace(",", ".")
        return int(float(consumo_limpo))
    except (TypeError, ValueError):
        return 0


def faturas_base(user=None):
    faturas = FaturaCpfl.objects.all()
    if not user or not getattr(user, "is_authenticated", False):
        return faturas.none()
    if not getattr(user, "is_superuser", False):
        empresa = empresa_usuario(user)
        if not empresa:
            return faturas.none()
        instalacoes = list(unidades_visiveis(user).values_list("instalacao", flat=True))
        faturas = faturas.filter(instalacao__in=instalacoes) if instalacoes else faturas.none()
    return faturas.order_by("-data_vencimento", "-id")


def montar_lista_faturas(faturas, user=None):
    unidades = unidade_map(user)
    lista = []
    for fatura in faturas:
        consumo = consumo_para_int(fatura.consumo_kwh)
        unidade = unidades.get(fatura.instalacao)
        lista.append({
            "id": fatura.id,
            "cliente": fatura.cliente_nome,
            "unidade": unidade.nome if unidade else fatura.instalacao,
            "instalacao": fatura.instalacao or "",
            "tipo": tipo_conta(fatura, unidade),
            "vencimento": fatura.data_vencimento.strftime("%d/%m/%Y") if fatura.data_vencimento else "",
            "valor": formatar_valor_tabela(fatura.valor_pagar),
            "consumo": f"{consumo} kWh" if consumo else "-",
        })
    return lista


def tipo_conta(fatura, unidade=None):
    """
    Tipo da conta, sempre a partir da unidade cadastrada.

    A versao anterior tinha o numero de instalacao de um cliente escrito no
    codigo (aquela instalacao -> Industrial) e, para todo o resto, devolvia
    "Residencial". Dois problemas: qualquer outra unidade industrial aparecia
    como residencial, e um palpite era apresentado como fato — inclusive nos
    relatorios exportados ao cliente.

    Sem unidade cadastrada nao ha como saber o tipo, e o relatorio passa a
    dizer exatamente isso.
    """
    if unidade:
        return unidade.get_tipo_display()
    return "Nao cadastrada"


def valor_csv(valor):
    return str(valor).replace(".", ",") if valor else ""


# Inicios de celula que Excel, LibreOffice e Google Planilhas tratam como
# formula. Nome de empresa, de unidade e de titular sao texto livre — o de
# titular sai de dentro do PDF, que qualquer remetente controla. Um nome como
# =HYPERLINK("https://...";"Clique") virava link ativo na planilha de quem
# abrisse a exportacao, inclusive a do superusuario, que exporta os dados de
# todas as empresas. Referencia: OWASP, "CSV Injection".
INICIO_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def texto_csv(valor):
    """Texto livre para o CSV, neutralizado contra injecao de formula."""
    texto = "" if valor is None else str(valor)
    if texto.startswith(INICIO_DE_FORMULA):
        # O apostrofo inicial faz a planilha exibir o valor como texto.
        return "'" + texto
    return texto


def texto_pdf(valor, vazio="-"):
    """
    Texto livre para um Paragraph do reportlab, com a marcacao escapada.

    O Paragraph interpreta uma mini linguagem de marcacao (<b>, <font>,
    <a href>). Sem escapar, um nome de unidade com <a href="..."> virava link
    clicavel no relatorio entregue ao cliente, e um "<" sem par derrubava a
    geracao do PDF inteiro.
    """
    return escapar_marcacao(str(valor) if valor not in (None, "") else vazio)


def numero_csv(valor):
    """
    Numero com virgula decimal, para o Excel em portugues reconhecer como
    numero. Se exportassemos "+23,1%", a coluna viraria texto e nao daria
    para ordenar nem somar.
    """
    if valor is None:
        return ""
    return f"{valor:.2f}".replace(".", ",")


DIRECOES = {
    "alta": "Alta",
    "baixa": "Queda",
    "estavel": "Estavel",
    "sem_base": "Primeira fatura",
}


def rotulo_direcao(comparativo):
    if not comparativo or not comparativo.get("tem_base"):
        return DIRECOES["sem_base"]
    return DIRECOES.get(comparativo.get("direcao"), "")


# Cores dos relatorios impressos. Sao mais escuras que as da tela: o painel
# tem fundo escuro, o PDF e branco, e os tons claros ficariam ilegiveis no
# papel. Conferidas para contraste adequado sobre branco.
COR_ALTA = colors.HexColor("#b91c1c")    # conta subindo
COR_BAIXA = colors.HexColor("#15803d")   # conta caindo
COR_NEUTRA = colors.HexColor("#64748b")


def _linhas_destaque_pdf(resumo_variacao, estilo, estilo_forte):
    """Bloco 'Destaques do periodo' do PDF: maiores altas e maiores quedas."""
    altas = resumo_variacao.get("altas") or []
    baixas = resumo_variacao.get("baixas") or []
    if not altas and not baixas:
        return []

    linhas = [[
        Paragraph("Destaque", estilo_forte),
        Paragraph("Unidade", estilo_forte),
        Paragraph("Mes", estilo_forte),
        Paragraph("Consumo", estilo_forte),
        Paragraph("Var. consumo", estilo_forte),
        Paragraph("Impacto no valor", estilo_forte),
    ]]

    estilos_linha = []
    for grupo, rotulo, cor in ((altas, "ALTA", COR_ALTA), (baixas, "QUEDA", COR_BAIXA)):
        for item in grupo:
            indice = len(linhas)
            sufixo = "a mais" if rotulo == "ALTA" else "a menos"
            linhas.append([
                Paragraph(rotulo, estilo_forte),
                Paragraph(texto_pdf(item["unidade"]), estilo),
                Paragraph(item["mes"], estilo),
                Paragraph(f"{item['consumo_anterior']} para {item['consumo_atual']} kWh", estilo),
                Paragraph(item["variacao"], estilo_forte),
                Paragraph(f"{item['diferenca_valor']} {sufixo}", estilo),
            ])
            # A palavra ALTA/QUEDA carrega o sentido; a cor so reforca, para
            # o relatorio continuar legivel impresso em preto e branco.
            estilos_linha.append(("TEXTCOLOR", (0, indice), (0, indice), cor))
            estilos_linha.append(("TEXTCOLOR", (4, indice), (4, indice), cor))

    tabela = Table(
        linhas,
        colWidths=[2.0 * cm, 6.0 * cm, 2.0 * cm, 6.0 * cm, 3.0 * cm, 6.4 * cm],
        repeatRows=1,
    )
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dbe4ef")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        *estilos_linha,
    ]))

    return [
        Paragraph("Destaques do periodo", estilo_forte),
        Spacer(1, 0.15 * cm),
        tabela,
        Spacer(1, 0.45 * cm),
    ]


def _numerar_paginas(canvas, doc):
    """Rodape com numero da pagina e data — o relatorio costuma ser impresso."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(COR_NEUTRA)
    largura, _altura = landscape(A4)
    canvas.drawRightString(
        largura - 1.2 * cm, 0.6 * cm,
        f"Pagina {canvas.getPageNumber()}",
    )
    canvas.drawString(
        1.2 * cm, 0.6 * cm,
        f"MonitorPro - gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')}",
    )
    canvas.restoreState()


def consumo_csv(consumo):
    consumo_int = consumo_para_int(consumo)
    return str(consumo_int) if consumo_int else ""


def periodo_leitura(fatura):
    if fatura.periodo_leitura and fatura.periodo_leitura not in ["Nao encontrado", "Não encontrado"]:
        return fatura.periodo_leitura
    return ""


def montar_grafico(faturas):
    """
    Barras do historico de gastos, em altura proporcional ao maior valor.

    A versao anterior dividia por uma constante 400 e limitava a 120px. Com
    faturas na casa dos R$ 4.000 — a realidade destas contas — todas as barras
    estouravam o teto e saiam com exatamente a mesma altura: o grafico ficava
    plano e nao informava nada.

    Escalar pelo maior valor do proprio conjunto faz o grafico funcionar em
    qualquer faixa de preco, de conta residencial a industrial.
    """
    recentes = list(reversed(list(faturas[:6])))
    if not recentes:
        return []

    valores = [float(fatura.valor_pagar or 0) for fatura in recentes]
    maior = max(valores) or 1

    dados = []
    for fatura, valor in zip(recentes, valores):
        vencimento = fatura.data_vencimento
        mes = f"{MESES_PT[vencimento.month - 1]}/{str(vencimento.year)[2:]}" if vencimento else "-"

        # Percentual, nao pixel: a barra acompanha a altura do painel em
        # qualquer tela. O piso de 6% mantem visivel a barra de um mes de
        # valor muito baixo, que de outro modo sumiria.
        altura = max(6, round((valor / maior) * 100))

        dados.append({
            "mes": mes,
            "valor": formatar_moeda(valor),
            "altura": altura,
            "destaque": valor == maior,
        })
    return dados


def montar_resumo(faturas):
    total_gasto = faturas.aggregate(soma=Sum("valor_pagar"))["soma"] or Decimal("0")
    ultima_fatura = faturas.first()

    return {
        "total_faturas": faturas.count(),
        "total_gasto": formatar_moeda(total_gasto),
        "mes_atual_gasto": formatar_moeda(ultima_fatura.valor_pagar if ultima_fatura else 0),
        "total_consumo": sum(consumo_para_int(fatura.consumo_kwh) for fatura in faturas),
    }


def formatar_percentual(valor):
    if valor is None:
        return "-"
    sinal = "+" if valor > 0 else ""
    return f"{sinal}{valor:.1f}%".replace(".", ",")


def mes_fatura(fatura):
    vencimento = fatura.data_vencimento
    if not vencimento:
        return "Sem vencimento"
    return f"{MESES_PT[vencimento.month - 1]}/{str(vencimento.year)[2:]}"


def _variacao_percentual(atual, anterior):
    """Variacao de `anterior` para `atual`, ou None quando nao ha base."""
    if not anterior:
        return None
    return ((float(atual) - float(anterior)) / float(anterior)) * 100


def montar_comparativos_por_id(faturas, user=None):
    """
    Compara cada fatura com a anterior da mesma instalacao.

    Calcula variacao de consumo e de valor. Sao coisas diferentes e podem
    andar em sentidos opostos: com reajuste de tarifa, a conta sobe mesmo com
    o consumo caindo — e quem so olha o kWh nao entende por que pagou mais.
    """
    unidades = unidade_map(user)
    lista = sorted(
        list(faturas),
        key=lambda fatura: (fatura.instalacao or "", fatura.data_vencimento or date.min, fatura.id),
    )
    anterior_por_instalacao = {}
    comparativos = {}

    for fatura in lista:
        chave = fatura.instalacao or ""
        anterior = anterior_por_instalacao.get(chave)

        consumo_atual = consumo_para_int(fatura.consumo_kwh)
        consumo_anterior = consumo_para_int(anterior.consumo_kwh) if anterior else 0
        variacao = _variacao_percentual(consumo_atual, consumo_anterior)

        valor_atual = fatura.valor_pagar or Decimal("0")
        valor_anterior = (anterior.valor_pagar or Decimal("0")) if anterior else Decimal("0")
        variacao_valor = _variacao_percentual(valor_atual, valor_anterior)

        unidade = unidades.get(fatura.instalacao)
        limite = float(unidade.limite_aumento_percentual) if unidade else 20.0
        anomalia = variacao is not None and variacao > limite

        # Faixa morta de 1%: oscilacao minima de leitura nao e "alta" nem
        # "baixa", e marcar como tal so produz ruido no relatorio.
        if variacao is None:
            direcao = "sem_base"
        elif variacao > 1:
            direcao = "alta"
        elif variacao < -1:
            direcao = "baixa"
        else:
            direcao = "estavel"

        comparativos[fatura.id] = {
            "consumo_anterior": consumo_anterior,
            "consumo_atual": consumo_atual,
            "variacao_numero": variacao,
            "variacao": formatar_percentual(variacao),
            "valor_anterior": valor_anterior,
            "valor_anterior_formatado": formatar_valor_tabela(valor_anterior) if anterior else "-",
            "variacao_valor_numero": variacao_valor,
            "variacao_valor": formatar_percentual(variacao_valor),
            "diferenca_valor": formatar_valor_tabela(abs(valor_atual - valor_anterior)) if anterior else "-",
            "limite": formatar_percentual(limite),
            "status": "Aumento anormal" if anomalia else "Normal",
            "anomalia": anomalia,
            "direcao": direcao,
            "tem_base": anterior is not None,
        }
        anterior_por_instalacao[chave] = fatura

    return comparativos


def montar_altas_e_baixas(faturas, user=None, quantidade=5):
    """
    Consolida as maiores altas e quedas de consumo do periodo.

    Alimenta a tela de relatorio: em vez de obrigar a percorrer a tabela
    inteira comparando linha a linha, entrega direto o que subiu, o que caiu e
    quanto isso representou em dinheiro.
    """
    faturas = list(faturas)
    comparativos = montar_comparativos_por_id(faturas, user)
    unidades = unidade_map(user)

    itens = []
    for fatura in faturas:
        comparativo = comparativos.get(fatura.id) or {}
        if not comparativo.get("tem_base") or comparativo["direcao"] in ["sem_base", "estavel"]:
            continue

        unidade = unidades.get(fatura.instalacao)
        itens.append({
            "id": fatura.id,
            "unidade": unidade.nome if unidade else (fatura.cliente_nome or fatura.instalacao or "-"),
            "instalacao": fatura.instalacao or "-",
            "mes": mes_fatura(fatura),
            "direcao": comparativo["direcao"],
            "variacao_numero": comparativo["variacao_numero"],
            "variacao": comparativo["variacao"],
            "consumo_anterior": comparativo["consumo_anterior"],
            "consumo_atual": comparativo["consumo_atual"],
            "variacao_valor": comparativo["variacao_valor"],
            "diferenca_valor": comparativo["diferenca_valor"],
            "valor": formatar_valor_tabela(fatura.valor_pagar),
            "anomalia": comparativo["anomalia"],
        })

    altas = sorted(
        [i for i in itens if i["direcao"] == "alta"],
        key=lambda i: i["variacao_numero"], reverse=True,
    )
    baixas = sorted(
        [i for i in itens if i["direcao"] == "baixa"],
        key=lambda i: i["variacao_numero"],
    )
    estaveis = sum(
        1 for c in comparativos.values() if c.get("direcao") == "estavel"
    )

    return {
        "altas": altas[:quantidade],
        "baixas": baixas[:quantidade],
        "total_altas": len(altas),
        "total_baixas": len(baixas),
        "total_estaveis": estaveis,
        "com_anomalia": sum(1 for i in itens if i["anomalia"]),
        "maior_alta": altas[0] if altas else None,
        "maior_baixa": baixas[0] if baixas else None,
        # Sem base de comparacao: primeira fatura daquela instalacao.
        "sem_comparacao": sum(1 for c in comparativos.values() if not c.get("tem_base")),
    }


def _alertas_registrados(user):
    empresa = empresa_usuario(user)
    alertas = AlertaOperacional.objects.filter(resolvido=False).select_related("empresa", "unidade")
    if empresa:
        alertas = alertas.filter(empresa=empresa)
    elif user and getattr(user, "is_authenticated", False) and not getattr(user, "is_superuser", False):
        alertas = alertas.none()

    return [
        {
            "severidade": alerta.severidade,
            "titulo": alerta.titulo,
            "descricao": alerta.descricao,
            "tempo": timezone.localtime(alerta.criado_em).strftime("%d/%m %H:%M"),
        }
        for alerta in alertas[:8]
    ]


def montar_alertas(faturas=None, user=None):
    faturas = list(faturas if faturas is not None else faturas_base(user))
    alertas = _alertas_registrados(user)
    hoje = timezone.localdate()
    unidades = unidade_map(user)
    comparativos = montar_comparativos_por_id(faturas, user)

    for fatura in faturas:
        if fatura.data_vencimento:
            dias = (fatura.data_vencimento - hoje).days
            if 0 <= dias <= 7:
                unidade = unidades.get(fatura.instalacao)
                alertas.append({
                    "severidade": "medium",
                    "titulo": "Vencimento proximo",
                    "descricao": f"{unidade.nome if unidade else fatura.cliente_nome} vence em {dias} dia(s), no valor de {formatar_valor_tabela(fatura.valor_pagar)}.",
                    "tempo": f"{dias}d",
                })

        comparativo = comparativos.get(fatura.id)
        if comparativo and comparativo["anomalia"]:
            unidade = unidades.get(fatura.instalacao)
            alertas.append({
                "severidade": "high",
                "titulo": "Aumento anormal de consumo",
                "descricao": f"{unidade.nome if unidade else fatura.cliente_nome} subiu {comparativo['variacao']} em {mes_fatura(fatura)}.",
                "tempo": "Critico",
            })

    if not alertas:
        alertas.append({
            "severidade": "low",
            "titulo": "Leitor IMAP sincronizado",
            "descricao": "O robo de e-mail esta alimentando a base com sucesso.",
            "tempo": "Ativo",
        })

    return alertas[:12]


def _preparar_linhas_financeiras(linhas):
    maior = max((float(linha["total_numero"]) for linha in linhas), default=0) or 1
    for linha in linhas:
        linha["largura"] = max(4, int((float(linha["total_numero"]) / maior) * 100))
        linha["total"] = formatar_valor_tabela(linha["total_numero"])
        linha["consumo"] = f"{linha['consumo_numero']} kWh" if linha["consumo_numero"] else "-"
    return linhas


def montar_painel_financeiro(faturas, user=None):
    unidades = unidade_map(user)
    comparativos = montar_comparativos_por_id(faturas, user)
    por_mes = {}
    por_unidade = {}
    por_cliente = {}
    total_valor = Decimal("0")
    total_consumo = 0
    quantidade = 0

    def acumular(grupo, chave, rotulo, ordem, valor, consumo):
        if chave not in grupo:
            grupo[chave] = {
                "rotulo": rotulo,
                "ordem": ordem,
                "total_numero": Decimal("0"),
                "consumo_numero": 0,
                "faturas": 0,
            }
        grupo[chave]["total_numero"] += valor
        grupo[chave]["consumo_numero"] += consumo
        grupo[chave]["faturas"] += 1

    for fatura in faturas:
        valor = fatura.valor_pagar or Decimal("0")
        consumo = consumo_para_int(fatura.consumo_kwh)
        unidade = unidades.get(fatura.instalacao)
        unidade_nome = unidade.nome if unidade else (fatura.instalacao or "Sem unidade")
        cliente_nome = fatura.cliente_nome or "Sem cliente"
        vencimento = fatura.data_vencimento
        mes_chave = vencimento.strftime("%Y-%m") if vencimento else "0000-00"

        total_valor += valor
        total_consumo += consumo
        quantidade += 1
        acumular(por_mes, mes_chave, mes_fatura(fatura), mes_chave, valor, consumo)
        acumular(por_unidade, unidade_nome, unidade_nome, unidade_nome, valor, consumo)
        acumular(por_cliente, cliente_nome, cliente_nome, cliente_nome, valor, consumo)

    meses = sorted(por_mes.values(), key=lambda item: item["ordem"], reverse=True)[:12]
    unidades_linhas = sorted(por_unidade.values(), key=lambda item: item["total_numero"], reverse=True)
    clientes = sorted(por_cliente.values(), key=lambda item: item["total_numero"], reverse=True)
    alertas_consumo = sum(1 for item in comparativos.values() if item["anomalia"])

    return {
        "total": formatar_valor_tabela(total_valor),
        "consumo": f"{total_consumo} kWh",
        "ticket_medio": formatar_valor_tabela(total_valor / quantidade if quantidade else Decimal("0")),
        "alertas_consumo": alertas_consumo,
        "por_mes": _preparar_linhas_financeiras(meses),
        "por_unidade": _preparar_linhas_financeiras(unidades_linhas),
        "por_cliente": _preparar_linhas_financeiras(clientes),
    }


def montar_dados_unidades(user):
    unidades = list(unidades_gerenciaveis(user))
    faturas = list(faturas_base(user))
    faturas_por_instalacao = {}
    for fatura in faturas:
        faturas_por_instalacao.setdefault(fatura.instalacao, []).append(fatura)

    linhas = []
    for unidade in unidades:
        faturas_unidade = faturas_por_instalacao.get(unidade.instalacao, [])
        total = sum((fatura.valor_pagar or Decimal("0")) for fatura in faturas_unidade)
        consumo = sum(consumo_para_int(fatura.consumo_kwh) for fatura in faturas_unidade)
        ultima = faturas_unidade[0] if faturas_unidade else None
        linhas.append({
            "id": unidade.id,
            "nome": unidade.nome,
            "empresa": unidade.empresa.nome,
            "instalacao": unidade.instalacao,
            "email_automacao": unidade.email_automacao or "-",
            "tipo": unidade.get_tipo_display(),
            "limite": formatar_percentual(float(unidade.limite_aumento_percentual)),
            "contato": unidade.email_alerta or unidade.whatsapp_alerta or "-",
            "ativa": unidade.ativa,
            "total": formatar_valor_tabela(total),
            "consumo": f"{consumo} kWh" if consumo else "-",
            "ultima": ultima.data_vencimento.strftime("%d/%m/%Y") if ultima and ultima.data_vencimento else "-",
        })
    return linhas


def contatos_alerta(user):
    emails = set()
    whatsapps = set()
    empresa = empresa_usuario(user)
    empresas = [empresa] if empresa else Empresa.objects.filter(ativa=True)

    for item in empresas:
        if item.email_alerta:
            emails.add(item.email_alerta)
        if item.whatsapp_alerta:
            whatsapps.add(item.whatsapp_alerta)

    for unidade in unidades_visiveis(user):
        if unidade.email_alerta:
            emails.add(unidade.email_alerta)
        if unidade.whatsapp_alerta:
            whatsapps.add(unidade.whatsapp_alerta)

    return sorted(emails), sorted(whatsapps)


def montar_mensagem_alertas(alertas):
    linhas = ["Alertas do MonitorPro:"]
    for alerta in alertas:
        linhas.append(f"- {alerta['titulo']}: {alerta['descricao']}")
    return "\n".join(linhas)


def _consumo_numero(valor):
    try:
        return float(str(valor).replace(".", "").replace(",", "."))
    except (AttributeError, ValueError):
        return 0


def preparar_leituras_medidor(leituras):
    itens = []
    for leitura in leituras or []:
        grandeza = leitura.get("grandeza") or "-"
        itens.append({
            "medidor": leitura.get("medidor") or "-",
            "grandeza": grandeza,
            "posto": leitura.get("posto") or "-",
            "leitura_anterior": leitura.get("leitura_anterior") or "-",
            "leitura_atual": leitura.get("leitura_atual") or "-",
            "constante": leitura.get("constante") or "-",
            "consumo": leitura.get("consumo") or "-",
            "tipo": leitura.get("tipo") or ("energia" if "kWh" in grandeza else "demanda"),
        })

    maior_consumo = max((_consumo_numero(item["consumo"]) for item in itens), default=0) or 1
    for item in itens:
        consumo = _consumo_numero(item["consumo"])
        item["largura"] = max(6, int((consumo / maior_consumo) * 100)) if consumo else 6
    return itens


def preparar_historico_medidor(grupos):
    preparados = []
    for grupo in grupos or []:
        linhas_base = grupo.get("linhas") or []
        maior = max((_consumo_numero(linha.get("valor")) for linha in linhas_base), default=0) or 1
        linhas = []
        for linha in linhas_base:
            valor = linha.get("valor") or "-"
            consumo = _consumo_numero(valor)
            linhas.append({
                "mes": linha.get("mes") or "-",
                "valor": valor,
                "dias": linha.get("dias") or "-",
                "largura": max(4, int((consumo / maior) * 100)) if consumo else 4,
            })

        if linhas:
            preparados.append({
                "titulo": grupo.get("titulo") or "Consumo",
                "unidade": grupo.get("unidade") or "kWh",
                "tipo": grupo.get("tipo") or "energia",
                "linhas": linhas,
            })
    return preparados


def _dias_periodo_leitura(periodo):
    if not periodo or " a " not in periodo:
        return "-"
    try:
        inicio, fim = [parte.strip() for parte in periodo.split(" a ", 1)]
        data_inicio = datetime.strptime(inicio, "%d/%m/%Y").date()
        data_fim = datetime.strptime(fim, "%d/%m/%Y").date()
        return abs((data_fim - data_inicio).days) or "-"
    except ValueError:
        return "-"


def montar_historico_faturas_instalacao(fatura_atual, user=None):
    faturas = list(
        faturas_base(user)
        .filter(instalacao=fatura_atual.instalacao)
        .order_by("-data_vencimento", "-id")[:12]
    )
    itens = []
    for fatura in faturas:
        consumo = consumo_para_int(fatura.consumo_kwh)
        if not consumo:
            continue
        itens.append((
            mes_fatura(fatura),
            str(consumo),
            _dias_periodo_leitura(fatura.periodo_leitura),
        ))

    if not itens:
        return []

    grupo = {
        "titulo": "Consumo total",
        "unidade": "kWh",
        "tipo": "energia",
        "itens": itens,
    }
    maior = max(_consumo_numero(item[1]) for item in grupo["itens"]) or 1
    grupo["linhas"] = [
        {
            "mes": mes,
            "valor": valor,
            "dias": dias,
            "largura": max(4, int((_consumo_numero(valor) / maior) * 100)),
        }
        for mes, valor, dias in grupo["itens"]
    ]
    return [grupo]


def tela_login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    faturas = faturas_base(request.user)
    contexto = {
        "gasto_total": formatar_moeda(faturas.aggregate(soma=Sum("valor_pagar"))["soma"]),
        "ultima_fatura": formatar_moeda(faturas.first().valor_pagar if faturas.exists() else 0),
    }

    if request.method == "POST":
        # Teto de tamanho antes de qualquer processamento: sem isso, um
        # username de megabytes seria entregue ao hasher de senha, que e
        # deliberadamente lento — vira um vetor de exaustao de CPU.
        usuario_digitado = (request.POST.get("username") or "")[:150].strip()
        senha_digitada = (request.POST.get("password") or "")[:128]

        # Dois limites em camadas: um por IP de origem, outro por usuario
        # tentado. O primeiro barra varredura a partir de uma maquina; o
        # segundo barra ataque distribuido contra uma conta especifica.
        if not limite_negocio(request, "login", 10, 300):
            registrar_auditoria(request, "limite_excedido", "Limite de tentativas de login por origem")
            messages.error(request, "Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente.")
            return redirect("login")

        # O texto digitado nao entra na chave do cache nem na auditoria: quem
        # erra de campo digita a senha no lugar do usuario, e as duas ficam
        # gravadas no banco. O HMAC mantem um contador por conta sem guardar
        # o que foi digitado.
        conta_tentada = salted_hmac("monitor.login_conta", usuario_digitado.lower()).hexdigest()[:32]
        if usuario_digitado and not limite_negocio(
            request, f"login_conta:{conta_tentada}", 8, 900, por_usuario=False, rotulo="login por conta"
        ):
            registrar_auditoria(request, "limite_excedido", "Limite de tentativas para a conta informada")
            messages.error(request, "Muitas tentativas para esta conta. Aguarde alguns minutos.")
            return redirect("login")

        if not usuario_digitado or not senha_digitada:
            messages.error(request, "Informe usuario e senha.")
            return redirect("login")

        utilizador = authenticate(request, username=usuario_digitado, password=senha_digitada)

        if utilizador is not None:
            # login() rotaciona a chave da sessao (protecao contra session
            # fixation) — comportamento nativo do Django, mantido de proposito.
            login(request, utilizador)

            # "Manter conectado" existia na tela mas nao fazia nada: o campo
            # era enviado e ignorado, e a sessao encerrava ao fechar o
            # navegador de qualquer jeito. Agora a caixa decide de fato.
            if request.POST.get("remember_me"):
                request.session.set_expiry(settings.SESSION_COOKIE_AGE)
            else:
                request.session.set_expiry(0)  # expira ao fechar o navegador
            registrar_auditoria(
                request,
                "login_sucesso",
                "Login realizado com sucesso",
                empresa=empresa_usuario(utilizador),
                detalhes={"usuario": utilizador.username},
            )
            return redirect("dashboard")

        # Nao registra o texto digitado: quando alguem erra o campo e digita a
        # senha no lugar do usuario, esse valor iria para o banco em claro.
        # Guarda so o comprimento, que ja ajuda a distinguir erro humano de
        # varredura automatizada.
        registrar_auditoria(
            request,
            "login_falha",
            "Tentativa de login invalida",
            detalhes={"tamanho_usuario_informado": len(usuario_digitado)},
        )
        messages.error(request, "Usuario ou senha incorretos.")
        return redirect("login")

    return render(request, "monitor/login.html", contexto)


@login_required(login_url="login")
@require_POST
def fazer_logout(request):
    registrar_auditoria(request, "logout", "Logout realizado")
    logout(request)
    return redirect("login")


@login_required(login_url="login")
def tela_dashboard(request):
    faturas = faturas_base(request.user)
    resumo = montar_resumo(faturas)
    contexto = {
        **resumo,
        "account": conta_contexto(request.user),
        "aba_ativa": "dashboard",
        "grafico_dados": montar_grafico(faturas),
        "lista_faturas": montar_lista_faturas(faturas, request.user),
        "alertas": montar_alertas(faturas, request.user),
        **coleta_contexto(request),
    }
    return render(request, "monitor/dashboard.html", contexto)


@login_required(login_url="login")
def detalhe_fatura(request, id):
    fatura = get_object_or_404(faturas_base(request.user), id=id)
    unidade = unidade_map(request.user).get(fatura.instalacao)
    detalhe = FaturaCpflDetalhe.objects.filter(fatura_id=fatura.id).first()
    leituras_medidor = preparar_leituras_medidor(detalhe.leituras_medidor if detalhe else [])
    historico_medidor = preparar_historico_medidor(detalhe.historico_medidor if detalhe else [])
    if not historico_medidor:
        historico_medidor = montar_historico_faturas_instalacao(fatura, request.user)

    return render(request, "monitor/detalhe.html", {
        "account": conta_contexto(request.user),
        "fatura": fatura,
        "unidade": unidade,
        "leituras_medidor": leituras_medidor,
        "historico_medidor": historico_medidor,
    })


@login_required(login_url="login")
@require_POST
def coletar_agora(request):
    if not validar_permissao(request, pode_operar(request.user), "Tentativa de iniciar coleta sem permissao operacional"):
        return redirect("dashboard")
    if not validar_operacao_critica(request, "coleta", "coleta", 3, 600):
        return redirect("dashboard")

    log, controle = iniciar_coleta_com_trava(request.user)
    if not log:
        inicio = controle.iniciado_em.strftime("%d/%m/%Y %H:%M") if controle and controle.iniciado_em else "agora"
        usuario = nome_usuario(controle.iniciado_por) if controle and controle.iniciado_por else "outro usuario"
        registrar_auditoria(
            request,
            "coleta_bloqueada",
            f"Coleta bloqueada por execucao em andamento desde {inicio}",
            detalhes={"iniciada_por": usuario},
        )
        messages.warning(request, f"Ja existe uma coleta em andamento desde {inicio}, iniciada por {usuario}. Aguarde finalizar.")
        return redirect("dashboard")

    # A coleta nao roda mais dentro da requisicao. Antes, `executar_extracao()`
    # era chamado aqui: conexao IMAP, download, parsing com pdfplumber e envio
    # de e-mail, tudo bloqueando o worker HTTP. Duas consequencias praticas:
    # o navegador ficava girando por minutos, e quando o proxy cortava a
    # requisicao por timeout o `finally` nunca rodava — a trava global ficava
    # presa e ninguem mais conseguia coletar.
    #
    # Agora a view so registra a solicitacao e devolve na hora; quem executa
    # e o comando `coletar_faturas`, fora do ciclo de request.
    lote = fila.criar_lote(
        origem="imap",
        usuario=request.user,
        empresa=empresa_usuario(request.user),
    )
    log.mensagem = f"Coleta enfileirada (lote #{lote.id})"
    log.save(update_fields=["mensagem"])

    registrar_auditoria(
        request,
        "coleta_solicitada",
        "Coleta manual solicitada",
        alvo_tipo="robo_execucao",
        alvo_id=log.id,
        detalhes={"lote": lote.id},
    )

    messages.success(
        request,
        "Coleta agendada. O robo esta lendo a caixa de entrada em segundo plano — "
        "acompanhe o andamento em Coletas.",
    )
    return redirect("coletas")

@login_required(login_url="login")
@require_POST
def exportar_csv(request):
    if not limite_negocio(request, "exportacao", 12, 300):
        messages.warning(request, "Limite temporario de exportacoes atingido.")
        return redirect("relatorio_completo")
    faturas = list(faturas_base(request.user)[:5000])
    unidades = unidade_map(request.user)
    comparativos = montar_comparativos_por_id(faturas, request.user)

    carimbo = timezone.localtime().strftime("%Y%m%d_%H%M")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    # Nome com data: baixar duas vezes nao sobrescreve o arquivo anterior na
    # pasta de downloads, e da para comparar exportacoes de dias diferentes.
    response["Content-Disposition"] = f'attachment; filename="faturas_energia_{carimbo}.csv"'

    # BOM: sem ele o Excel em portugues abre UTF-8 como latin-1 e todo acento
    # vira caractere quebrado.
    response.write("\ufeff")

    # Somente linhas de dados, sem cabecalho de resumo nem totais no rodape:
    # qualquer linha que nao seja um registro atrapalha tabela dinamica e
    # filtro no Excel. O resumo consolidado esta no PDF.
    writer = csv.writer(response, delimiter=";")
    writer.writerow([
        "ID",
        "Empresa",
        "Cliente",
        "Unidade",
        "Tipo de conta",
        "Instalacao",
        "Mes/Ano",
        "Data de vencimento",
        "Valor (R$)",
        "Valor anterior (R$)",
        "Variacao valor (%)",
        "Diferenca valor (R$)",
        "Consumo (kWh)",
        "Consumo anterior (kWh)",
        "Variacao consumo (%)",
        "Direcao",
        "Status consumo",
        "Limite alerta",
        "Periodo de leitura",
    ])

    for fatura in faturas:
        unidade = unidades.get(fatura.instalacao)
        comparativo = comparativos.get(fatura.id, {})
        tem_base = comparativo.get("tem_base")

        writer.writerow([
            fatura.id,
            texto_csv(unidade.empresa.nome if unidade else conta_contexto(request.user)["empresa"]),
            texto_csv(fatura.cliente_nome),
            texto_csv(unidade.nome if unidade else ""),
            tipo_conta(fatura, unidade),
            texto_csv(fatura.instalacao),
            mes_fatura(fatura),
            fatura.data_vencimento.strftime("%d/%m/%Y") if fatura.data_vencimento else "",
            valor_csv(fatura.valor_pagar),
            valor_csv(comparativo.get("valor_anterior")) if tem_base else "",
            # Numero cru com virgula decimal: o Excel reconhece como numero e
            # permite ordenar e filtrar. Com "+23,1%" viraria texto.
            numero_csv(comparativo.get("variacao_valor_numero")) if tem_base else "",
            valor_csv(abs((fatura.valor_pagar or Decimal("0")) - comparativo.get("valor_anterior", Decimal("0")))) if tem_base else "",
            consumo_csv(fatura.consumo_kwh),
            comparativo.get("consumo_anterior") if tem_base else "",
            numero_csv(comparativo.get("variacao_numero")) if tem_base else "",
            rotulo_direcao(comparativo),
            comparativo.get("status") or "",
            comparativo.get("limite") or "",
            texto_csv(periodo_leitura(fatura)),
        ])

    registrar_auditoria(
        request,
        "exportacao_csv",
        f"Relatorio CSV exportado com {len(faturas)} fatura(s)",
        detalhes={"total_faturas": len(faturas)},
    )
    return response


@login_required(login_url="login")
@require_POST
def exportar_pdf(request):
    if not limite_negocio(request, "exportacao", 12, 300):
        messages.warning(request, "Limite temporario de exportacoes atingido.")
        return redirect("relatorio_completo")
    faturas = list(faturas_base(request.user)[:5000])
    unidades = unidade_map(request.user)
    comparativos = montar_comparativos_por_id(faturas, request.user)
    resumo_variacao = montar_altas_e_baixas(faturas, request.user, quantidade=3)
    empresa = empresa_usuario(request.user) or Empresa.objects.filter(ativa=True).order_by("id").first()
    total_faturas = len(faturas)
    total_gasto = sum((fatura.valor_pagar or Decimal("0")) for fatura in faturas)
    total_consumo = sum(consumo_para_int(fatura.consumo_kwh) for fatura in faturas)
    gerado_em = timezone.localtime().strftime("%d/%m/%Y %H:%M")
    carimbo = timezone.localtime().strftime("%Y%m%d_%H%M")

    buffer = io.BytesIO()
    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="faturas_energia_{carimbo}.pdf"'

    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=1.2 * cm,
        leftMargin=1.2 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.0 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "MonitorTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        textColor=colors.HexColor(empresa.cor_primaria if empresa else "#0f172a"),
        alignment=TA_CENTER,
        spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "MonitorSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        textColor=colors.HexColor("#64748b"),
        alignment=TA_CENTER,
        spaceAfter=14,
    )
    small_style = ParagraphStyle(
        "SmallCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7,
        leading=9,
        textColor=colors.HexColor("#0f172a"),
    )
    small_bold_style = ParagraphStyle(
        "SmallCellBold",
        parent=small_style,
        fontName="Helvetica-Bold",
    )
    header_cell_style = ParagraphStyle(
        "HeaderCell",
        parent=small_bold_style,
        textColor=colors.white,
    )

    story = [
        Paragraph(texto_pdf(empresa.nome) if empresa else "Relatorio CPFL", title_style),
        Paragraph(f"Relatorio de energia CPFL - gerado em {gerado_em}", subtitle_style),
    ]

    if empresa:
        story.append(Paragraph(
            f"Documento: {texto_pdf(empresa.documento)} | Alertas: {texto_pdf(empresa.email_alerta)} | WhatsApp: {texto_pdf(empresa.whatsapp_alerta)}",
            subtitle_style,
        ))

    resumo = Table(
        [
            ["Total de faturas", "Valor total", "Energia consumida", "Subiram", "Cairam", "Acima do limite"],
            [
                str(total_faturas),
                f"R$ {formatar_moeda(total_gasto)}",
                f"{total_consumo} kWh",
                str(resumo_variacao["total_altas"]),
                str(resumo_variacao["total_baixas"]),
                str(resumo_variacao["com_anomalia"]),
            ],
        ],
        colWidths=[4.2 * cm, 5.0 * cm, 4.6 * cm, 3.8 * cm, 3.8 * cm, 4.0 * cm],
    )
    resumo.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f8fafc")),
        ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#0f172a")),
        # Cor no numero de altas e quedas, alinhada com a tela.
        ("TEXTCOLOR", (3, 1), (3, 1), COR_ALTA),
        ("TEXTCOLOR", (4, 1), (4, 1), COR_BAIXA),
        ("TEXTCOLOR", (5, 1), (5, 1), COR_ALTA),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("FONTSIZE", (0, 1), (-1, 1), 12),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([resumo, Spacer(1, 0.4 * cm)])

    # --- Destaques: o que mais subiu e o que mais caiu -------------------
    # Quem recebe o relatorio quer saber primeiro onde doeu; percorrer a
    # tabela inteira comparando linha a linha e trabalho desnecessario.
    destaques = _linhas_destaque_pdf(resumo_variacao, small_style, small_bold_style)
    if destaques:
        story.extend(destaques)

    story.append(Paragraph("Faturas registradas", small_bold_style))
    story.append(Spacer(1, 0.15 * cm))

    tabela_dados = [[
        Paragraph("ID", header_cell_style),
        Paragraph("Cliente", header_cell_style),
        Paragraph("Unidade", header_cell_style),
        Paragraph("Vencimento", header_cell_style),
        Paragraph("Valor", header_cell_style),
        Paragraph("Var. valor", header_cell_style),
        Paragraph("Consumo", header_cell_style),
        Paragraph("Var. consumo", header_cell_style),
        Paragraph("Situacao", header_cell_style),
    ]]

    estilos_variacao = []
    for fatura in faturas:
        unidade = unidades.get(fatura.instalacao)
        comparativo = comparativos.get(fatura.id, {})
        tem_base = comparativo.get("tem_base")
        indice = len(tabela_dados)
        consumo = consumo_para_int(fatura.consumo_kwh)

        tabela_dados.append([
            Paragraph(f"#{fatura.id}", small_style),
            Paragraph(texto_pdf(fatura.cliente_nome), small_style),
            Paragraph(texto_pdf(unidade.nome if unidade else fatura.instalacao), small_style),
            Paragraph(fatura.data_vencimento.strftime("%d/%m/%Y") if fatura.data_vencimento else "-", small_style),
            Paragraph(formatar_valor_tabela(fatura.valor_pagar), small_style),
            Paragraph(comparativo.get("variacao_valor") if tem_base else "-", small_bold_style),
            Paragraph(f"{consumo} kWh" if consumo else "-", small_style),
            Paragraph(comparativo.get("variacao") if tem_base else "-", small_bold_style),
            Paragraph(rotulo_direcao(comparativo), small_style),
        ])

        if not tem_base:
            continue

        # Cor nas duas colunas de variacao. O texto da coluna Situacao ja diz
        # "Alta"/"Queda", entao o relatorio nao depende da cor para ser lido.
        direcao = comparativo.get("direcao")
        if direcao == "alta":
            cor = COR_ALTA
        elif direcao == "baixa":
            cor = COR_BAIXA
        else:
            cor = COR_NEUTRA
        estilos_variacao.append(("TEXTCOLOR", (7, indice), (7, indice), cor))

        variacao_valor = comparativo.get("variacao_valor_numero")
        if variacao_valor is not None:
            if variacao_valor > 1:
                cor_valor = COR_ALTA
            elif variacao_valor < -1:
                cor_valor = COR_BAIXA
            else:
                cor_valor = COR_NEUTRA
            estilos_variacao.append(("TEXTCOLOR", (5, indice), (5, indice), cor_valor))

        # Faixa de destaque na linha que passou do limite configurado.
        if comparativo.get("anomalia"):
            estilos_variacao.append(
                ("BACKGROUND", (0, indice), (-1, indice), colors.HexColor("#fef2f2"))
            )

    tabela = Table(
        tabela_dados,
        colWidths=[1.1 * cm, 4.6 * cm, 4.2 * cm, 2.3 * cm, 2.5 * cm, 2.2 * cm, 2.3 * cm, 2.4 * cm, 3.8 * cm],
        repeatRows=1,
    )
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dbe4ef")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        # Depois das regras gerais, para que a cor da variacao prevaleca.
        *estilos_variacao,
    ]))
    story.append(tabela)

    doc.build(story, onFirstPage=_numerar_paginas, onLaterPages=_numerar_paginas)
    response.write(buffer.getvalue())
    buffer.close()
    registrar_auditoria(
        request,
        "exportacao_pdf",
        f"Relatorio PDF exportado com {len(faturas)} fatura(s)",
        detalhes={"total_faturas": len(faturas)},
    )
    return response


@login_required(login_url="login")
def relatorio_completo(request):
    # Materializa uma vez: a lista alimenta a tabela, os comparativos e o
    # resumo de altas e baixas. Sem isso, o mesmo SELECT rodaria tres vezes.
    faturas = list(faturas_base(request.user))
    comparativos = montar_comparativos_por_id(faturas, request.user)

    # O comparativo entra em cada linha aqui, e nao no template: o template do
    # Django nao indexa dicionario por variavel sem um filtro proprio, e um
    # filtro so para isso e mais peca para manter do que valor entregue.
    linhas = montar_lista_faturas(faturas, request.user)
    for linha in linhas:
        linha["comp"] = comparativos.get(linha["id"], {})

    contexto = {
        "account": conta_contexto(request.user),
        "aba_ativa": "relatorio",
        "lista_faturas": linhas,
        "total_gasto": formatar_valor_tabela(sum((f.valor_pagar or Decimal("0")) for f in faturas)),
        "total_faturas": len(faturas),
        "total_consumo": sum(consumo_para_int(fatura.consumo_kwh) for fatura in faturas),
        "comparativos": comparativos,
        "resumo_variacao": montar_altas_e_baixas(faturas, request.user),
    }
    return render(request, "monitor/relatorio.html", contexto)


@login_required(login_url="login")
def pagina_padrao(request, titulo, subtitulo, aba, itens=None):
    return render(request, "monitor/pagina_padrao.html", {
        "account": conta_contexto(request.user),
        "titulo": titulo,
        "subtitulo": subtitulo,
        "aba_ativa": aba,
        "itens": itens or [],
        **coleta_contexto(request),
    })


@login_required(login_url="login")
def historico(request):
    return pagina_padrao(
        request,
        "Historico",
        "Linha do tempo das faturas e coletas registradas.",
        "historico",
        montar_lista_faturas(faturas_base(request.user)[:10], request.user),
    )


@login_required(login_url="login")
def alertas(request):
    faturas = faturas_base(request.user)
    alertas_ativos = montar_alertas(faturas, request.user)
    emails, whatsapps = contatos_alerta(request.user)
    return render(request, "monitor/alertas.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "alertas",
        "alertas": alertas_ativos,
        "total_alertas": len(alertas_ativos),
        "emails": emails,
        "whatsapps": whatsapps,
        "token_alertas": novo_token_operacao(request, "alertas") if pode_operar(request.user) else "",
    })


@login_required(login_url="login")
@require_POST
def enviar_alertas(request):
    if not validar_permissao(request, pode_operar(request.user), "Tentativa de enviar alertas sem permissao operacional"):
        return redirect("alertas")
    if not validar_operacao_critica(request, "alertas", "envio_alertas", 3, 900):
        return redirect("alertas")

    alertas_ativos = montar_alertas(faturas_base(request.user), request.user)
    emails, whatsapps = contatos_alerta(request.user)
    mensagem = montar_mensagem_alertas(alertas_ativos)
    enviados = 0

    try:
        if emails:
            send_mail(
                "Alertas do MonitorPro",
                mensagem,
                getattr(settings, "DEFAULT_FROM_EMAIL", "monitorpro@localhost"),
                emails,
                fail_silently=False,
            )
            enviados += len(emails)

        webhook = getattr(settings, "WHATSAPP_WEBHOOK_URL", "") or os.getenv("WHATSAPP_WEBHOOK_URL", "")
        if whatsapps and webhook:
            # Antes a URL ia direta para o urlopen. urlopen aceita esquemas
            # como file:// e segue redirects, entao uma variavel mal
            # preenchida (ou alterada por quem tenha acesso ao ambiente)
            # transformava esta chamada em leitura de arquivo local ou em
            # acesso ao metadata service da nuvem. Agora o destino e validado
            # antes: exige https e recusa endereco de rede interna.
            try:
                webhook = validar_url_webhook(webhook)
            except DestinoInvalido as erro:
                registrar_auditoria(
                    request,
                    "operacao_bloqueada",
                    "Webhook de WhatsApp recusado pela politica de saida",
                    detalhes={"motivo": str(erro)},
                )
                messages.error(request, f"Webhook de WhatsApp invalido: {erro}")
                return redirect("alertas")

            payload = json.dumps({"telefones": whatsapps, "mensagem": mensagem}).encode("utf-8")
            requisicao = urllib.request.Request(
                webhook,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # Sem seguir redirect: um 302 levaria a requisicao para um destino
            # que nao passou pela validacao acima.
            enviar_sem_redirecionar(requisicao, timeout=12)
            enviados += len(whatsapps)
    except Exception as erro:
        messages.error(request, f"Nao foi possivel enviar os alertas: {erro}")
        return redirect("alertas")

    if enviados:
        registrar_auditoria(
            request,
            "alertas_enviados",
            f"Alertas enviados para {enviados} contato(s)",
            detalhes={"emails": len(emails), "whatsapps": len(whatsapps)},
        )
        messages.success(request, f"Alertas enviados para {enviados} contato(s).")
    elif whatsapps and not webhook:
        messages.warning(request, "WhatsApp nao enviado: configure WHATSAPP_WEBHOOK_URL no ambiente.")
    else:
        messages.warning(request, "Nenhum e-mail ou WhatsApp de alerta cadastrado.")

    return redirect("alertas")


@login_required(login_url="login")
def conformidade(request):
    return pagina_padrao(
        request,
        "Conformidade",
        "Resumo operacional das faturas acompanhadas.",
        "conformidade",
        [
            {"cliente": "Coleta automatica", "vencimento": "Controle ativo", "valor": "OK", "consumo": "-", "status": "Monitorado", "status_cor": "green"},
            {"cliente": "Exportacao CSV", "vencimento": "Disponivel", "valor": "OK", "consumo": "-", "status": "Ativo", "status_cor": "green"},
        ],
    )


@login_required(login_url="login")
def coletas(request):
    if not validar_permissao(request, pode_operar(request.user), "Tentativa de acessar coletas sem permissao operacional"):
        return redirect("dashboard")
    empresa = empresa_usuario(request.user)
    logs = RoboExecucaoLog.objects.select_related("empresa", "unidade")
    if empresa:
        logs = logs.filter(empresa=empresa)
    elif not getattr(request.user, "is_superuser", False):
        logs = logs.none()

    return render(request, "monitor/coletas.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "coletas",
        "lista_faturas": montar_lista_faturas(faturas_base(request.user)[:20], request.user),
        "logs": logs[:8],
        **coleta_contexto(request),
    })


@login_required(login_url="login")
def configuracoes(request):
    if not validar_permissao(request, pode_administrar(request.user), "Tentativa de acessar configuracoes sem permissao administrativa"):
        return redirect("dashboard")
    # criar=True apenas no POST: abrir a tela nao deve gravar nada.
    empresa = empresa_operacional(request.user, criar=(request.method == "POST"))
    if request.method == "POST":
        if not validar_operacao_critica(request, "configuracoes", "configuracoes", 10, 600):
            return redirect("configuracoes")
        form = EmpresaForm(request.POST, instance=empresa)
        if form.is_valid():
            anterior = snapshot_campos(empresa, form.fields.keys())
            empresa = form.save()
            alterados = registrar_alteracoes(request, empresa, anterior, rotulo=empresa.nome)
            registrar_auditoria(
                request,
                "configuracao_empresa",
                "Identidade da empresa atualizada",
                empresa=empresa,
                alvo_tipo="empresa",
                alvo_id=empresa.id,
                detalhes={"campos_alterados": alterados},
            )
            messages.success(request, "Identidade da empresa atualizada.")
            return redirect("configuracoes")
    else:
        form = EmpresaForm(instance=empresa)

    return render(request, "monitor/configuracoes.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "configuracoes",
        "form": form,
        "empresa": empresa,
        "operation_token": novo_token_operacao(request, "configuracoes"),
    })


@login_required(login_url="login")
def unidades(request):
    if not validar_permissao(request, pode_administrar(request.user), "Tentativa de acessar unidades sem permissao administrativa"):
        return redirect("dashboard")
    linhas = montar_dados_unidades(request.user)
    return render(request, "monitor/unidades.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "unidades",
        "unidades": linhas,
        "total_unidades": len(linhas),
        "ativas": sum(1 for linha in linhas if linha["ativa"]),
    })


@login_required(login_url="login")
def unidade_nova(request):
    if not validar_permissao(request, pode_administrar(request.user), "Tentativa de criar unidade sem permissao administrativa"):
        return redirect("dashboard")
    if request.method == "POST":
        if not validar_operacao_critica(request, "unidade_form", "unidades", 20, 600):
            return redirect("unidades")
        form = UnidadeConsumidoraForm(request.POST)
        if form.is_valid():
            empresa = empresa_operacional(request.user, criar=True)
            limite_unidades = getattr(settings, "MONITOR_MAX_UNIDADES_EMPRESA", 500)
            if empresa and UnidadeConsumidora.objects.filter(empresa=empresa).count() >= limite_unidades:
                registrar_auditoria(request, "limite_excedido", "Limite de unidades por empresa excedido", empresa=empresa, detalhes={"limite": limite_unidades})
                messages.error(request, "Limite de unidades consumidoras atingido para esta empresa.")
                return redirect("unidades")
            unidade = form.save(commit=False)
            unidade.empresa = empresa
            unidade.save()
            registrar_auditoria(
                request,
                "unidade_criada",
                f"Unidade {unidade.nome} cadastrada",
                empresa=unidade.empresa,
                alvo_tipo="unidade",
                alvo_id=unidade.id,
                detalhes={"instalacao": unidade.instalacao, "email_automacao": unidade.email_automacao},
            )
            messages.success(request, "Unidade consumidora cadastrada.")
            return redirect("unidades")
    else:
        form = UnidadeConsumidoraForm()

    return render(request, "monitor/unidade_form.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "unidades",
        "form": form,
        "titulo": "Nova unidade",
        "subtitulo": "Cadastre instalacoes CPFL para separar dados por cliente.",
        "operation_token": novo_token_operacao(request, "unidade_form"),
    })


@login_required(login_url="login")
def unidade_editar(request, id):
    if not validar_permissao(request, pode_administrar(request.user), "Tentativa de editar unidade sem permissao administrativa"):
        return redirect("dashboard")
    unidade = get_object_or_404(unidades_gerenciaveis(request.user), id=id)
    if request.method == "POST":
        if not validar_operacao_critica(request, "unidade_form", "unidades", 20, 600):
            return redirect("unidade_editar", id=unidade.id)
        form = UnidadeConsumidoraForm(request.POST, instance=unidade)
        if form.is_valid():
            # Fotografa antes do save para que RegistroAlteracao guarde de
            # onde cada campo saiu, nao so onde chegou.
            anterior = snapshot_campos(unidade, form.fields.keys())
            unidade = form.save()
            alterados = registrar_alteracoes(request, unidade, anterior, rotulo=unidade.nome)
            registrar_auditoria(
                request,
                "unidade_editada",
                f"Unidade {unidade.nome} atualizada",
                empresa=unidade.empresa,
                alvo_tipo="unidade",
                alvo_id=unidade.id,
                detalhes={"instalacao": unidade.instalacao, "campos_alterados": alterados},
            )
            messages.success(request, "Unidade consumidora atualizada.")
            return redirect("unidades")
    else:
        form = UnidadeConsumidoraForm(instance=unidade)

    return render(request, "monitor/unidade_form.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "unidades",
        "form": form,
        "titulo": unidade.nome,
        "subtitulo": f"Instalacao {unidade.instalacao}",
        "operation_token": novo_token_operacao(request, "unidade_form"),
    })


@login_required(login_url="login")
def financeiro(request):
    faturas = faturas_base(request.user)
    return render(request, "monitor/financeiro.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "financeiro",
        "financeiro": montar_painel_financeiro(faturas, request.user),
    })


@login_required(login_url="login")
def logs_robo(request):
    if not validar_permissao(request, pode_operar(request.user), "Tentativa de acessar logs sem permissao operacional"):
        return redirect("dashboard")
    empresa = empresa_usuario(request.user)
    logs = RoboExecucaoLog.objects.select_related("empresa", "unidade")
    eventos = AuditoriaEvento.objects.select_related("usuario", "empresa")
    if empresa:
        logs = logs.filter(empresa=empresa)
        eventos = eventos.filter(empresa=empresa)
    elif not getattr(request.user, "is_superuser", False):
        logs = logs.none()
        eventos = eventos.none()

    return render(request, "monitor/logs.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "logs",
        "logs": logs[:25],
        # A lista so aparece para superusuario. O arquivo de backup contem
        # dados de todas as empresas; ate a existencia dele nao interessa ao
        # administrador de um cliente especifico.
        "backups": BackupRegistro.objects.all()[:10] if pode_administrar_sistema(request.user) else [],
        "pode_administrar_sistema": pode_administrar_sistema(request.user),
        "eventos_auditoria": eventos[:25],
        "token_backup": novo_token_operacao(request, "backup") if pode_administrar_sistema(request.user) else "",
        **coleta_contexto(request),
    })


@login_required(login_url="login")
@require_POST
def backup_dados(request):
    if not validar_permissao(
        request,
        pode_administrar_sistema(request.user),
        "Tentativa de criar backup sem permissao de sistema",
    ):
        return redirect("logs_robo")
    if not validar_operacao_critica(request, "backup", "backup", 2, 1800):
        return redirect("logs_robo")

    pasta = Path(settings.BASE_DIR) / "backups"
    pasta.mkdir(exist_ok=True)
    nome = f"backup_monitor_{timezone.localtime().strftime('%Y%m%d_%H%M%S')}.json"
    caminho = pasta / nome

    with caminho.open("w", encoding="utf-8") as arquivo:
        call_command(
            "dumpdata",
            "monitor.Empresa",
            "monitor.PerfilUsuario",
            "monitor.UnidadeConsumidora",
            "monitor.AlertaOperacional",
            "monitor.RoboExecucaoLog",
            "monitor.AuditoriaEvento",
            "monitor.BackupRegistro",
            "monitor.FaturaCpfl",
            "monitor.FaturaCpflDetalhe",
            indent=2,
            stdout=arquivo,
        )

    backup = BackupRegistro.objects.create(
        arquivo=str(caminho),
        tamanho_bytes=caminho.stat().st_size,
        detalhes="Backup JSON gerado pelo painel.",
    )
    registrar_auditoria(
        request,
        "backup_criado",
        "Backup JSON criado pelo painel",
        alvo_tipo="backup",
        alvo_id=backup.id,
        detalhes={"arquivo": str(caminho), "tamanho_bytes": backup.tamanho_bytes},
    )
    messages.success(request, "Backup criado com sucesso.")
    return redirect("logs_robo")


@login_required(login_url="login")
def upload_faturas(request):
    """
    Envio manual de faturas em PDF.

    Cada arquivo e validado, gravado com nome saneado e enfileirado. O
    processamento nao acontece aqui: a resposta volta assim que os arquivos
    estao na fila, e o worker cuida do resto. Um PDF recusado nao impede os
    outros do mesmo lote.
    """
    if not validar_permissao(request, pode_operar(request.user), "Tentativa de enviar faturas sem permissao operacional"):
        return redirect("dashboard")

    if request.method == "POST":
        if not validar_operacao_critica(request, "upload", "upload", 10, 600):
            return redirect("upload_faturas")

        form = UploadFaturasForm(request.POST, request.FILES)
        if form.is_valid():
            resultado = _receber_arquivos(request, form.cleaned_data["arquivos"])
            return redirect("lote_detalhe", id=resultado.id)
    else:
        form = UploadFaturasForm()

    lotes = (
        LoteProcessamento.objects
        .filter(origem="upload")
        .select_related("usuario")
        .prefetch_related("arquivos")
    )
    empresa = empresa_usuario(request.user)
    if empresa:
        lotes = lotes.filter(empresa=empresa)
    elif not getattr(request.user, "is_superuser", False):
        lotes = lotes.none()

    return render(request, "monitor/upload.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "upload",
        "form": form,
        "lotes": lotes[:10],
        "limite_arquivos": getattr(settings, "PDF_ARQUIVOS_POR_LOTE", 20),
        "limite_mb": getattr(settings, "PDF_TAMANHO_MAXIMO_MB", 25),
        "operation_token": novo_token_operacao(request, "upload"),
    })


def _receber_arquivos(request, arquivos):
    """Grava e enfileira os PDFs, registrando individualmente os recusados."""
    lote = fila.criar_lote(
        origem="upload",
        usuario=request.user,
        empresa=empresa_operacional(request.user, criar=True),
        total=len(arquivos),
    )

    destino = Path(getattr(settings, "PDFS_RECEBIDOS_DIR", Path(settings.BASE_DIR) / "pdfs_recebidos"))
    aceitos = 0
    recusados = 0
    # Hashes ja aceitos nesta remessa. Sem isso, selecionar o mesmo PDF duas
    # vezes (ou o mesmo conteudo com dois nomes) enfileirava os dois: o
    # segundo so seria descartado quando o primeiro terminasse de gravar.
    vistos_no_lote = set()

    for arquivo in arquivos:
        try:
            # nome_arquivo_seguro descarta qualquer componente de caminho no
            # nome enviado; sem isso um upload chamado "../../settings.py"
            # escreveria fora do diretorio de destino.
            conteudo = arquivo.read()
            caminho = gravar_arquivo_seguro(destino, arquivo.name, conteudo)
        except Exception as erro:
            fila.registrar_recusa(lote, arquivo.name, f"Falha ao gravar: {erro}")
            recusados += 1
            continue

        try:
            # Validacao profunda: paginas, PDF protegido por senha, corrupcao.
            meta = validar_pdf(caminho)
        except PdfInvalido as erro:
            fila.registrar_recusa(lote, arquivo.name, str(erro))
            caminho.unlink(missing_ok=True)
            recusados += 1
            continue

        if meta["sha256"] in vistos_no_lote:
            fila.registrar_recusa(lote, arquivo.name, "Conteudo identico a outro arquivo deste mesmo envio.")
            caminho.unlink(missing_ok=True)
            recusados += 1
            continue

        ja_existe = FaturaCpflDetalhe.objects.filter(pdf_sha256=meta["sha256"]).first()
        if ja_existe:
            fila.registrar_recusa(
                lote,
                arquivo.name,
                f"Arquivo identico ja processado (fatura #{ja_existe.fatura_id}).",
            )
            caminho.unlink(missing_ok=True)
            recusados += 1
            continue

        vistos_no_lote.add(meta["sha256"])
        fila.enfileirar(lote, caminho, arquivo.name, meta)
        aceitos += 1

    registrar_auditoria(
        request,
        "upload_faturas",
        f"Upload de {len(arquivos)} arquivo(s): {aceitos} na fila, {recusados} recusado(s)",
        alvo_tipo="lote",
        alvo_id=lote.id,
        detalhes={"aceitos": aceitos, "recusados": recusados, "total": len(arquivos)},
    )

    if aceitos:
        messages.success(request, f"{aceitos} arquivo(s) na fila de processamento.")
    if recusados:
        messages.warning(request, f"{recusados} arquivo(s) recusado(s). Veja o motivo de cada um abaixo.")

    return lote


@login_required(login_url="login")
def lote_detalhe(request, id):
    lote = get_object_or_404(_lotes_visiveis(request.user), id=id)
    return render(request, "monitor/lote_detalhe.html", {
        "account": conta_contexto(request.user),
        "aba_ativa": "upload",
        "lote": lote,
        "arquivos": lote.arquivos.all(),
    })


@login_required(login_url="login")
def lote_progresso(request, id):
    """
    Progresso do lote em JSON, consumido pela pagina para atualizar sozinha.

    Devolve estado por arquivo — nao so um percentual agregado — porque o que
    interessa a quem enviou 20 faturas e saber qual delas falhou e por que.
    """
    lote = get_object_or_404(_lotes_visiveis(request.user), id=id)
    arquivos = lote.arquivos.all()

    return JsonResponse({
        "id": lote.id,
        "status": lote.status,
        "status_rotulo": lote.get_status_display(),
        "total": lote.total_arquivos,
        "concluidos": lote.concluidos,
        "percentual": lote.percentual,
        "finalizado": bool(lote.finalizado_em),
        "arquivos": [
            {
                "id": arquivo.id,
                "nome": arquivo.nome_original,
                "status": arquivo.status,
                "status_rotulo": arquivo.get_status_display(),
                "mensagem": arquivo.mensagem,
                "tentativas": arquivo.tentativas,
                "paginas": arquivo.paginas,
                "tamanho_kb": round(arquivo.tamanho_bytes / 1024, 1) if arquivo.tamanho_bytes else 0,
            }
            for arquivo in arquivos
        ],
    })


def _lotes_visiveis(user):
    lotes = LoteProcessamento.objects.prefetch_related("arquivos")
    if not user or not user.is_authenticated:
        return lotes.none()
    if getattr(user, "is_superuser", False):
        return lotes
    empresa = empresa_usuario(user)
    return lotes.filter(empresa=empresa) if empresa else lotes.none()


def em_construcao(request):
    return render(request, "monitor/pagina_publica.html", {
        "titulo": "Pagina em construcao",
        "subtitulo": "Esta area ainda nao foi configurada.",
    })
