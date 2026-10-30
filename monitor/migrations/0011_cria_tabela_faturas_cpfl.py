"""
Cria a tabela `faturas_cpfl` em ambientes novos.

A tabela e `managed = False`: quem escreve nela e o robo, por SQL direto, e o
Django so le. Por isso nenhuma migration a criava — ela existia apenas no
banco onde o sistema nasceu, criada a mao. Um clone novo do repositorio subia
sem ela e quebrava na primeira tela com "relation faturas_cpfl does not exist".

A estrutura abaixo e a da tabela em uso, inclusive a constraint unica que a
migration 0008 garante em bancos antigos. Em banco que ja tem a tabela,
`IF NOT EXISTS` torna esta migration inofensiva.

O reverso nao apaga nada de proposito: desfazer uma migration nao pode custar
as faturas gravadas.
"""

from django.db import migrations


CRIAR = """
CREATE TABLE IF NOT EXISTS faturas_cpfl (
    id              SERIAL PRIMARY KEY,
    cliente_nome    VARCHAR(150)  NOT NULL,
    data_vencimento DATE          NOT NULL,
    valor_pagar     NUMERIC(10,2) NOT NULL,
    data_coleta     TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    pago            BOOLEAN       DEFAULT FALSE,
    instalacao      VARCHAR(20),
    consumo_kwh     VARCHAR(20),
    periodo_leitura TEXT,
    CONSTRAINT uq_fatura UNIQUE (instalacao, data_vencimento)
);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("monitor", "0010_alter_auditoriaevento_acao"),
    ]

    operations = [
        migrations.RunSQL(sql=CRIAR, reverse_sql=migrations.RunSQL.noop),
    ]
