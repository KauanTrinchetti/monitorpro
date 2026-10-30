"""
Tag {% estatico %} — igual a {% static %}, mas com versao no endereco.

Motivo: o runserver serve estatico pelo StaticFilesHandler, que responde
antes da cadeia de middleware e envia apenas `Last-Modified`, sem
`Cache-Control` nem `ETag`. Sem `Cache-Control` o Chrome aplica cache
heuristico e reaproveita o arquivo sem consultar o servidor. Na pratica: voce
altera o CSS, recarrega, e a tela aparece quebrada — porque o navegador
montou a pagina nova com a folha de estilo velha.

Acrescentar `?v=<mtime>` muda a URL sempre que o arquivo muda, e o navegador
e obrigado a buscar de novo.

Em producao a tag apenas repassa para {% static %}: o
ManifestStaticFilesStorage ja poe um hash do conteudo no nome do arquivo
(monitorpro.a1b2c3d4.css), que resolve o mesmo problema de forma melhor.
"""

from pathlib import Path

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def estatico(caminho: str) -> str:
    url = static(caminho)

    if not settings.DEBUG:
        return url

    absoluto = finders.find(caminho)
    if not absoluto:
        return url

    try:
        # Consulta o disco a cada renderizacao de proposito: guardar o mtime
        # em cache anularia o objetivo, porque o runserver so reinicia quando
        # muda um .py — editar o CSS nao trocaria a versao. Em producao esta
        # ramificacao nem executa, e em desenvolvimento um stat() e irrelevante.
        versao = int(Path(absoluto).stat().st_mtime)
    except OSError:
        return url

    return f"{url}?v={versao}"
