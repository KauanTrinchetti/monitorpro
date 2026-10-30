# Endurecimento de segurança e integridade — MonitorPro

Relatório técnico das revisões de segurança: 06/08/2026 (seções 1 a 4),
10/08/2026 (seção 5) e 30/10/2026, antes da publicação do código (seção 6).
Base: Django 6.0.6 + PostgreSQL, app `monitor`, robô IMAP de leitura de faturas.

> Visão geral e mapa das defesas no [README](../README.md). Tratamento de dados
> pessoais em [LGPD.md](LGPD.md).

---

## 1. Vulnerabilidades corrigidas

Severidade pelo impacto real neste sistema, não por CVSS teórico.

### 🔴 Críticas

#### C1 — Path traversal com escrita arbitrária de arquivo
**Onde:** `robo_leitor_cpfl.py`, download do anexo IMAP.

O nome do anexo vinha do e-mail e era concatenado direto no caminho de
gravação: `Path(os.getcwd()) / parte.get_filename()`. Como qualquer pessoa
pode enviar e-mail para o alias da automação, um anexo chamado
`../../core/settings.py` sobrescrevia código da aplicação. Era o pior risco
do sistema — escrita arbitrária de arquivo por remetente não autenticado.

**Correção:** `seguranca.nome_arquivo_seguro()` descarta qualquer componente
de diretório, normaliza acentos, filtra por lista de caracteres permitidos,
trata nomes reservados do Windows (`CON`, `LPT1`) e acrescenta sufixo
aleatório. `caminho_dentro_de()` confirma a contenção após resolver symlinks.
A gravação foi confinada a `pdfs_recebidos/`, em vez do diretório de trabalho
do processo (que variava conforme de onde o robô era iniciado).

**Verificado:** 8 payloads de travessia neutralizados; gravação confinada.

#### C2 — SECRET_KEY com fallback embutido no código
**Onde:** `core/settings.py:33` (versão anterior).

`os.environ.get('SECRET_KEY', 'django-insecure-...')`. Sem a variável de
ambiente, a aplicação subia com a chave que está no repositório. Quem tivesse
acesso ao código forjava cookies de sessão e tokens de reset de senha —
login como qualquer usuário, incluindo administradores.

**Correção:** sem fallback. Em produção (`DEBUG=False`) a ausência levanta
`ImproperlyConfigured` e a aplicação se recusa a subir. Em desenvolvimento,
gera uma chave efêmera por boot — que invalida sessões antigas de propósito,
para não virar chave "de produção" por acidente.

#### C3 — DEBUG ligado por padrão
**Onde:** `core/settings.py:36` (versão anterior).

`DEBUG = os.environ.get('DEBUG', 'True')`. Variável faltando em produção =
página de erro do Django expondo settings (com senha do banco), consultas SQL
e caminhos do sistema de arquivos.

**Correção:** padrão `False`. Ligar exige ação explícita no ambiente.
`ALLOWED_HOSTS` vazio com `DEBUG=False` também aborta a inicialização.

#### C4 — Segredos a um `git init` de vazarem
Não havia `.gitignore`. Na raiz estavam `.env` (com `SENHA_APP` do Gmail e
`DB_PASSWORD`) e `db.sqlite3`. O primeiro `git add .` levaria tudo — e
segredo commitado permanece no histórico mesmo depois de removido do arquivo.

**Correção:** `.gitignore` cobrindo segredos, banco, backups, PDFs de clientes
e logs. `.env.example` como modelo sem valores. `requirements.txt` com versões
fixadas (não existia; o deploy era irreprodutível).

#### C5 — Credenciais em código-fonte
`robo_login.py`, `robo_coleta.py` e `teste_alerta.py` traziam e-mails reais e
campos de senha inline (`password=""`, `senha_app=""`). Eram scripts de
estudo — apontavam para `quotes.toscrape.com` e uma tabela `metricas_conta`
inexistente — mas o padrão convidava a preencher segredo no arquivo.

**Correção:** movidos para `scripts_legado/` com um LEIA-ME explicando por quê.
Nenhum é importado pela aplicação; a pasta pode ser apagada.

---

### 🟠 Altas

#### A1 — Rate limit de login contornável por cabeçalho forjado
`ip_cliente()` confiava em `X-Forwarded-For` incondicionalmente. Sem proxy na
frente, qualquer cliente envia o cabeçalho que quiser: trocando o valor a cada
tentativa, o limite de 10/5min nunca disparava. Também envenenava a trilha de
auditoria com origens falsas.

**Correção:** o cabeçalho só é considerado quando `ATRAS_DE_PROXY_CONFIAVEL=True`,
e mesmo assim usa o **último** salto (escrito pelo proxy), não o primeiro (que
o cliente controla).

**Verificado:** 12 tentativas alternando `X-Forwarded-For` continuam bloqueadas.

#### A2 — Contador de rate limit por processo
Sem `CACHES` configurado, o Django usava `LocMemCache`: cada worker mantinha o
próprio contador. Com N workers o limite efetivo era 10×N, e reiniciar a
aplicação zerava tudo.

**Correção:** `DatabaseCache` compartilhado (`monitor_cache`). Acrescentado um
segundo limite, por conta alvo (8 tentativas/15min), que barra ataque
distribuído contra um usuário específico.

**Verificado:** brute force bloqueado na 9ª tentativa.

#### A3 — Nenhum cabeçalho de segurança, HTTPS não forçado
Faltavam HSTS, redirecionamento SSL, `Secure`/`SameSite` nos cookies, CSP e
Permissions-Policy. O cookie de sessão trafegava em claro se alguém acessasse
por HTTP.

**Correção:** `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` (1 ano com preload),
`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SameSite=Lax`,
`CSRF_USE_SESSIONS`, `X_FRAME_OPTIONS=DENY`, expiração de sessão em 8h com
renovação por atividade. `CabecalhosDeSegurancaMiddleware` acrescenta CSP,
Permissions-Policy e `Cache-Control: no-store` nas telas autenticadas.

**Verificado:** todos presentes na resposta; `manage.py check --deploy` limpo.

#### A4 — SSRF no webhook de WhatsApp
`WHATSAPP_WEBHOOK_URL` ia direto para `urllib.request.urlopen()`. `urlopen`
aceita `file://` e segue redirects — uma variável mal preenchida virava leitura
de arquivo local ou acesso ao metadata service da nuvem.

**Correção:** `validar_url_webhook()` exige `https`, resolve o host e recusa
endereços privados, loopback, link-local, reservados e multicast.

**Verificado:** 8 destinos maliciosos bloqueados, incluindo `169.254.169.254`.

#### A5 — Processamento de PDF dentro da requisição HTTP
`coletar_agora` chamava `executar_extracao()` de forma síncrona: IMAP +
download + pdfplumber + SMTP, tudo bloqueando o worker HTTP. Quando o proxy
cortava por timeout, o `finally` não rodava e `ColetaControle.em_andamento`
ficava travado em `True` **permanentemente** — ninguém mais conseguia coletar
sem editar o banco à mão.

**Correção:** a view apenas enfileira e responde. A execução migrou para o
comando `coletar_faturas`. `liberar_coleta_travada()` solta automaticamente
qualquer trava com mais de 45 minutos.

#### A6 — Zero validação do PDF antes do parsing
Bastava o nome do anexo terminar em `.pdf`. Sem magic bytes, sem limite de
tamanho ou páginas, sem detecção de PDF protegido por senha, sem timeout.

**Correção:** `validar_pdf()` confere, na ordem do mais barato para o mais
caro: existência → tamanho → assinatura `%PDF-` → abertura do documento →
contagem de páginas. Limites configuráveis (25 MB, 60 páginas por padrão).

**Verificado:** executável renomeado para `.pdf`, arquivo vazio, acima do
limite e corrompido — todos recusados.

#### A7 — Race condition duplicando faturas
`salvar_no_banco()` fazia `SELECT` e depois `INSERT`, sem transação nem lock.
Duas execuções simultâneas liam "não existe" ao mesmo tempo e inseriam a mesma
fatura duas vezes. O código comentava que a tabela "deve ter" uma constraint
`UNIQUE`, mas nada garantia — e a tabela é `managed=False`.

**Correção:** `INSERT ... ON CONFLICT (instalacao, data_vencimento) DO UPDATE`,
resolvido atomicamente pelo Postgres. Migration `0008` garante a constraint em
ambientes novos e **falha alto** se encontrar duplicatas, em vez de apagar
dados por conta própria.

#### A8 — Gravação dividida em duas transações
A fatura commitava, os detalhes commitavam depois. Falha na segunda etapa
deixava a fatura sem detalhes, o robô devolvia `sucesso=False`, o e-mail
voltava para reprocessamento — divergência entre o gravado e o exibido.

**Correção:** `salvar_fatura_completa()` faz as duas escritas numa transação
só. Ou as duas entram, ou nenhuma entra.

---

### 🟡 Médias

| ID | Correção |
|---|---|
| M1 | SHA-256 de cada PDF gravado em `FaturaCpflDetalhe`. Deduplica reprocessamento e detecta adulteração. |
| M2 | `RegistroAlteracao`: trilha campo a campo com valor anterior e novo, ligada às edições de empresa e unidade. |
| M3 | Nome, e-mail, CPF/CNPJ e instalação mascarados em log. `limpar_detalhes()` remove senha/token da auditoria — antes, quem digitasse a senha no campo de usuário a via gravada em claro. |
| M4 | `backup_dados` com compressão, checksum e rotação; `restaurar_backup` com modo `--ensaio` que valida sem gravar. |
| M5 | `requirements.txt` com 22 dependências fixadas. |
| M6 | `pdfs_falha/`, `pdfs_recebidos/`, `backups/` e `db.sqlite3` no `.gitignore`. PDF processado com sucesso é apagado do disco. |
| M7 | Login trunca usuário em 150 e senha em 128 caracteres antes do hasher, e recusa campos vazios. |

### 🔵 Interface e acessibilidade

- **Contraste:** `--texto-fraco` media **3,14:1** contra o fundo — reprovava no
  WCAG AA. Passou para `#6b83a0` = **4,92:1**. Os 8 tokens de cor foram
  medidos no navegador: todos passam.
- **Design system:** CSS único em `monitor/static/monitor/css/monitorpro.css`
  com tokens de cor, tipografia (escala 1,2), espaçamento (base 4px) e forma.
  Antes o CSS estava embutido nos 15 templates — nada reaproveitado nem
  cacheável.
- **Responsividade mobile-first:** breakpoints em 640px (tablet) e 1024px
  (desktop). Antes só 4 de 15 templates tinham media query, todas em 820px.
- **Tabelas:** 11 tabelas envolvidas em `.tabela-rolavel`. Sem isso, uma
  tabela de 9 colunas fazia a **página inteira** rolar lateralmente no celular.
- **ARIA:** o projeto tinha **zero** atributos `aria-*` e zero `alt`. Foram
  adicionados `aria-current`, `aria-live`, `aria-labelledby`, `role`,
  `scope` em 59 cabeçalhos, link "pular para o conteúdo" e `aria-hidden` nos
  ícones decorativos.
- **Teclado:** `:focus-visible` com contorno de 2px em tudo que recebe foco.
- **Alvos de toque:** mínimo de 44px no celular (WCAG 2.5.5), compactando
  para 36px no desktop, onde o apontador é preciso.
- **Movimento:** `prefers-reduced-motion` respeitado.
- **Estados:** carregamento, erro e sucesso explícitos no upload, com barra de
  progresso e motivo individual por arquivo recusado.

---

## 2. Decisões de arquitetura

### Fila no PostgreSQL, não Celery/Redis
O volume é de dezenas de faturas por rodada, não milhares por segundo. Um
broker adicional é mais um serviço para manter no ar, e no Windows o Celery
ainda exige `--pool=solo`. O Postgres já está aqui, já é transacional, e
`SELECT ... FOR UPDATE SKIP LOCKED` dá entrega exclusiva por worker com a
mesma garantia neste porte. A troca, se um dia se justificar, fica contida em
`fila.reivindicar_proximo()` e no management command.

### Processo filho para o parsing, não thread
O timeout só é real se der para matar quem travou — uma thread presa dentro do
pdfplumber não pode ser interrompida pelo Python. Cada PDF roda num processo
`spawn` que o worker mata ao estourar o tempo. Efeito colateral bem-vindo: um
PDF que estoure a memória derruba só o filho.

> A função alvo mora em `monitor/worker_pdf.py`, **sem imports de Django no
> nível de módulo**. No modo `spawn` o filho re-importa o módulo que define a
> função: se fosse o management command, os models seriam importados antes do
> `django.setup()` do filho. Isso quebrou na primeira execução e foi corrigido.

### Falhar fechado no rate limit, falhar aberto na auditoria
Cache indisponível **nega** a operação — sem contador não há proteção contra
brute force. Já uma falha ao gravar auditoria não bloqueia o usuário: perder
um registro é melhor que derrubar o sistema.

### Validação em duas camadas
Barreira rápida no formulário (tamanho, magic bytes) e profunda no worker
(páginas, senha, corrupção). O JavaScript da página é só conveniência — a
validação do servidor roda de novo em tudo, inclusive num POST que não passe
pela tela.

### CSP estrita, JavaScript externo
`script-src 'self'`, sem `unsafe-inline`. Isso quebrou os scripts inline das
telas novas; a correção foi externalizar o JS e passar configuração por
`data-*`, não afrouxar a política. Mantém a defesa contra XSS e torna o
JavaScript cacheável.

### Fuso horário
Estava `UTC` com `LANGUAGE_CODE='en-us'`, o que fazia todo horário do painel
aparecer 3 horas adiantado. Corrigido para `America/Sao_Paulo` e `pt-br`.

---

## 3. Checklist de testes

### 3.1 Segurança — automatizado
Script de verificação da auditoria — **32/32 aprovados**. A cobertura foi
depois incorporada aos testes do projeto (`monitor/tests.py`, seção 5).

- [x] 8 payloads de path traversal neutralizados (`../`, `..\`, absoluto, nome reservado do Windows, byte nulo)
- [x] Gravação confinada ao diretório base após resolver symlinks
- [x] Executável (`MZ`) renomeado para `.pdf` recusado
- [x] Arquivo vazio recusado
- [x] Arquivo acima do limite recusado
- [x] PDF corrompido recusado
- [x] 8 destinos SSRF bloqueados (`file://`, `gopher://`, `169.254.169.254`, loopback IPv4/IPv6, rede privada)
- [x] Nome, e-mail e CPF mascarados
- [x] Senha e token removidos da auditoria, inclusive aninhados
- [x] Brute force bloqueado na 9ª tentativa
- [x] `X-Forwarded-For` forjado não reseta o limite
- [x] SHA-256 estável, distinto e com 64 caracteres

### 3.2 Segurança — checklist manual de implantação
- [ ] Confirmar que `DEBUG=False` no `.env` do **servidor** (`True` só em desenvolvimento)
- [ ] Gerar `SECRET_KEY` **diferente** da local para produção
- [ ] `ATRAS_DE_PROXY_CONFIAVEL=True` **apenas** se houver nginx/Cloudflare na frente
- [ ] Certificado HTTPS válido antes de ativar HSTS (1 ano é difícil de reverter)
- [ ] `pip install argon2-cffi` para ativar o hasher Argon2id (hoje cai em PBKDF2, que é seguro)

### 3.3 Rotas
Varredura de rotas da auditoria — **12/12 aprovadas**, com `Cache-Control: no-store` nas telas autenticadas e todos os cabeçalhos de segurança presentes. Hoje coberta por `RenderizacaoTest` e `CabecalhosDeSegurancaTest`.

### 3.4 Responsividade — medido no navegador
| Largura | Layout | Rolagem horizontal | Alvos < 44px |
|---|---|---|---|
| 375px (celular) | coluna única | **não** | 0 |
| 760px (tablet) | 2 colunas de KPI | **não** | 0 |
| 1272px (desktop) | grid com lateral fixa de 260px | **não** | 0 (36px, adequado a mouse) |

- [x] Tabela larga rola dentro do próprio container, não na página
- [x] Todos os 8 tokens de cor passam no WCAG AA
- [x] Nenhum SVG sem rótulo acessível
- [x] Nenhum script inline (CSP estrita respeitada)
- [x] CSS e JS servidos como estáticos com 200

### 3.5 Upload em lote — ponta a ponta
- [x] Executável disfarçado de PDF barrado no formulário
- [x] Nome com `../../` gravado como `settings_2fe8d9db.pdf` dentro de `pdfs_recebidos/`
- [x] Zero arquivos gravados fora do diretório base
- [x] SHA-256 e contagem de páginas calculados
- [x] Endpoint de progresso retorna estado por arquivo
- [x] Worker processou 2 PDFs em paralelo
- [x] Falha do worker gerou retry com backoff, sem derrubar o lote
- [x] Dados pessoais mascarados no log do worker
- [x] Lote fechado em `concluido` 2/2 (100%)

### 3.6 Backup
- [x] Backup gerado: 82 registros, 4,6 KB comprimido, com SHA-256
- [x] Ensaio de restauração confirma checksum e lista 7 modelos
- [x] Restauração real roda dentro de transação e exige `--confirmar`

### 3.7 Coleta IMAP — testada ponta a ponta em 03/09/2026
- [x] Conexão IMAP com a caixa de automação
- [x] E-mail com fatura anexa enviado e coletado
- [x] Nome do anexo saneado (`fatura_cpfl_teste.pdf` → `fatura_cpfl_teste_7e95b523.pdf`)
- [x] PDF validado: 2 páginas, 1211 KB, SHA-256 conferido
- [x] Dados extraídos e gravados (instalação, valor e consumo conferidos com a fatura)
- [x] Nome do titular mascarado no log (formato `M****A D* S***A`)
- [x] Gravação como UPDATE, sem duplicar (9 faturas antes e depois)
- [x] Reenvio do mesmo arquivo: parsing pulado por checksum idêntico
- [x] Mensagem movida para `processados`; PDF apagado do disco
- [x] Mensagens sem anexo permanecem na caixa de entrada

### 3.8 Testes ainda não feitos
- [ ] Envio real de webhook de WhatsApp
- [ ] Restauração real (`--confirmar`) — só o ensaio foi executado
- [ ] Teste com leitor de tela (NVDA/JAWS)
- [ ] Carga com dezenas de PDFs simultâneos

---

## 4. O que fazer agora

### Passos obrigatórios no servidor
```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createcachetable
python manage.py collectstatic
```

### Agendamento (Agendador de Tarefas do Windows)
```bash
python manage.py coletar_faturas --limite 25
```
```bash
python manage.py processar_fila --uma-passada --concorrencia 2
```
```bash
python manage.py backup_dados --manter 14
```

Sugestão: coleta a cada 1h, fila a cada 5min, backup diário de madrugada.

### Ensaiar a restauração periodicamente
```bash
python manage.py restaurar_backup backups/backup_monitor_XXXX.json.gz --ensaio
```

---

## 5. Segunda rodada — 10/08/2026

Quatro pendências da entrega anterior foram fechadas.

### Argon2id ativado
`pip install argon2-cffi`. O hasher passou de PBKDF2 para Argon2id, confirmado
em teste. O Django re-hasheia a senha de cada usuário no próximo login.

### Expurgo automático de arquivos de fatura
Novo comando `expurgar_pdfs`. Fatura de cliente é dado pessoal e não deve
ficar em disco indefinidamente — o dado relevante já está no banco, com
controle de acesso. Varre `pdfs_falha/`, `pdfs_recebidos/` e opcionalmente
`tmp/pdfs/`, com retenção configurável (90 dias por padrão), auditoria do que
foi removido e **simulação por padrão**: apagar exige `--confirmar`.

Agendamento sugerido (semanal):
```bash
python manage.py expurgar_pdfs --dias 90 --incluir-tmp --confirmar
```

> Na primeira execução, os arquivos existentes estavam dentro do prazo e
> **não foram apagados**. Para antecipar o expurgo, reduza o prazo
> (`--dias 30`).

### CSS inline eliminado e CSP apertada
547 linhas migradas para o design system: `login.html` (306), `detalhe.html`
(167), `dashboard.html` (67), `pagina_publica.html` (7). Todas reescritas
mobile-first. O `<style>` do `base.html` foi removido.

Com nenhum `<style>` inline restante, a CSP ficou granular:

| Diretiva | Valor | Efeito |
|---|---|---|
| `style-src-elem` | `'self' fonts.googleapis.com` | **bloqueia** bloco `<style>` injetado |
| `style-src-attr` | `'unsafe-inline'` | permite `style="width: N%"` das barras |
| `style-src` | com `'unsafe-inline'` | reserva para navegador antigo |

Separar as diretivas fecha o vetor real — um `<style>` injetado — sem quebrar
o atributo, que não executa script. **10/10 páginas** sem estilo ou script
inline.

### Testes movidos para o projeto
`monitor/tests.py` com **19 testes**, executáveis a qualquer momento:

```bash
python manage.py test monitor
```

Cobrem renderização (nenhuma tag de template vazando), path traversal, magic
bytes, SSRF, mascaramento, rate limiting, checksum, cabeçalhos e força do
hasher. O teste de renderização existe por causa de um bug real: comentários
`{# ... #}` escritos em duas linhas **não** são reconhecidos pelo Django (a
sintaxe só vale dentro de uma linha) e apareciam como texto no topo da tela.
Conferir só o status HTTP não pegava — a página devolvia 200 com o comentário
visível.

`TabelaNaoGerenciadaMixin` cria `faturas_cpfl` no banco de teste, já que ela é
`managed=False` e o Django não a criaria sozinho.

---

## 6. Terceira rodada — 30/10/2026, revisão antes da publicação

Antes de abrir o código, uma revisão procurou o que as rodadas anteriores não
cobriram e o que não pode ir a público. Cada vulnerabilidade abaixo ganhou um
teste que **falhava antes da correção** e passa depois.

### 🟠 A9 — Login do admin do Django fora do rate limit
`/admin/login/` usa o formulário próprio do Django, que não passa por
`limite_negocio()` nem pela auditoria. A proteção contra força bruta de
`/login/` tinha uma porta lateral sem tranca — justamente para as contas de
superusuário.

**Correção:** `admin.site.login` redireciona para `/login/`. Quem já entrou
como superusuário continua usando o admin normalmente.

**Verificado:** `POST /admin/login/` com a senha **correta** não autentica
(`LoginDoAdminTest`).

### 🟡 M8 — Texto digitado no campo de usuário gravado em claro
O limite por conta usava o usuário digitado como chave de cache e no resumo
da auditoria (`Limite de login_conta:<digitado> excedido`). Quem erra de campo
digita a senha no lugar do usuário — e ela ia parar em duas tabelas do banco,
contrariando a regra que a própria tela de login já seguia ao registrar
falhas.

**Correção:** a chave do contador passa a ser um HMAC (`salted_hmac`) do
texto digitado, e a auditoria registra só "login por conta".

**Verificado:** dez tentativas com uma "senha" no campo de usuário, nenhuma
ocorrência dela na auditoria nem na tabela de cache.

### 🟡 M9 — Injeção de fórmula no CSV (OWASP *CSV Injection*)
Nome de empresa, de unidade e de titular são texto livre — o do titular sai de
dentro do PDF, que qualquer remetente controla. Célula iniciada por `=`, `+`,
`-` ou `@` vira fórmula ao abrir no Excel. Um administrador de cliente podia
mirar o superusuário, que exporta os dados de todas as empresas.

**Correção:** `texto_csv()` prefixa `'` nesses casos. Colunas numéricas
continuam numéricas.

### 🟡 M10 — Marcação interpretada no relatório PDF
O `Paragraph` do reportlab interpreta `<a href>`, `<font>` e `<b>`. Um nome
de unidade com link virava **link clicável no relatório entregue ao cliente**;
um `<` sem par derrubava a exportação inteira com erro 500.

**Correção:** `texto_pdf()` escapa a marcação de todo texto livre.

### 🟡 M11 — Webhook seguia redirect depois de validado
`validar_url_webhook()` (A4) confere o destino, mas a chamada usava o
`urlopen` padrão, que **segue redirects**. Um host público que respondesse
`302` apontando para `http://169.254.169.254/` levava a requisição para
dentro da rede depois da validação. Apontado pelo bandit (B310) e confirmado
na prática: o `urlopen` padrão visitou o destino do redirect.

**Correção:** `enviar_sem_redirecionar()` usa um opener sem tratamento de
redirect; qualquer `3xx` vira erro.

**Verificado:** servidor local que responde `302` — a requisição para no
destino validado (`test_webhook_nao_segue_redirect`).

### 🟠 D1 — Dependências com vulnerabilidade conhecida
O `pip-audit` encontrou **72 avisos em 7 pacotes** fixados no
`requirements.txt`, entre eles Django 6.0.6 (5), Pillow 12.2.0 (52) e
cryptography 48.0.0 (4).

**Correção:** Django 6.0.9, sqlparse 0.6.0, Pillow 13.0.0 e cryptography
50.0.2. `requests` e `beautifulsoup4` saíram — só os scripts de estudo os
usavam — e levaram junto `urllib3`, `certifi` e `soupsieve`, também
vulneráveis. O projeto passou de 24 para 17 pacotes.

**Verificado:** `pip-audit` sem vulnerabilidades; 37 testes passando num
ambiente limpo; e o extrator produziu resultado **idêntico** (comparação por
hash) em 12 faturas reais antes e depois da troca — sem que nenhum dado delas
saísse da máquina.

A CI repete o `pip-audit` a cada push e toda segunda-feira, e o Dependabot
abre pull request quando sai versão nova.

### Privacidade e publicação
- Removidos do código e da documentação um número de instalação real e os
  dados de uma fatura real usada em teste.
- Scripts SQL do protótipo (com senha de exemplo em texto puro) saíram do
  repositório.
- O `.gitignore` passou a barrar qualquer `*.pdf`, `*.csv` e `*.xls(x)`:
  fatura e exportação são dado pessoal.
- As capturas de tela publicadas vêm do comando `popular_demo`, que gera
  dados fictícios (instalações `9000xxx`, e-mails em `example.com`, IPs das
  faixas de documentação da RFC 5737), só roda com `DEBUG=True` e se recusa a
  tocar banco que tenha qualquer dado real.

### Reprodutibilidade
A migration `0011` cria `faturas_cpfl` em ambiente novo. Antes, um clone limpo
quebrava na primeira tela, e os testes dependiam de um mixin que criava a
tabela com estrutura diferente da real.

### Testes e integração contínua
`monitor/tests.py` passou de 25 para **37 testes**. O GitHub Actions roda a
cada push: testes com PostgreSQL, `check --deploy` com configuração de
produção, `pip-audit`, bandit (análise estática), gitleaks (varredura de
segredos no histórico) e CodeQL.

---

## 7. Limitações conhecidas

1. **`style-src-attr` mantém `'unsafe-inline'`.** Necessário porque larguras
   de barra são calculadas no servidor. Risco baixo: atributo de estilo não
   executa script.
2. **`faturas_cpfl` continua fora do dumpdata.** É `managed=False`; o backup
   correto dela é `pg_dump`.
3. **Coleta IMAP ainda processa uma caixa só.** O lote vai a 25 mensagens por
   execução, mas a caixa é uma.
4. **Sem segundo fator de autenticação.** O login tem rate limit em duas
   camadas e Argon2id, mas não MFA. É o próximo passo natural para as contas
   de superusuário.
5. **Textos gerados pelo robô sem acentuação.** Mensagens de alerta e de log
   saem em ASCII (`Instalacao nao cadastrada`); as telas, não.
