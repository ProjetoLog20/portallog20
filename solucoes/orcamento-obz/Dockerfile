# Imagem do Orcamento OBZ para o Portal de Solucoes LOG20. Fica no repositorio do portal:
# o repositorio do sistema (JungGustavo/log20-orcamento_obz) nao muda.
# Os arquivos COPY --from=portal vem desta pasta (solucoes/orcamento-obz/).
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.portal
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
COPY --from=portal portal_settings.py /app/config/settings/portal.py
COPY --from=portal --chmod=755 portal-saude /usr/local/bin/portal-saude
COPY --from=portal --chmod=755 portal-iniciar.sh /usr/local/bin/portal-iniciar
RUN useradd --create-home --uid 1000 app && mkdir -p /app/media /app/logs \
    && chown app /app && chown -R app /app/media /app/logs
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
  CMD portal-saude http://127.0.0.1:8000/entrar/
CMD ["portal-iniciar"]
