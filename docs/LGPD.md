# Proteção de dados pessoais — LGPD

Este documento descreve **o que o MonitorPro faz com dados pessoais** e quais
medidas técnicas estão no código para cumprir a Lei Geral de Proteção de Dados
(Lei 13.709/2018). É documentação técnica, não parecer jurídico: base legal,
prazos contratuais e a comunicação de incidentes são decisões do controlador.

> Detalhes de cada controle de segurança em [SEGURANCA.md](SEGURANCA.md).

---

## 1. Quem é quem

| Papel na LGPD | No uso típico do MonitorPro |
|---|---|
| **Titular** | A pessoa a quem a conta de energia se refere, e também os contatos de alerta e os usuários do sistema |
| **Controlador** | A empresa cliente, que decide quais unidades monitorar e para quê |
| **Operador** | Quem opera o MonitorPro em nome do cliente, se não for o próprio cliente |

Quando uma empresa usa o sistema só para as próprias contas, ela é a
controladora.

## 2. Inventário: que dado entra, de onde e para quê

| Dado | Origem | Onde fica | Finalidade |
|---|---|---|---|
| Nome do titular da conta | Texto do PDF | `faturas_cpfl.cliente_nome` | Identificar a conta no painel e no relatório |
| Número da instalação | PDF e cadastro | `faturas_cpfl`, `UnidadeConsumidora` | Ligar a fatura à unidade e à empresa certa |
| Vencimento, valor, consumo, período de leitura | PDF | `faturas_cpfl` | Comparar com a fatura anterior e gerar alertas |
| Leituras e histórico do medidor | PDF | `FaturaCpflDetalhe` | Detalhe da fatura (energia e demanda) |
| Endereço da unidade (opcional) | Cadastro manual | `UnidadeConsumidora.endereco` | Identificar a unidade |
| E-mail e WhatsApp de alerta | Cadastro manual | `Empresa`, `UnidadeConsumidora` | Enviar alertas |
| CPF ou CNPJ da empresa (opcional) | Cadastro manual | `Empresa.documento` | Cabeçalho do relatório em PDF |
| Usuário, nome, e-mail e senha | Cadastro | `auth_user` (senha só como hash Argon2id) | Autenticação |
| IP, navegador, ação e horário | Cada requisição sensível | `AuditoriaEvento`, `RegistroAlteracao` | Segurança e prestação de contas |
| Destinatários do e-mail da fatura | Cabeçalho do e-mail | Só em memória; mascarados no log | Conferir se a fatura chegou pelo alias da unidade |
| O PDF da fatura | E-mail ou upload | Disco, temporariamente (seção 4) | Extração e diagnóstico de falha |

**O que o sistema não coleta:** o CPF impresso na fatura serve apenas de âncora
para localizar o nome no texto e **não é gravado**; o endereço do PDF não é
extraído. Não há analytics, pixel de rastreamento nem script de terceiros (a CSP
bloqueia). Os cookies são só os técnicos: o de sessão e, quando há um aviso a
exibir, o de mensagens do Django.

## 3. Os princípios do art. 6º no código

| Princípio | Como o sistema atende |
|---|---|
| **Finalidade e adequação** | Os dados servem só para acompanhar consumo e custo de energia. Nenhum uso secundário |
| **Necessidade** | Do PDF sai só o que a comparação usa. O arquivo é apagado depois de lido com sucesso |
| **Livre acesso e transparência** | Cada empresa vê os próprios dados no painel e exporta tudo em CSV e PDF |
| **Qualidade dos dados** | SHA-256 de cada PDF prova de qual documento veio cada valor; `RegistroAlteracao` guarda o antes e o depois de cada mudança |
| **Segurança** | Controle de acesso por empresa e por papel, Argon2id, rate limit, CSP, HSTS, validação de upload, proteção contra SSRF e *path traversal* ([SEGURANCA.md](SEGURANCA.md)) |
| **Prevenção** | Testes de segurança na CI, `pip-audit` semanal e aplicação que se recusa a subir com configuração insegura |
| **Não discriminação** | Não há decisão automatizada sobre pessoas; os alertas tratam de consumo |
| **Responsabilização** | Trilha de auditoria e este documento |

## 4. Retenção e descarte

| Dado | Retenção | Mecanismo |
|---|---|---|
| PDF lido com sucesso | **Apagado na hora** | O robô e o worker removem o arquivo ao terminar |
| PDF que falhou na leitura | 90 dias, para diagnóstico | `expurgar_pdfs --dias 90 --confirmar` (agendado semanalmente). Simula por padrão e registra na auditoria o que apagou |
| Faturas extraídas | Enquanto durar a relação com o cliente | Definido pelo controlador; eliminação pelo admin |
| Backups | Os 14 mais recentes | `backup_dados --manter 14` remove os antigos, inclusive os gerados pelo painel |
| Log de aplicação | 5 arquivos de 5 MB (segurança: 10) | Rotação por tamanho; sem dado pessoal em claro |
| Sessão | 8 horas, ou ao fechar o navegador | Configuração da sessão |
| Contador de rate limit | 5 a 15 minutos | Expiração no cache |
| Auditoria | Sem expurgo automático | Ver seção 9 |

## 5. Dado pessoal fora do banco

O banco é o único lugar com controle de acesso por empresa. O resto é tratado
como superfície de vazamento:

- **Log:** nome, e-mail, documento e instalação são mascarados na origem
  (`monitor/seguranca.py`). `GRAO DOURADO PADARIAS` vira `G**O D*****O P******S`,
  e a instalação `9000101` vira `***0101`, mantendo os 4 últimos dígitos para
  rastreio.
- **Auditoria:** `limpar_detalhes()` remove senha, token e chaves de API antes de
  gravar, inclusive em dicionários aninhados. A tela de login não grava o texto
  digitado: guarda só o tamanho, e o contador por conta usa um HMAC.
- **Disco:** PDFs ficam fora de `MEDIA_ROOT`, então nada é servido pela web.
  São gravados com permissão `0600` (onde o sistema de arquivos aceita) e com o
  nome saneado mais um sufixo aleatório.
- **Repositório:** `.gitignore` barra `.env`, banco, backups, logs, pastas de PDF
  e qualquer `*.pdf`, `*.csv` e `*.xls(x)`. A CI roda o gitleaks em todo o histórico.

## 6. Registro das operações (art. 37)

`AuditoriaEvento` grava quem fez, o quê, de qual IP e quando, para:
login bem-sucedido e falho, logout, limite de tentativas excedido, acesso
negado, coleta solicitada, exportação CSV e PDF, alteração de empresa e de
unidade, envio de alertas, upload de faturas, backup gerado e restaurado e
expurgo de arquivos.

`RegistroAlteracao` complementa com **o que** mudou: campo, valor anterior e
valor novo. É o que permite reconstruir o histórico de um dado e contestar uma
alteração indevida.

## 7. Direitos do titular (art. 18)

| Direito | Como atender hoje |
|---|---|
| Confirmação e acesso | Relatório filtrado pela instalação e exportação em CSV ou PDF |
| Correção | Edição pelo painel ou pelo admin; a alteração fica em `RegistroAlteracao` |
| Eliminação | Excluir as faturas e a unidade pelo admin. PDFs saem no expurgo e backups antigos na rotação |
| Portabilidade | Exportação CSV, em formato aberto |
| Informação sobre compartilhamento | Seção 8 |
| Anonimização | **Não há rotina automática** (seção 10) |

Os pedidos chegam ao controlador. O sistema dá os meios, mas não tem portal de
autoatendimento para o titular.

## 8. Terceiros envolvidos

| Terceiro | O que recebe | Observação |
|---|---|---|
| Provedor da caixa de e-mail da automação (ex.: Gmail) | As faturas em PDF, por onde elas chegam | Escolha do controlador; acesso por senha de app, nunca a senha da conta |
| Provedor do webhook de WhatsApp | Telefones de alerta e o texto do alerta (nome da unidade, valor e vencimento) | Só `https` e host público, sem seguir *redirect* |
| Servidor SMTP | E-mail de alerta e texto do alerta | Configurado pelo `.env` |
| Google Fonts | O IP e o navegador de quem abre o painel | Risco residual (seção 10) |
| Hospedagem e banco | Tudo | Responsabilidade de quem implanta |

## 9. Incidente de segurança

1. **Conter.** Troque a `SECRET_KEY` (todas as sessões caem na hora) e as senhas
   do banco, da caixa de e-mail (`SENHA_APP`) e do SMTP. Desative usuários
   suspeitos no admin.
2. **Investigar.** `AuditoriaEvento` mostra logins, falhas, acessos negados,
   exportações e backups por IP e horário; `logs/seguranca.log` tem os eventos
   do Django; `RegistroAlteracao` mostra o que foi mudado.
3. **Avaliar.** Quais dados, de quantos titulares, e se há risco ou dano
   relevante.
4. **Comunicar.** Se o MonitorPro for operado para um cliente, avise o
   controlador sem demora. Cabe ao controlador comunicar a ANPD e os titulares;
   a Resolução CD/ANPD nº 15/2024 fixa **três dias úteis** quando o incidente
   puder causar risco ou dano relevante.
5. **Registrar** o incidente e as medidas tomadas, mesmo os que não exigirem
   comunicação.

## 10. Riscos residuais e próximos passos

Lista honesta do que ainda não está resolvido:

- **Google Fonts.** O navegador de quem acessa o painel busca as fontes no
  Google, o que expõe IP e navegador a um terceiro. Próximo passo: servir as
  fontes (licença OFL) junto com a aplicação e tirar os domínios do Google da CSP.
- **Backup sem criptografia própria.** O arquivo é comprimido e tem checksum,
  mas não é cifrado; contém dados de todas as empresas. Próximo passo: cifrar
  antes de gravar ou guardar em volume cifrado.
- **Auditoria sem prazo de retenção.** Cresce indefinidamente e guarda IP. O
  prazo é decisão do controlador; falta a rotina de expurgo.
- **Sem anonimização automática** a pedido do titular: hoje a eliminação é
  manual, pelo admin.
- **Sem segundo fator de autenticação** para superusuário.

## 11. Este repositório

Nenhum dado real foi publicado. As capturas de tela vêm do comando
`popular_demo`, que gera empresas fictícias, instalações na faixa `9000xxx`,
e-mails em `example.com` (domínio reservado pela RFC 2606) e IPs das faixas
reservadas para documentação (RFC 5737). O comando só roda com `DEBUG=True` e
se recusa a tocar um banco que tenha qualquer dado real. Antes da publicação, o
código e a documentação foram revisados para remover um número de instalação e
os dados de uma fatura real usados em teste.
