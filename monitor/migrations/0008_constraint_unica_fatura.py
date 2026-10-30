"""
Garante a constraint unica que sustenta a gravacao atomica de faturas.

A tabela `faturas_cpfl` e `managed = False` (o robo grava nela por SQL
direto), entao o Django nao cria constraints para ela sozinho. Sem
UNIQUE (instalacao, data_vencimento):

  - o `INSERT ... ON CONFLICT` de salvar_fatura_completa() falha, e
  - duas execucoes simultaneas do robo gravam a mesma fatura duas vezes.

O banco de desenvolvimento ja tinha essa constraint criada a mao (uq_fatura).
Esta migration existe para que um ambiente novo nasca com ela.
"""

from django.db import migrations


CRIAR = """
DO $$
BEGIN
    IF to_regclass('public.faturas_cpfl') IS NULL THEN
        RAISE NOTICE 'Tabela faturas_cpfl ainda nao existe; constraint sera criada depois.';
        RETURN;
    END IF;

    -- Nao recria se ja houver qualquer constraint unica sobre o par de colunas.
    IF EXISTS (
        SELECT 1
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = 'faturas_cpfl'
           AND c.contype = 'u'
           AND c.conkey @> ARRAY[
                 (SELECT attnum FROM pg_attribute
                   WHERE attrelid = 'faturas_cpfl'::regclass AND attname = 'instalacao'),
                 (SELECT attnum FROM pg_attribute
                   WHERE attrelid = 'faturas_cpfl'::regclass AND attname = 'data_vencimento')
               ]::smallint[]
    ) THEN
        RAISE NOTICE 'Constraint unica ja existe em faturas_cpfl; nada a fazer.';
        RETURN;
    END IF;

    -- Falha alto e claro em vez de apagar dados por conta propria: registros
    -- duplicados precisam de decisao humana sobre qual versao preservar.
    IF EXISTS (
        SELECT 1 FROM faturas_cpfl
         GROUP BY instalacao, data_vencimento
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Existem faturas duplicadas por (instalacao, data_vencimento). '
            'Liste com: SELECT instalacao, data_vencimento, count(*) FROM faturas_cpfl '
            'GROUP BY 1,2 HAVING count(*) > 1;  Resolva antes de aplicar esta migration.';
    END IF;

    ALTER TABLE faturas_cpfl
        ADD CONSTRAINT uq_fatura UNIQUE (instalacao, data_vencimento);
    RAISE NOTICE 'Constraint uq_fatura criada.';
END $$;
"""

REMOVER = """
ALTER TABLE IF EXISTS faturas_cpfl DROP CONSTRAINT IF EXISTS uq_fatura;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("monitor", "0007_faturacpfldetalhe_pdf_nome_original_and_more"),
    ]

    operations = [
        migrations.RunSQL(sql=CRIAR, reverse_sql=REMOVER),
    ]
