#!/bin/sh
# Inicio do Orcamento OBZ no portal: tabelas, primeiro administrador e servidor waitress.
set -e
python manage.py migrate --noinput

if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  python manage.py shell -c '
import os
from django.contrib.auth import get_user_model
Usuario = get_user_model()
nome = os.environ["DJANGO_SUPERUSER_USERNAME"]
if Usuario.objects.filter(username=nome).exists():
    print(f"Administrador {nome} ja existe; a senha nao foi alterada.")
else:
    Usuario.objects.create_superuser(nome, os.environ.get("DJANGO_SUPERUSER_EMAIL", ""),
                                     os.environ["DJANGO_SUPERUSER_PASSWORD"])
    print(f"Administrador {nome} criado.")
'
fi

exec waitress-serve --listen=0.0.0.0:8000 --threads=8 config.wsgi:application
