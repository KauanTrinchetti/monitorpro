"""
Configuracao do MonitorPro.

Regra geral: nenhum segredo tem valor padrao utilizavel. Se faltar
configuracao critica em producao, a aplicacao se recusa a subir em vez
de cair silenciosamente num modo inseguro.
"""

from pathlib import Path
import os
import sys

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


# ==========================================================
# Helpers de leitura de ambiente
# ==========================================================

def env_bool(name, default=False):
    valor = os.environ.get(name)
    if valor is None:
        return default
    return valor.strip().lower() in ["1", "true", "yes", "on"]


def env_list(name, default=None):
    valor = os.environ.get(name)
    if not valor:
        return list(default or [])
    return [item.strip() for item in valor.split(",") if item.strip()]


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ==========================================================
# Modo de execucao
# ==========================================================

# Padrao seguro: DEBUG desligado. Ligar exige acao explicita no ambiente.
DEBUG = env_bool("DEBUG", False)

# Comandos que rodam fora do ciclo de request e nao precisam de segredo
# de producao (permite `migrate`/`collectstatic` em build sem SECRET_KEY).
_COMANDO_DE_BUILD = len(sys.argv) > 1 and sys.argv[1] in {
    "collectstatic", "makemigrations", "migrate", "check", "createcachetable",
}


# ==========================================================
# SECRET_KEY — sem fallback embutido
# ==========================================================

SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()

if not SECRET_KEY:
    if DEBUG or _COMANDO_DE_BUILD:
        # Chave efemera de desenvolvimento: muda a cada boot, o que invalida
        # sessoes antigas de proposito e impede que vire chave "de producao".
        from django.core.management.utils import get_random_secret_key

        SECRET_KEY = get_random_secret_key()
    else:
        raise ImproperlyConfigured(
            "SECRET_KEY nao definida. Gere uma com:\n"
            '  python -c "from django.core.management.utils import '
            'get_random_secret_key; print(get_random_secret_key())"\n'
            "e coloque no .env antes de subir em producao."
        )


# ==========================================================
# Hosts e origens confiaveis
# ==========================================================

if DEBUG:
    ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])
else:
    ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
    if not ALLOWED_HOSTS and not _COMANDO_DE_BUILD:
        raise ImproperlyConfigured(
            "ALLOWED_HOSTS vazio com DEBUG=False. Defina os dominios no .env, "
            "ex: ALLOWED_HOSTS=seudominio.com.br,www.seudominio.com.br"
        )

CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")


# ==========================================================
# Aplicacoes
# ==========================================================

INSTALLED_APPS = [
    "monitor",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Cabecalhos que o Django nao emite sozinho (CSP, Referrer-Policy,
    # Permissions-Policy). Fica por ultimo para carimbar toda resposta.
    "monitor.middleware.CabecalhosDeSegurancaMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"


# ==========================================================
# Banco de dados
# ==========================================================

_DB_PASSWORD = os.environ.get("DB_PASSWORD")
if not _DB_PASSWORD and not DEBUG and not _COMANDO_DE_BUILD:
    raise ImproperlyConfigured("DB_PASSWORD nao definida com DEBUG=False.")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "sistema_prestadores"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": _DB_PASSWORD,
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        # Conexoes reaproveitadas por 60s: reduz custo de handshake sem
        # segurar conexao ociosa indefinidamente.
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"connect_timeout": 10},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ==========================================================
# Cache — usado pelo rate limiting
# ==========================================================
# Precisa ser compartilhado entre processos, senao cada worker mantem o
# proprio contador e o limite efetivo vira (limite x numero de workers).
# Com LocMemCache o controle de brute force e praticamente decorativo.
#
# Criar a tabela uma vez com:  python manage.py createcachetable

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "monitor_cache",
        "TIMEOUT": 300,
        "OPTIONS": {"MAX_ENTRIES": 10000, "CULL_FREQUENCY": 3},
    }
}


# ==========================================================
# Autenticacao e senhas
# ==========================================================

# Argon2id e o hasher recomendado pela OWASP. Fica primeiro na lista, o que
# tambem faz o Django re-hashear senhas antigas no proximo login bem-sucedido.
# Se o pacote nao estiver instalado, mantem PBKDF2 (padrao do Django, seguro)
# em vez de quebrar a autenticacao.
try:
    import argon2  # noqa: F401

    _ARGON2_DISPONIVEL = True
except ImportError:
    _ARGON2_DISPONIVEL = False

PASSWORD_HASHERS = (
    [
        "django.contrib.auth.hashers.Argon2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
        "django.contrib.auth.hashers.ScryptPasswordHasher",
    ]
    if _ARGON2_DISPONIVEL
    else [
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
        "django.contrib.auth.hashers.ScryptPasswordHasher",
    ]
)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ==========================================================
# Sessao
# ==========================================================
# O login() do Django ja rotaciona a chave da sessao (protege contra session
# fixation). O que faltava era expiracao e escopo do cookie.

SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE", 8 * 60 * 60)  # 8h
SESSION_SAVE_EVERY_REQUEST = True          # renova a janela a cada atividade
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True             # bloqueia leitura por JavaScript
SESSION_COOKIE_SAMESITE = "Lax"            # mitiga CSRF cross-site
SESSION_COOKIE_NAME = "monitorpro_sessao"

CSRF_COOKIE_HTTPONLY = False               # o template le o token do form
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_USE_SESSIONS = True                   # token na sessao, nao em cookie proprio


# ==========================================================
# HTTPS e cabecalhos de seguranca
# ==========================================================

# Marque ATRAS_DE_PROXY_CONFIAVEL=True apenas quando houver nginx/Cloudflare/
# Render na frente reescrevendo os cabecalhos. Se ficar True sem proxy, um
# cliente pode forjar X-Forwarded-Proto e X-Forwarded-For.
ATRAS_DE_PROXY_CONFIAVEL = env_bool("ATRAS_DE_PROXY_CONFIAVEL", False)

if ATRAS_DE_PROXY_CONFIAVEL:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000  # 1 ano
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Consumido pelo CabecalhosDeSegurancaMiddleware.
#
# Sobre estilo: todo o CSS foi movido para monitor/css/monitorpro.css, entao
# nao existe mais nenhum <style> inline — style-src-elem pode ficar estrito.
# Ja style-src-attr precisa de 'unsafe-inline' porque algumas larguras sao
# calculadas no servidor (barra de progresso, barras comparativas) e chegam
# como style="width: N%". Separar as duas diretivas bloqueia o vetor real (um
# bloco <style> injetado) sem quebrar o atributo, que nao executa script.
#
# style-src continua presente como reserva para navegadores que ainda nao
# implementam as diretivas -elem e -attr.
CSP_DIRETIVAS = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self' 'unsafe-inline' https://fonts.googleapis.com",
    "style-src-elem": "'self' https://fonts.googleapis.com",
    "style-src-attr": "'unsafe-inline'",
    "font-src": "'self' https://fonts.gstatic.com",
    "img-src": "'self' data:",
    "connect-src": "'self'",
    "form-action": "'self'",
    "frame-ancestors": "'none'",
    "base-uri": "'self'",
    "object-src": "'none'",
}


# ==========================================================
# Limites de requisicao e upload
# ==========================================================

PDF_TAMANHO_MAXIMO_MB = env_int("PDF_TAMANHO_MAXIMO_MB", 25)
PDF_TAMANHO_MAXIMO_BYTES = PDF_TAMANHO_MAXIMO_MB * 1024 * 1024
# 150 paginas cobre consolidados de semestre e de ano. O numero anterior
# (60) barrava um consolidado real de 72 paginas que continha tres faturas
# validas. A contagem de paginas e a mais fraca das defesas: quem realmente
# contem abuso e o teto de tamanho, o timeout e o processo filho descartavel.
PDF_PAGINAS_MAXIMAS = env_int("PDF_PAGINAS_MAXIMAS", 150)
PDF_TIMEOUT_SEGUNDOS = env_int("PDF_TIMEOUT_SEGUNDOS", 120)
PDF_ARQUIVOS_POR_LOTE = env_int("PDF_ARQUIVOS_POR_LOTE", 20)

# Acima deste tamanho o upload vai para arquivo temporario em disco em vez de
# ficar na memoria — evita que N uploads simultaneos estourem a RAM.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024

# Teto de corpo de requisicao que nao seja arquivo (protege contra POST gigante).
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 500
DATA_UPLOAD_MAX_NUMBER_FILES = PDF_ARQUIVOS_POR_LOTE

FILE_UPLOAD_PERMISSIONS = 0o600

MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "media/"

# Diretorio dos PDFs recebidos. Fica fora de MEDIA_ROOT de proposito: nada
# aqui deve ser servido diretamente pela web.
PDFS_RECEBIDOS_DIR = BASE_DIR / "pdfs_recebidos"
PDFS_FALHA_DIR = Path(os.environ.get("PDFS_FALHA_DIR", BASE_DIR / "pdfs_falha"))

MONITOR_MAX_UNIDADES_EMPRESA = env_int("MONITOR_MAX_UNIDADES_EMPRESA", 500)


# ==========================================================
# Internacionalizacao
# ==========================================================
# Estava en-us/UTC, o que fazia todo horario exibido pelo painel aparecer
# 3 horas adiantado em relacao ao horario de Brasilia.

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True


# ==========================================================
# Arquivos estaticos
# ==========================================================

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# monitor/static/ e encontrado pelo AppDirectoriesFinder (padrao do Django);
# incluir o mesmo caminho em STATICFILES_DIRS causaria deteccao duplicada.

# Em producao o nome do arquivo passa a carregar um hash do conteudo
# (monitorpro.a1b2c3d4.css). Quando o CSS muda, muda a URL — o navegador nao
# tem como servir uma versao velha do cache.
#
# Isso resolve um problema concreto: o runserver envia apenas Last-Modified,
# sem Cache-Control nem ETag. Sem Cache-Control o Chrome aplica cache
# heuristico e reutiliza o arquivo sem revalidar, entao uma alteracao de CSS
# aparece como "layout quebrado" ate alguem fazer Ctrl+Shift+R.
#
# Exige `python manage.py collectstatic` no deploy.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
        ),
    },
}


# ==========================================================
# E-mail
# ==========================================================

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "monitorpro@localhost")
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_TIMEOUT = 20

WHATSAPP_WEBHOOK_URL = os.environ.get("WHATSAPP_WEBHOOK_URL", "")


# ==========================================================
# Logging
# ==========================================================
# Regra: log de aplicacao nao recebe dado pessoal em claro. Nome de cliente,
# e-mail e documento sao mascarados na origem (monitor/seguranca.py).

LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "padrao": {
            "format": "{asctime} {levelname:<8} {name} {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "padrao",
        },
        "arquivo": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOGS_DIR / "monitorpro.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "padrao",
        },
        "seguranca": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOGS_DIR / "seguranca.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 10,
            "encoding": "utf-8",
            "formatter": "padrao",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console", "arquivo"],
            "level": "INFO",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console", "seguranca"],
            "level": "INFO",
            "propagate": False,
        },
        "monitor": {
            "handlers": ["console", "arquivo"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "monitor.seguranca": {
            "handlers": ["console", "seguranca"],
            "level": "INFO",
            "propagate": False,
        },
        "robo": {
            "handlers": ["console", "arquivo"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
