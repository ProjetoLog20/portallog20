#!/usr/bin/env bash
# Sobe (ou atualiza) o Supabase do servidor. Chamado por deploy/atualizar.sh e deploy/preparar-vm.sh
# quando PORTAL_SUPABASE=sim no .env; tambem pode ser rodado a mao, na pasta do portal:
#   bash deploy/supabase/subir.sh
#
# 1. gera no .env os segredos que faltam (uma vez so; depois nunca troca) e atualiza o endereco
#    publico da API pelo PORTAL_URL_BASE;
# 2. docker compose up -d da pilha (baixa as imagens na primeira vez: alguns minutos);
# 3. espera o gateway ficar saudavel.
# Os dados ficam nos volumes log20-supabase-*: rodar de novo nunca apaga nada.
set -euo pipefail
PASTA_PORTAL="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PASTA_PORTAL"
azul() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
aviso() { printf '\033[1;33m%s\033[0m\n' "$*"; }
erro() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi
COMPOSE=("${DOCKER[@]}" compose -p log20-supabase -f deploy/supabase/docker-compose.yml --env-file .env)

azul "Supabase do servidor"
# Os segredos sao gerados pelo portal (Python dele); sem o portal no ar, usa o python3 da maquina.
if "${DOCKER[@]}" compose exec -T portal python -m marketplace supabase preparar 2>/dev/null; then
  :
elif command -v python3 >/dev/null 2>&1; then
  python3 -m marketplace supabase preparar
else
  erro "Nao consegui preparar o .env do Supabase (portal fora do ar e sem python3)."
  exit 1
fi

# Memoria: a pilha usa algo entre 1,5 e 3 GB.
LIVRE_MB=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ "${LIVRE_MB:-0}" -gt 0 ] && [ "$LIVRE_MB" -lt 1500 ]; then
  aviso "Pouca memoria livre na VM (${LIVRE_MB} MB). O Supabase precisa de 1,5 a 3 GB; pode ficar lento ou reiniciar."
fi

"${COMPOSE[@]}" up -d
echo "Aguardando o Supabase ficar pronto (ate 5 minutos na primeira vez)..."
for _ in $(seq 1 100); do
  ESTADO=$("${DOCKER[@]}" inspect -f '{{.State.Health.Status}}' log20-supabase-gw 2>/dev/null || echo "?")
  [ "$ESTADO" = "healthy" ] && break
  sleep 3
done
if [ "${ESTADO:-}" != "healthy" ]; then
  erro "O Supabase nao ficou pronto. Situacao e ultimas linhas:"
  "${COMPOSE[@]}" ps
  "${COMPOSE[@]}" logs --tail 20
  exit 1
fi
URL=$(sed -n 's/^SUPABASE_URL_PUBLICA=//p' .env | tail -1)
USUARIO=$(sed -n 's/^SUPABASE_PAINEL_USUARIO=//p' .env | tail -1)
echo "  API (para os sistemas): $URL"
echo "  Painel: solucao 'Supabase Studio' no portal (usuario do painel: $USUARIO; senha em SUPABASE_PAINEL_SENHA no .env)"
