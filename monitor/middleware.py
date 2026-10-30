"""
Cabecalhos de seguranca que o Django nao emite por conta propria.

O SecurityMiddleware nativo cuida de HSTS, nosniff e SSL redirect; o
XFrameOptionsMiddleware cuida do X-Frame-Options. Faltavam Content-Security-
Policy, Permissions-Policy e o reforco de no-store nas telas autenticadas.
"""

from django.conf import settings


class CabecalhosDeSegurancaMiddleware:
    """Carimba CSP e politicas complementares em toda resposta."""

    # Recursos do navegador que a aplicacao nao usa. Negar explicitamente
    # impede que um script injetado peca acesso a camera, microfone etc.
    PERMISSIONS_POLICY = (
        "accelerometer=(), ambient-light-sensor=(), autoplay=(), battery=(), "
        "camera=(), display-capture=(), document-domain=(), encrypted-media=(), "
        "fullscreen=(self), geolocation=(), gyroscope=(), magnetometer=(), "
        "microphone=(), midi=(), payment=(), usb=(), xr-spatial-tracking=()"
    )

    def __init__(self, get_response):
        self.get_response = get_response
        self._csp = self._montar_csp()

    @staticmethod
    def _montar_csp():
        diretivas = getattr(settings, "CSP_DIRETIVAS", None)
        if not diretivas:
            return ""

        partes = [f"{nome} {valor}".strip() for nome, valor in diretivas.items()]

        # upgrade-insecure-requests so faz sentido quando o site ja esta em
        # HTTPS; em desenvolvimento local ele quebraria o carregamento.
        if not settings.DEBUG:
            partes.append("upgrade-insecure-requests")

        return "; ".join(partes)

    def __call__(self, request):
        resposta = self.get_response(request)

        # Nao adianta tentar carimbar Cache-Control em /static/ aqui: no
        # runserver o StaticFilesHandler intercepta essas URLs antes da cadeia
        # de middleware. O cache de estatico e resolvido no template, pela tag
        # {% estatico %} (monitor/templatetags/estaticos.py).
        if self._csp and "Content-Security-Policy" not in resposta:
            resposta["Content-Security-Policy"] = self._csp

        resposta.setdefault("Permissions-Policy", self.PERMISSIONS_POLICY)
        resposta.setdefault("Cross-Origin-Resource-Policy", "same-origin")

        # Telas autenticadas nao devem ficar no cache do navegador nem em
        # proxy intermediario: evita que a proxima pessoa no mesmo terminal
        # veja dados da sessao anterior pelo botao "voltar".
        if getattr(request, "user", None) is not None and request.user.is_authenticated:
            resposta.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, private")
            resposta.setdefault("Pragma", "no-cache")

        return resposta
