from django.contrib import admin
from django.shortcuts import redirect

from .models import (
    AlertaOperacional,
    AuditoriaEvento,
    BackupRegistro,
    ColetaControle,
    Empresa,
    FaturaCpfl,
    FaturaCpflDetalhe,
    PerfilUsuario,
    RoboExecucaoLog,
    UnidadeConsumidora,
)


def _login_pela_tela_principal(request, extra_context=None):
    """
    Substitui o formulario de login do proprio admin.

    O login do admin do Django nao passa pelo rate limit nem pela auditoria
    da tela /login/: era uma segunda porta, sem tranca, para forca bruta
    contra justamente as contas mais privilegiadas. Agora toda tentativa vai
    para a tela principal. Quem ja esta logado como superusuario continua
    entrando no admin normalmente — so o formulario daqui deixou de existir.
    """
    return redirect("login")


admin.site.login = _login_pela_tela_principal


@admin.register(Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    list_display = ("nome", "documento", "email_alerta", "whatsapp_alerta", "ativa")
    search_fields = ("nome", "documento", "email_alerta")
    list_filter = ("ativa",)


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ("usuario", "empresa", "papel", "criado_em")
    search_fields = ("usuario__username", "usuario__email", "empresa__nome")
    list_filter = ("papel", "empresa")


@admin.register(UnidadeConsumidora)
class UnidadeConsumidoraAdmin(admin.ModelAdmin):
    list_display = ("nome", "instalacao", "email_automacao", "empresa", "tipo", "limite_aumento_percentual", "ativa")
    search_fields = ("nome", "instalacao", "email_automacao", "empresa__nome")
    list_filter = ("tipo", "ativa", "empresa")


@admin.register(FaturaCpfl)
class FaturaCpflAdmin(admin.ModelAdmin):
    list_display = ("id", "cliente_nome", "instalacao", "data_vencimento", "valor_pagar", "consumo_kwh")
    search_fields = ("cliente_nome", "instalacao")
    list_filter = ("data_vencimento",)


@admin.register(FaturaCpflDetalhe)
class FaturaCpflDetalheAdmin(admin.ModelAdmin):
    list_display = ("fatura_id", "instalacao", "data_vencimento", "atualizado_em")
    search_fields = ("fatura_id", "instalacao")
    list_filter = ("data_vencimento",)


@admin.register(AlertaOperacional)
class AlertaOperacionalAdmin(admin.ModelAdmin):
    list_display = ("titulo", "empresa", "unidade", "tipo", "severidade", "resolvido", "criado_em")
    search_fields = ("titulo", "descricao", "empresa__nome", "unidade__nome")
    list_filter = ("tipo", "severidade", "resolvido", "empresa")


@admin.register(RoboExecucaoLog)
class RoboExecucaoLogAdmin(admin.ModelAdmin):
    list_display = ("status", "mensagem", "empresa", "unidade", "iniciado_em", "finalizado_em")
    search_fields = ("mensagem", "detalhes", "empresa__nome", "unidade__nome")
    list_filter = ("status", "empresa")


@admin.register(AuditoriaEvento)
class AuditoriaEventoAdmin(admin.ModelAdmin):
    list_display = ("criado_em", "acao", "usuario", "empresa", "resumo", "ip")
    search_fields = ("resumo", "usuario__username", "usuario__email", "empresa__nome", "ip")
    list_filter = ("acao", "empresa", "criado_em")
    readonly_fields = ("criado_em",)


@admin.register(ColetaControle)
class ColetaControleAdmin(admin.ModelAdmin):
    list_display = ("chave", "em_andamento", "iniciado_por", "iniciado_em", "finalizado_em", "mensagem")
    search_fields = ("chave", "mensagem", "iniciado_por__username")
    list_filter = ("em_andamento",)


@admin.register(BackupRegistro)
class BackupRegistroAdmin(admin.ModelAdmin):
    list_display = ("arquivo", "status", "tamanho_bytes", "criado_em")
    search_fields = ("arquivo", "detalhes")
    list_filter = ("status",)