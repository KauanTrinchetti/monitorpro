from django.urls import path
from . import views


urlpatterns = [
    # Nossas telas principais
    path('login/', views.tela_login, name='login'),
    path('', views.tela_dashboard, name='dashboard'),
    path('logout/', views.fazer_logout, name='logout'),
    
    # As rotas fantasmas para o HTML não quebrar
    path('historico/', views.historico, name='historico'),
    path('alertas/', views.alertas, name='alertas'),
    path('alertas/enviar/', views.enviar_alertas, name='enviar_alertas'),
    path('conformidade/', views.conformidade, name='conformidade'),
    path('coletas/', views.coletas, name='coletas'),
    path('logs/', views.logs_robo, name='logs_robo'),
    path('backup/', views.backup_dados, name='backup_dados'),
    # Upload manual de faturas em PDF
    path('faturas/upload/', views.upload_faturas, name='upload_faturas'),
    path('faturas/lote/<int:id>/', views.lote_detalhe, name='lote_detalhe'),
    path('faturas/lote/<int:id>/progresso/', views.lote_progresso, name='lote_progresso'),

    path('unidades/', views.unidades, name='unidades'),
    path('unidades/nova/', views.unidade_nova, name='unidade_nova'),
    path('unidades/<int:id>/editar/', views.unidade_editar, name='unidade_editar'),
    path('financeiro/', views.financeiro, name='financeiro'),
    path('configuracoes/', views.configuracoes, name='configuracoes'),
    path('coletar_agora/', views.coletar_agora, name='coletar_agora'),
    path('exportar_pdf/', views.exportar_pdf, name='exportar_pdf'),
    path('password_reset/', views.em_construcao, name='password_reset'),
    path('exportar-csv/', views.exportar_csv, name='exportar_csv'),
    path('relatorio/', views.relatorio_completo, name='relatorio_completo'),
    path('fatura/<int:id>/', views.detalhe_fatura, name='detalhe_fatura'),
]
