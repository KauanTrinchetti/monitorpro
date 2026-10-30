# Política de segurança

## Versões cobertas

Só a versão mais recente da branch `main` recebe correções de segurança.

## Como relatar uma vulnerabilidade

**Não abra uma *issue* pública.** Use o relato privado do GitHub:

1. Abra a aba **Security** deste repositório.
2. Clique em **Report a vulnerability**.
3. Descreva o problema, os passos para reproduzir e o impacto que você enxerga.

O relato fica visível só para quem mantém o projeto até a correção ser publicada.

## O que esperar

| Etapa | Prazo |
|---|---|
| Confirmação de recebimento | até 5 dias úteis |
| Avaliação inicial e severidade | até 10 dias úteis |
| Correção de falha crítica ou alta | o mais rápido possível, com aviso de andamento |

Correções entram no relatório técnico [`docs/SEGURANCA.md`](docs/SEGURANCA.md)
com o problema, a correção e o teste que a comprova. Quem relatou recebe o
crédito, se quiser.

## Escopo

**Dentro:** o código deste repositório, ou seja, a aplicação Django, o robô de
leitura de faturas, os comandos de gerenciamento e a configuração padrão.

**Fora:** instalações de terceiros, engenharia social, ataques de negação de
serviço por volume e problemas que dependam de acesso prévio ao servidor ou ao
arquivo `.env`.

## Regras para testar

- **Nunca use dados pessoais reais.** Monte o ambiente com
  `python manage.py popular_demo`, que gera empresas, faturas e usuários fictícios.
- Teste só em instalação local sua.
- Pesquisa feita de boa-fé e dentro destas regras não será tratada como ataque.
