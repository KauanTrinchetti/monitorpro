# Deploy do MonitorPro

> Detalhes das correcoes de seguranca e integridade estao em [SEGURANCA.md](SEGURANCA.md).

## Variaveis de ambiente

Copie `.env.example` para `.env` e preencha. **O `.env` nunca deve ser
versionado** (ja esta no `.gitignore`).

A aplicacao se recusa a subir com `DEBUG=False` se faltar `SECRET_KEY`,
`ALLOWED_HOSTS` ou `DB_PASSWORD` — de proposito: e melhor falhar no boot que
subir silenciosamente num modo inseguro.

Gere a chave de producao (diferente da usada em desenvolvimento) com:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Atencao a duas variaveis novas:

- `ATRAS_DE_PROXY_CONFIAVEL` — marque `True` **apenas** se houver nginx,
  Cloudflare ou similar na frente reescrevendo os cabecalhos. Se ficar `True`
  sem proxy, um cliente pode forjar `X-Forwarded-For` e burlar o rate limit.
- `PDF_TAMANHO_MAXIMO_MB`, `PDF_PAGINAS_MAXIMAS`, `PDF_TIMEOUT_SEGUNDOS` —
  limites do pipeline de PDF.

## Passos no servidor

1. Subir o projeto em um VPS, Render, Railway, DigitalOcean ou outro provedor com Python e PostgreSQL.
2. `pip install -r requirements.txt`
3. `python manage.py migrate`
4. `python manage.py createcachetable` — cria a tabela do rate limit compartilhado.
5. `python manage.py collectstatic`
6. `python manage.py createsuperuser`
7. Apontar o dominio para o servidor.
8. Configurar HTTPS com certificado valido **antes** de subir com `DEBUG=False`
   (o HSTS de 1 ano e dificil de reverter no navegador de quem ja acessou).
9. `python manage.py check --deploy` — deve passar sem avisos.
10. Agendar as rotinas abaixo.
11. Conferir a tela `Logs e backups` depois da primeira coleta.

> Nunca rode `popular_demo` no servidor: ele cria usuarios de demonstracao,
> inclusive um superusuario. O comando ja se recusa com `DEBUG=False`.

## Rotinas agendadas

Em VPS Linux use cron; em Windows Server, o Agendador de Tarefas.

**Coleta de e-mail — a cada 1h**
```bash
python manage.py coletar_faturas --limite 25
```

**Worker da fila de PDFs — a cada 5min**
```bash
python manage.py processar_fila --uma-passada --concorrencia 2
```

**Backup com rotacao — diario, de madrugada**
```bash
python manage.py backup_dados --manter 14
```

**Expurgo de PDFs de fatura — semanal**
```bash
python manage.py expurgar_pdfs --dias 90 --incluir-tmp --confirmar
```

> O comando antigo (`shell -c "...executar_extracao()"`) nao deve mais ser
> usado: rodava o parsing dentro do processo do painel e travava a coleta
> global quando era interrompido.

## Restauracao de backup

Ensaie periodicamente — backup que nunca foi restaurado nao e backup:

```bash
python manage.py restaurar_backup backups/backup_monitor_XXXX.json.gz --ensaio
```

O ensaio confere o checksum SHA-256 contra o registrado na geracao e lista o
que seria restaurado, sem gravar nada. Para restaurar de verdade, troque
`--ensaio` por `--confirmar`.

## Checklist comercial

- Cadastrar uma `Empresa` para cada cliente.
- Cadastrar as `Unidades consumidoras` de cada cliente.
- Em cada unidade, preencher `Instalacao` com o numero CPFL e `Email automacao` com o alias que recebera a fatura.
- Criar uma regra no e-mail do cliente para encaminhar mensagens da CPFL ao alias da unidade.
- Vincular cada usuario ao cliente correto no admin, usando `PerfilUsuario`.
- Configurar e-mail e WhatsApp de alerta na empresa ou na unidade.
- Preencher logo, cor e dados da empresa em `Configuracoes`.
- Testar `Exportar PDF`, `Exportar CSV`, `Gerar backup` e `Enviar alertas`.
