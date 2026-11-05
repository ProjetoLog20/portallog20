"""Configuracao do Orcamento OBZ dentro do Portal de Solucoes LOG20.

Guardada no repositorio do portal (o do sistema nao muda) e copiada na imagem como
config/settings/portal.py. Parte da configuracao de rede do proprio projeto (DEBUG sempre
desligado, whitenoise, sessao de 8 horas) e muda so o que o portal precisa.
"""
from .rede import *  # noqa: F401,F403
from .rede import LOGGING, env

# Endereco publico do portal (ex.: http://172.25.71.33:8105), para o CSRF aceitar os formularios.
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Varios sistemas no mesmo IP (portas diferentes) recebem os mesmos cookies: nomes proprios
# para o login do Orcamento nao derrubar o de outro sistema Django (e vice-versa).
SESSION_COOKIE_NAME = "obz_sessao"
CSRF_COOKIE_NAME = "obz_csrf"

# Logs so no console: aparecem na aba Logs do portal (o container nao guarda arquivo de log).
LOGGING["handlers"].pop("arquivo", None)
for _logger in LOGGING["loggers"].values():
    _logger["handlers"] = ["console"]
