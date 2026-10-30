from django.conf import settings
from django.db import models


class Empresa(models.Model):
    nome = models.CharField(max_length=160)
    documento = models.CharField(max_length=30, blank=True)
    email_alerta = models.EmailField(blank=True)
    whatsapp_alerta = models.CharField(max_length=30, blank=True)
    logo_url = models.URLField(blank=True)
    cor_primaria = models.CharField(max_length=20, default="#3b96f5")
    ativa = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class PerfilUsuario(models.Model):
    PAPEL_CHOICES = [
        ("admin", "Administrador"),
        ("operador", "Operador"),
        ("leitura", "Leitura"),
    ]

    usuario = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="perfil_monitor")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="usuarios")
    papel = models.CharField(max_length=20, choices=PAPEL_CHOICES, default="admin")
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "perfil de usuario"
        verbose_name_plural = "perfis de usuario"

    def __str__(self):
        return f"{self.usuario} - {self.empresa}"


class UnidadeConsumidora(models.Model):
    TIPO_CHOICES = [
        ("residencial", "Residencial"),
        ("comercial", "Comercial"),
        ("industrial", "Industrial"),
        ("outro", "Outro"),
    ]

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="unidades")
    nome = models.CharField(max_length=160)
    instalacao = models.CharField(max_length=50, unique=True)
    email_automacao = models.EmailField(blank=True, db_index=True)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default="residencial")
    endereco = models.CharField(max_length=255, blank=True)
    limite_aumento_percentual = models.DecimalField(max_digits=6, decimal_places=2, default=20)
    email_alerta = models.EmailField(blank=True)
    whatsapp_alerta = models.CharField(max_length=30, blank=True)
    ativa = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["empresa__nome", "nome"]
        verbose_name = "unidade consumidora"
        verbose_name_plural = "unidades consumidoras"

    def __str__(self):
        return f"{self.nome} ({self.instalacao})"


class AlertaOperacional(models.Model):
    SEVERIDADE_CHOICES = [
        ("low", "Baixa"),
        ("medium", "Media"),
        ("high", "Alta"),
    ]

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="alertas", null=True, blank=True)
    unidade = models.ForeignKey(UnidadeConsumidora, on_delete=models.SET_NULL, null=True, blank=True, related_name="alertas")
    fatura_id = models.IntegerField(null=True, blank=True)
    tipo = models.CharField(max_length=60)
    titulo = models.CharField(max_length=160)
    descricao = models.TextField()
    severidade = models.CharField(max_length=20, choices=SEVERIDADE_CHOICES, default="low")
    resolvido = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]

    def __str__(self):
        return self.titulo


class RoboExecucaoLog(models.Model):
    STATUS_CHOICES = [
        ("processando", "Processando"),
        ("sucesso", "Sucesso"),
        ("aviso", "Aviso"),
        ("erro", "Erro"),
    ]

    empresa = models.ForeignKey(Empresa, on_delete=models.SET_NULL, null=True, blank=True)
    unidade = models.ForeignKey(UnidadeConsumidora, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    mensagem = models.CharField(max_length=255)
    detalhes = models.TextField(blank=True)
    iniciado_em = models.DateTimeField(auto_now_add=True)
    finalizado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-iniciado_em"]
        verbose_name = "log do robo"
        verbose_name_plural = "logs do robo"

    def __str__(self):
        return f"{self.status} - {self.mensagem}"


class ColetaControle(models.Model):
    chave = models.CharField(max_length=40, unique=True, default="global")
    em_andamento = models.BooleanField(default=False)
    iniciado_em = models.DateTimeField(null=True, blank=True)
    finalizado_em = models.DateTimeField(null=True, blank=True)
    iniciado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    log = models.ForeignKey(RoboExecucaoLog, on_delete=models.SET_NULL, null=True, blank=True)
    mensagem = models.CharField(max_length=255, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["chave"]
        verbose_name = "controle de coleta"
        verbose_name_plural = "controles de coleta"

    def __str__(self):
        status = "em andamento" if self.em_andamento else "liberada"
        return f"{self.chave} - {status}"


class AuditoriaEvento(models.Model):
    ACAO_CHOICES = [
        ("login_sucesso", "Login realizado"),
        ("login_falha", "Falha de login"),
        ("logout", "Logout"),
        ("coleta_solicitada", "Coleta solicitada"),
        ("coleta_bloqueada", "Coleta bloqueada"),
        ("coleta_finalizada", "Coleta finalizada"),
        ("exportacao_csv", "Exportacao CSV"),
        ("exportacao_pdf", "Exportacao PDF"),
        ("configuracao_empresa", "Configuracao da empresa"),
        ("unidade_criada", "Unidade criada"),
        ("unidade_editada", "Unidade editada"),
        ("alertas_enviados", "Alertas enviados"),
        ("backup_criado", "Backup criado"),
        ("backup_restaurado", "Backup restaurado"),
        ("acesso_negado", "Acesso negado"),
        ("limite_excedido", "Limite excedido"),
        ("operacao_bloqueada", "Operacao bloqueada"),
        ("upload_faturas", "Upload de faturas"),
        ("expurgo_arquivos", "Expurgo de arquivos"),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.SET_NULL, null=True, blank=True)
    acao = models.CharField(max_length=60, choices=ACAO_CHOICES)
    resumo = models.CharField(max_length=255)
    alvo_tipo = models.CharField(max_length=80, blank=True)
    alvo_id = models.CharField(max_length=80, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    detalhes = models.JSONField(default=dict, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]
        verbose_name = "evento de auditoria"
        verbose_name_plural = "eventos de auditoria"

    def __str__(self):
        return f"{self.get_acao_display()} - {self.resumo}"

class BackupRegistro(models.Model):
    status = models.CharField(max_length=30, default="criado")
    arquivo = models.CharField(max_length=255)
    tamanho_bytes = models.BigIntegerField(default=0)
    criado_em = models.DateTimeField(auto_now_add=True)
    detalhes = models.TextField(blank=True)

    class Meta:
        ordering = ["-criado_em"]

    def __str__(self):
        return self.arquivo


class FaturaCpflDetalhe(models.Model):
    fatura_id = models.IntegerField(unique=True)
    instalacao = models.CharField(max_length=50, db_index=True)
    data_vencimento = models.DateField()
    leituras_medidor = models.JSONField(default=list, blank=True)
    historico_medidor = models.JSONField(default=list, blank=True)

    # Procedencia do arquivo que originou a fatura. O SHA-256 serve para tres
    # coisas: evitar reprocessar o mesmo PDF, detectar adulteracao (mesmo
    # arquivo com hash diferente) e provar de qual documento veio cada valor.
    pdf_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    pdf_nome_original = models.CharField(max_length=255, blank=True)
    pdf_tamanho_bytes = models.BigIntegerField(default=0)
    pdf_paginas = models.IntegerField(default=0)

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-data_vencimento", "-fatura_id"]
        verbose_name = "detalhe de fatura CPFL"
        verbose_name_plural = "detalhes de faturas CPFL"

    def __str__(self):
        return f"Detalhes fatura #{self.fatura_id} - {self.instalacao}"


class RegistroAlteracao(models.Model):
    """
    Trilha de auditoria com valor anterior.

    AuditoriaEvento responde "quem fez o que e quando". Este modelo responde
    "o que exatamente mudou": para cada campo alterado, guarda o valor antes
    e depois. Sem isso nao da para reconstruir o historico de um dado critico
    nem contestar uma alteracao indevida.
    """

    OPERACAO_CHOICES = [
        ("criacao", "Criacao"),
        ("alteracao", "Alteracao"),
        ("exclusao", "Exclusao"),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.SET_NULL, null=True, blank=True)
    modelo = models.CharField(max_length=80, db_index=True)
    objeto_id = models.CharField(max_length=80, db_index=True)
    rotulo_objeto = models.CharField(max_length=200, blank=True)
    operacao = models.CharField(max_length=20, choices=OPERACAO_CHOICES)
    campo = models.CharField(max_length=80)
    valor_anterior = models.TextField(blank=True)
    valor_novo = models.TextField(blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]
        verbose_name = "registro de alteracao"
        verbose_name_plural = "registros de alteracao"
        indexes = [
            models.Index(fields=["modelo", "objeto_id", "-criado_em"]),
        ]

    def __str__(self):
        return f"{self.modelo}#{self.objeto_id}.{self.campo}: {self.valor_anterior!r} -> {self.valor_novo!r}"


class LoteProcessamento(models.Model):
    """Agrupa os PDFs enviados de uma vez, para mostrar progresso do conjunto."""

    ORIGEM_CHOICES = [
        ("upload", "Upload manual"),
        ("imap", "Caixa de e-mail"),
    ]
    STATUS_CHOICES = [
        ("pendente", "Na fila"),
        ("processando", "Processando"),
        ("concluido", "Concluido"),
        ("concluido_com_erros", "Concluido com erros"),
        ("erro", "Erro"),
    ]

    origem = models.CharField(max_length=20, choices=ORIGEM_CHOICES, default="upload")
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default="pendente", db_index=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="lotes", null=True, blank=True)
    total_arquivos = models.IntegerField(default=0)
    criado_em = models.DateTimeField(auto_now_add=True)
    finalizado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-criado_em"]
        verbose_name = "lote de processamento"
        verbose_name_plural = "lotes de processamento"

    def __str__(self):
        return f"Lote #{self.id} ({self.get_origem_display()}) - {self.total_arquivos} arquivo(s)"

    @property
    def concluidos(self):
        return self.arquivos.filter(status__in=["concluido", "erro", "recusado"]).count()

    @property
    def percentual(self):
        if not self.total_arquivos:
            return 0
        return int((self.concluidos / self.total_arquivos) * 100)


class ArquivoProcessamento(models.Model):
    """
    Um PDF na fila de processamento.

    A fila vive no proprio Postgres em vez de Redis/Celery: o volume de
    faturas nao justifica outro servico para manter no ar, e SELECT ... FOR
    UPDATE SKIP LOCKED ja da entrega exclusiva por worker com seguranca.
    """

    STATUS_CHOICES = [
        ("pendente", "Na fila"),
        ("processando", "Processando"),
        ("concluido", "Concluido"),
        ("erro", "Erro"),
        ("recusado", "Recusado na validacao"),
    ]

    lote = models.ForeignKey(LoteProcessamento, on_delete=models.CASCADE, related_name="arquivos")
    nome_original = models.CharField(max_length=255)
    caminho = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    tamanho_bytes = models.BigIntegerField(default=0)
    paginas = models.IntegerField(default=0)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pendente", db_index=True)
    tentativas = models.IntegerField(default=0)
    max_tentativas = models.IntegerField(default=3)
    # Backoff exponencial: o worker so reconsidera o item depois deste horario.
    proxima_tentativa_em = models.DateTimeField(null=True, blank=True, db_index=True)

    mensagem = models.CharField(max_length=500, blank=True)
    resumo = models.JSONField(default=dict, blank=True)

    criado_em = models.DateTimeField(auto_now_add=True)
    iniciado_em = models.DateTimeField(null=True, blank=True)
    finalizado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "arquivo em processamento"
        verbose_name_plural = "arquivos em processamento"
        indexes = [
            models.Index(fields=["status", "proxima_tentativa_em"]),
        ]

    def __str__(self):
        return f"{self.nome_original} ({self.get_status_display()})"

    @property
    def terminou(self):
        return self.status in ["concluido", "erro", "recusado"]


class FaturaCpfl(models.Model):
    # Mapeamento dos campos que o robô insere na base de dados
    cliente_nome = models.CharField(max_length=255)
    data_vencimento = models.DateField()
    valor_pagar = models.DecimalField(max_digits=10, decimal_places=2)
    instalacao = models.CharField(max_length=50)
    consumo_kwh = models.CharField(max_length=50)
    periodo_leitura = models.CharField(max_length=100)
    pago = models.BooleanField(default=False)

    class Meta:
        db_table = 'faturas_cpfl'  # Nome exato da tabela utilizada pelo robô
        managed = False  # Indica ao Django para não tentar criar ou alterar esta tabela via migrações

    def __str__(self):
        return f"{self.cliente_nome} - {self.data_vencimento}"
