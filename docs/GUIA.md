# Guia do MonitorPro

Manual de quem mexe no sistema. Escrito para ser consultado aos pedaços — use
o índice, não leia de ponta a ponta.

- [1. O que o sistema faz](#1-o-que-o-sistema-faz)
- [2. Como usar o painel](#2-como-usar-o-painel)
- [3. Rodando no dia a dia](#3-rodando-no-dia-a-dia)
- [4. Onde fica cada coisa no código](#4-onde-fica-cada-coisa-no-código)
- [5. Receitas para tarefas comuns](#5-receitas-para-tarefas-comuns)
- [6. Quando der problema](#6-quando-der-problema)
- [7. Glossário do Django](#7-glossário-do-django)

---

## 1. O que o sistema faz

O MonitorPro acompanha contas de energia elétrica de vários clientes e avisa
quando alguma sai da curva.

### O ciclo completo

```
   A distribuidora manda a fatura por e-mail
                  |
                  v
   O robô lê a caixa de entrada (IMAP)
                  |
                  v
   Baixa o PDF anexo e valida
   (é PDF mesmo? tamanho? páginas? tem senha?)
                  |
                  v
   Extrai: cliente, vencimento, valor, instalação, consumo
                  |
                  v
   Liga a fatura a uma UNIDADE CONSUMIDORA
   pelo número da instalação
                  |
                  v
   Grava no banco e compara com a fatura anterior
                  |
          +-------+-------+
          |               |
          v               v
   Subiu muito?      Dentro do normal
   gera ALERTA       só registra
          |
          v
   Painel mostra, e-mail/WhatsApp avisam
```

### Os três conceitos que sustentam tudo

**Empresa** — o cliente. Cada empresa tem suas unidades e seus usuários.

**Unidade consumidora** — o ponto de energia. É o que liga a fatura ao
cliente, pelo **número da instalação** que vem impresso no PDF.

> Isto é o que mais causa confusão: se a instalação não estiver cadastrada
> como unidade, o robô lê o PDF, não sabe de quem é, e gera um alerta
> "Instalação não cadastrada" em vez de registrar a fatura. **Cadastrar a
> unidade antes é obrigatório.**

**Fatura** — a conta em si. Fica na tabela `faturas_cpfl`, que o robô escreve
por SQL direto (o Django só lê).

### De onde vêm as faturas

Dois caminhos, e os dois passam pela mesma validação:

| Caminho | Como funciona |
|---|---|
| **Automático** | O robô lê a caixa de e-mail configurada em `EMAIL_CONTA` |
| **Manual** | Você arrasta os PDFs em *Enviar faturas* |

O caminho automático depende de o cliente encaminhar as faturas para o alias
cadastrado no campo **E-mail da automação** da unidade.

---

## 2. Como usar o painel

Endereço local: `http://127.0.0.1:8000/`

### Os três papéis

Cada usuário tem um perfil ligado a uma empresa, com um papel:

| Papel | Pode fazer |
|---|---|
| **Administrador** | Unidades e configurações **da própria empresa**, além do que o operador faz |
| **Operador** | Coleta, envio de faturas, ver logs, disparar alertas |
| **Leitura** | Só visualizar: painel, relatórios, financeiro, histórico |
| **Superusuário** | Enxerga todas as empresas + operações de sistema (backup) |

> **Administrador é da empresa, não do sistema.** Um administrador gerencia as
> unidades e configurações do próprio cliente e não alcança dado de nenhum
> outro. Operações que atravessam empresas — hoje, o backup — exigem
> superusuário, porque o arquivo gerado contém os dados de todos os clientes.
> Quem confunde os dois níveis acaba entregando a base inteira a um cliente.

### Tela por tela

**Visão geral** (`/`) — resumo do mês: total de faturas, valor, consumo, o
gráfico dos últimos 6 meses e os alertas ativos. O mês de maior gasto vem
destacado em roxo no gráfico.

**Relatórios** (`/relatorio/`) — a tela mais completa. Mostra:
- Contagem de quantas contas subiram, caíram e ficaram estáveis
- **Maiores altas** e **maiores quedas**, com o impacto em reais
- Tabela com variação de consumo *e* de valor lado a lado

> Consumo e valor podem andar em sentidos opostos. Com reajuste de tarifa, a
> conta sobe mesmo com o consumo caindo. Por isso as duas colunas existem.

Clicar em qualquer linha abre o detalhe da fatura.

**Financeiro** (`/financeiro/`) — gastos agrupados por mês, por unidade e por
cliente, em barras comparativas.

**Histórico** (`/historico/`) — as 10 faturas mais recentes.

**Enviar faturas** (`/faturas/upload/`) — arraste até 20 PDFs de uma vez. Cada
arquivo é conferido no servidor e entra numa fila. A tela do lote mostra o
andamento de cada arquivo e o motivo de qualquer recusa.

**Alertas** (`/alertas/`) — o que está fora do normal: vencimento chegando,
consumo acima do limite da unidade, instalação não cadastrada. O botão
*Enviar alertas* dispara e-mail e WhatsApp para os contatos cadastrados.

**Unidades** (`/unidades/`) — cadastro das instalações. **É aqui que o sistema
começa a funcionar.** Sem unidade cadastrada, faturas chegam e ficam órfãs.

**Coletas** (`/coletas/`) — histórico de execuções do robô e o botão *Coletar
agora*.

**Logs e backups** (`/logs/`) — execuções do robô, trilha de auditoria (quem
fez o quê e quando) e geração de backup.

**Configurações** (`/configuracoes/`) — nome, documento, logo, cor e contatos
de alerta da empresa.

### O que significa cada situação no relatório

| Selo | Quer dizer |
|---|---|
| **Alta** | Consumo subiu mais de 1% em relação à fatura anterior |
| **Queda** | Consumo caiu mais de 1% |
| **Estável** | Variou menos de 1% para cima ou para baixo |
| **Acima do limite** | Passou do limite percentual configurado naquela unidade |
| **Primeira fatura** | É a primeira daquela instalação, não há com o que comparar |

A faixa de 1% existe para que oscilação normal de leitura não vire ruído.

---

## 3. Rodando no dia a dia

Todos os comandos rodam na raiz do projeto, com o ambiente virtual ativado.

### Subir o sistema

```bash
python manage.py runserver
```

Depois abra `http://127.0.0.1:8000/`. Para parar: `Ctrl+C` no terminal.

### Os cinco comandos

**Coletar faturas do e-mail** — lê até 25 mensagens por execução:

```bash
python manage.py coletar_faturas --limite 25
```

**Processar a fila de PDFs** — pega o que está esperando e processa:

```bash
python manage.py processar_fila --uma-passada --concorrencia 2
```

`--concorrencia 2` processa 2 PDFs ao mesmo tempo. Aumente com cuidado: cada
um roda em processo separado e consome memória.

**Gerar backup** — comprimido, com verificação de integridade:

```bash
python manage.py backup_dados --manter 14
```

**Testar a restauração** — não grava nada, só confere:

```bash
python manage.py restaurar_backup backups/backup_monitor_XXXX.json.gz --ensaio
```

Para restaurar de verdade, troque `--ensaio` por `--confirmar`.

**Limpar PDFs antigos** — faturas são dado pessoal e não devem ficar em disco
para sempre:

```bash
python manage.py expurgar_pdfs --dias 90 --incluir-tmp
```

Por padrão só mostra o que apagaria. Acrescente `--confirmar` para apagar.

### Rodar os testes

Antes de qualquer alteração ir para o servidor:

```bash
python manage.py test monitor
```

São 37 testes. Se algum falhar, não suba a mudança.

### Ver o sistema com dados fictícios

Para demonstrar ou tirar capturas de tela sem expor cliente nenhum, use um
banco separado e o comando `popular_demo`:

```bash
DB_NAME=monitorpro_demo python manage.py migrate
DB_NAME=monitorpro_demo python manage.py createcachetable
DB_NAME=monitorpro_demo python manage.py popular_demo
```

Ele cria três empresas, sete unidades e 84 faturas inventadas, e mostra
uma única vez as senhas geradas para os usuários `demo` (superusuário),
`gestor.padaria` e `leitura.clinica`. Só roda com `DEBUG=True` e se recusa a
tocar banco que já tenha dado real. Para recomeçar do zero, use `--recriar`.

### No servidor, agende assim

| Comando | Frequência |
|---|---|
| `coletar_faturas` | a cada 1 hora |
| `processar_fila` | a cada 5 minutos |
| `backup_dados` | diário, de madrugada |
| `expurgar_pdfs --confirmar` | semanal |

No Windows Server: Agendador de Tarefas. Em Linux: cron.

---

## 4. Onde fica cada coisa no código

### Se você quer mexer em X, abra Y

| Quero... | Abra |
|---|---|
| Mudar o que aparece numa tela | `monitor/views.py` |
| Mudar o desenho da tela | `monitor/templates/monitor/` |
| Mudar cor, fonte, espaçamento | `monitor/static/monitor/css/monitorpro.css` |
| Acrescentar um campo no banco | `monitor/models.py` (e criar migration) |
| Mudar validação de formulário | `monitor/forms.py` |
| Mexer na leitura do PDF | `robo_leitor_cpfl.py` |
| Mudar endereço de uma página | `monitor/urls.py` |
| Mexer em segurança/configuração | `core/settings.py` |

### O que cada arquivo faz

**`core/settings.py`** — configuração central: banco, segurança, CSP, cache,
sessão, limites de upload, logging. Se algo obrigatório faltar em produção, a
aplicação **se recusa a subir** em vez de rodar de forma insegura.

**`monitor/models.py`** — as tabelas:

| Modelo | Guarda |
|---|---|
| `Empresa` | o cliente |
| `PerfilUsuario` | liga usuário a empresa e define o papel |
| `UnidadeConsumidora` | a instalação de energia |
| `FaturaCpfl` | a fatura (tabela do robô, o Django só lê) |
| `FaturaCpflDetalhe` | leituras do medidor e o SHA-256 do PDF de origem |
| `AlertaOperacional` | alertas gerados pelo robô |
| `AuditoriaEvento` | quem fez o quê e quando |
| `RegistroAlteracao` | o que exatamente mudou, com valor anterior |
| `LoteProcessamento` / `ArquivoProcessamento` | a fila de PDFs |
| `RoboExecucaoLog` / `ColetaControle` | execuções e trava da coleta |
| `BackupRegistro` | backups gerados |

**`monitor/views.py`** — o miolo. Cada função é uma tela ou uma ação. As
funções `montar_*` preparam os dados que vão para o template.

**`monitor/seguranca.py`** — as defesas, todas num lugar só:
- `nome_arquivo_seguro()` — impede que um anexo chamado `../../settings.py`
  escreva fora da pasta
- `validar_pdf()` — confere que é PDF de verdade pelo conteúdo, não pela
  extensão; checa tamanho, páginas e senha
- `validar_url_webhook()` — impede que o webhook aponte para a rede interna
- `mascarar_*()` — tira dado pessoal dos logs
- `sha256_arquivo()` — identifica o arquivo e detecta adulteração

**`monitor/fila.py`** — a fila roda no próprio PostgreSQL, sem Redis nem
Celery. `reivindicar_proximo()` entrega um arquivo por worker sem que dois
peguem o mesmo.

**`monitor/worker_pdf.py`** — a função que roda no processo filho. Não importa
nada do Django no topo do arquivo, e isso é proposital: quebra se importar.

**`monitor/middleware.py`** — carimba os cabeçalhos de segurança em toda
resposta.

**`monitor/templatetags/estaticos.py`** — a tag `{% estatico %}`, que põe a
data do arquivo na URL do CSS. É o que evita o navegador servir estilo velho.

**`robo_leitor_cpfl.py`** — o robô. Conecta no e-mail, baixa anexo, extrai os
dados do PDF e grava. Tem lógica separada para CPFL e para Elektro, porque a
Elektro usa fontes embaralhadas que precisam ser decodificadas.

### Como uma tela funciona, do clique ao HTML

```
  Navegador pede /relatorio/
            |
            v
  monitor/urls.py   -> descobre qual função chamar
            |
            v
  monitor/views.py  -> relatorio_completo()
            |          busca faturas, calcula variações
            v
  templates/monitor/relatorio.html
            |          monta o HTML com esses dados
            v
  static/monitor/css/monitorpro.css
            |          dá a aparência
            v
  Navegador mostra a página
```

---

## 5. Receitas para tarefas comuns

### Criar um usuário administrador

```bash
python manage.py createsuperuser
```

Superusuário enxerga todas as empresas. Para um usuário comum, crie pelo admin
do Django (`/admin/`) e **cadastre o PerfilUsuario** ligando ele a uma empresa
— sem perfil, a pessoa entra e não vê nada.

### Trocar a senha de alguém

```bash
python manage.py changepassword nome_do_usuario
```

Não existe como *ver* a senha de ninguém: o sistema guarda só o hash. Isso é
proposital.

### Cadastrar um cliente novo, do zero

1. `/configuracoes/` — dados da empresa
2. `/unidades/nova/` — uma unidade para cada instalação, com:
   - **Instalação**: o número que vem no PDF da fatura
   - **E-mail da automação**: o alias que vai receber as faturas
   - **Limite de aumento**: a partir de quanto vira alerta (padrão 20%)
3. No `/admin/`, crie o usuário e o `PerfilUsuario` apontando para a empresa
4. Peça ao cliente para encaminhar as faturas ao alias cadastrado

### Descobrir por que uma fatura não apareceu

1. `/logs/` — veja a última execução do robô
2. `/alertas/` — procure "Instalação não cadastrada"
3. Confira em `pdfs_falha/` se o PDF ficou guardado
4. Confirme que a instalação do PDF bate com a cadastrada na unidade

Na maioria das vezes é instalação não cadastrada ou divergente.

### Ver quem alterou um dado

`/logs/`, seção de auditoria. `AuditoriaEvento` diz *que* houve alteração;
`RegistroAlteracao` diz *o que* mudou, com o valor antes e depois.

### Mudar as cores do sistema

Abra `monitor/static/monitor/css/monitorpro.css` e mexa nos tokens no topo do
arquivo (`:root`). Um valor alterado ali muda o sistema inteiro.

> Se mudar cor de texto, confira o contraste. Os tons atuais foram medidos e
> passam no mínimo de acessibilidade (4,5:1). O comentário no topo do arquivo
> explica.

### Acrescentar um campo numa tabela

```bash
# 1. edite monitor/models.py
# 2. gere a migration
python manage.py makemigrations monitor
# 3. aplique
python manage.py migrate
```

---

## 6. Quando der problema

### A tela aparece quebrada, sem estilo

Cache do navegador com CSS antigo. `Ctrl+Shift+R` força recarga. Se persistir,
confira se o CSS está sendo servido:
`http://127.0.0.1:8000/static/monitor/css/monitorpro.css`

### "Já existe uma coleta em andamento" e não sai disso

Trava de coleta que ficou presa. Ela se solta sozinha depois de 45 minutos. Se
tiver pressa:

```bash
python manage.py shell -c "from monitor.views import liberar_coleta_travada; print(liberar_coleta_travada())"
```

### O robô não acha as faturas

Confira nesta ordem:
1. `EMAIL_CONTA` e `SENHA_APP` no `.env` — precisa ser **senha de app** do
   Google, não a senha da conta
2. IMAP habilitado no Gmail
3. Se o e-mail tem anexo PDF de verdade
4. Se o e-mail não foi para as pastas `processados` ou `pendentes`

### Aparece "Muitas tentativas, aguarde"

Proteção contra força bruta: 10 tentativas por origem em 5 minutos, ou 8 na
mesma conta em 15 minutos. Espere o tempo passar. Para limpar durante um
teste:

```bash
python manage.py shell -c "from django.core.cache import cache; cache.clear()"
```

### A aplicação não sobe e reclama de configuração

É proposital. Com `DEBUG=False`, faltar `SECRET_KEY`, `ALLOWED_HOSTS` ou
`DB_PASSWORD` derruba o boot em vez de subir inseguro. A mensagem de erro diz
qual variável falta. Veja o `.env.example`.

### Um PDF foi recusado no upload

A tela do lote mostra o motivo de cada um. Os mais comuns:

| Motivo | O que aconteceu |
|---|---|
| não é um PDF de verdade | o conteúdo não começa com a assinatura de PDF |
| protegido por senha | não dá para extrair sem a senha |
| excede o limite | maior que 25 MB ou mais de 150 páginas |
| arquivo idêntico já processado | mesmo SHA-256 de outro já lido |
| PDF digitalizado, sem texto | é a foto/scan da conta; peça o PDF original da distribuidora |

> **Sobre conta escaneada:** o extrator lê texto, não imagem. Um PDF que é só
> a digitalização do papel não tem o que ler. Não há conserto no código — a
> saída é pedir ao cliente o PDF que a distribuidora enviou, que além de
> funcionar traz dado mais confiável do que qualquer leitura por OCR.

---

## 7. Glossário do Django

Se você não mexe com Django direto, esses são os termos que aparecem o tempo
todo:

**View** — a função que responde a um endereço. Recebe a requisição, busca os
dados, devolve a página. Ficam em `views.py`.

**Template** — o arquivo HTML com espaços a preencher. `{{ nome }}` insere um
valor, `{% if %}` decide o que mostrar.

**Model** — a definição de uma tabela do banco, escrita em Python. Uma classe
vira uma tabela; um atributo vira uma coluna.

**Migration** — o registro de uma alteração no banco. Você muda o model, gera
a migration, aplica. É o histórico de como o banco chegou ao estado atual.

**QuerySet** — o resultado de uma busca no banco. Preguiçoso: só consulta de
verdade quando alguém percorre o resultado.

**Middleware** — código que passa por toda requisição, antes e depois da view.
É onde ficam os cabeçalhos de segurança.

**Management command** — um script rodado por `python manage.py nome`. Os
cinco comandos deste projeto são isso.

**CSRF** — proteção contra um site externo enviar formulário em seu nome. O
`{% csrf_token %}` nos formulários é isso.

**Migração aplicada vs. pendente** — `migrate` aplica o que ainda não foi
aplicado. `showmigrations` mostra o que está pendente.

---

## Onde continuar

| Arquivo | Assunto |
|---|---|
| [SEGURANCA.md](SEGURANCA.md) | o que foi corrigido em segurança e por quê |
| [LGPD.md](LGPD.md) | quais dados pessoais o sistema trata e como |
| [DEPLOY.md](DEPLOY.md) | como colocar no servidor |
| [.env.example](../.env.example) | todas as variáveis de configuração |
| `monitor/tests.py` | os testes, que também servem de exemplo de uso |
