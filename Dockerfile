# syntax=docker/dockerfile:1
# Imagem do Portal de Solucoes para rodar em servidor.
# O codigo do portal vem da pasta montada em /portal (o clone do repositorio, veja
# docker-compose.yml); a imagem traz so o Python, as dependencias, o Git e a CLI do Docker.
# O codigo das solucoes o proprio portal clona dos repositorios indicados nos manifestos.

FROM docker:27-cli AS docker-cli

FROM python:3.12-slim
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONUNBUFFERED=1 \
    PORTAL_HOST=0.0.0.0
WORKDIR /portal
EXPOSE 8000
CMD ["python", "-m", "marketplace"]
