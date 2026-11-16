#!/usr/bin/env bash
# Atualiza o Portal de Solucoes LOG20 nesta VM e deixa as solucoes no ar. Um comando so:
#
#   bash deploy/atualizar.sh                       # portal + solucoes novas ou com problema
#   bash deploy/atualizar.sh --atualizar-solucoes  # tambem reconstroi as solucoes com o ultimo commit
#   bash deploy/atualizar.sh --ip 192.168.0.50     # informa o IP em vez de detectar
#   bash deploy/atualizar.sh --sem-backup          # pula o backup de antes (nao recomendado)
#
# O que faz, em ordem:
#   0. backup de tudo (bancos, arquivos das solucoes, dados do portal); se falhar, para aqui;
#   1. git pull do portal (para se houver arquivos alterados a mao nesta VM);
#   2. confere o IP da VM e corrige PORTAL_URL_BASE/PORTAL_URL_PUBLICA no .env se mudou
#      (so quando o .env usa um IP; com dominio, nao mexe);
#   3. sobe portal + Caddy + PostgreSQL e recria o portal (carrega o codigo e o .env novos);
#   4. instala e inicia as solucoes que faltam, reinicia as com problema e regera o Caddy.
#      Se o IP mudou, reinicia tambem as que estao no ar (sites que gravam o endereco na
#      construcao, como os que usam o Supabase do servidor, sao reconstruidos sozinhos).
#      Solucoes paradas ficam paradas.
# Pode rodar quantas vezes quiser. Os bancos e volumes existentes nunca sao apagados nem recriados.

set -euo pipefail

PASTA_PORTAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PASTA_PORTAL"

azul() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
aviso() { printf '\033[1;33m%s\033[0m\n' "$*"; }
erro() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

IP=""
SINC=()
SEM_BACKUP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ip) IP="${2:-}"; shift 2 ;;
    --atualizar-solucoes) SINC+=(--atualizar); shift ;;
    --reiniciar-solucoes) SINC+=(--reiniciar); shift ;;
    --sem-backup) SEM_BACKUP=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) erro "Opcao desconhecida: $1 (use --help)"; exit 1 ;;
  esac
done

if [ ! -f .env ] || [ ! -f docker-compose.yml ]; then
  erro "Nao foi possivel encontrar .env e docker-compose.yml nesta pasta. Na primeira instalacao use: bash deploy/preparar-vm.sh"
  exit 1
fi

if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi

# ---------- 0. backup antes de mexer em qualquer coisa ----------
if grep -qiE '^PORTAL_BACKUP_ANTES_DE_ATUALIZAR=(nao|não|false|0|off)' .env; then SEM_BACKUP=1; fi
if [ "$SEM_BACKUP" = 1 ]; then
  aviso "Backup antes de atualizar: pulado."
elif ! "${DOCKER[@]}" compose ps --status running --services 2>/dev/null | grep -qx portal; then
  aviso "O portal nao esta rodando: sem backup antes de atualizar (nada a copiar por ele agora)."
elif ! "${DOCKER[@]}" compose exec -T portal python -m marketplace backup --help >/dev/null 2>&1; then
  aviso "Esta versao do portal ainda nao faz backup; o primeiro sai depois desta atualizacao (tela Backups)."
else
  azul "Backup antes de atualizar"
  set +e
  "${DOCKER[@]}" compose exec -T portal python -m marketplace backup --motivo antes-de-atualizar
  RB=$?
  set -e
  if [ $RB -ne 0 ]; then
    erro "O backup antes de atualizar falhou ou teve erros (veja acima). Nada foi atualizado."
    erro "Resolva e rode de novo, ou atualize mesmo assim com: bash deploy/atualizar.sh --sem-backup"
    exit 1
  fi
fi

# ---------- 1. codigo do portal ----------
azul "Atualizando o codigo do portal"
if ! git diff --quiet || ! git diff --cached --quiet; then
  erro "Ha arquivos do portal alterados nesta VM:"
  git status --short >&2
  erro "Nada foi atualizado. Guarde ou descarte essas mudancas (git stash) e rode de novo."
  exit 1
fi
ANTES=$(git rev-parse --short HEAD)
git pull --ff-only --quiet
DEPOIS=$(git rev-parse --short HEAD)
if [ "$ANTES" = "$DEPOIS" ]; then echo "Ja estava na versao mais recente ($DEPOIS)."; else echo "$ANTES -> $DEPOIS"; fi

# ---------- 2. IP ----------
IP="${IP:-$(hostname -I | awk '{print $1}')}"
BASE_ATUAL=$(sed -n 's/^PORTAL_URL_BASE=//p' .env | tr -d '\r' | tail -1)
if [[ "$BASE_ATUAL" =~ ^http://[0-9.]+$ ]]; then
  if [ "$BASE_ATUAL" != "http://$IP" ]; then
    azul "O IP da VM mudou: ${BASE_ATUAL#http://} -> $IP"
    sed -i -e "s#^PORTAL_URL_BASE=.*#PORTAL_URL_BASE=http://$IP#" \
           -e "s#^PORTAL_URL_PUBLICA=.*#PORTAL_URL_PUBLICA=http://$IP:8000#" .env
    SINC+=(--reiniciar)
  fi
else
  echo "PORTAL_URL_BASE e '$BASE_ATUAL' (dominio ou personalizado): o IP nao foi conferido."
fi

# Senha do administrador do PostgreSQL do servidor: gerada uma vez, guardada so no .env.
garantir_senha_postgres() {
  if ! grep -qE '^PORTAL_POSTGRES_SENHA=.+' .env; then
    local senha
    senha=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    if grep -q '^PORTAL_POSTGRES_SENHA=' .env; then
      sed -i "s/^PORTAL_POSTGRES_SENHA=.*/PORTAL_POSTGRES_SENHA=$senha/" .env
    else
      printf '\n# Senha do administrador do PostgreSQL do servidor (gerada pelo deploy).\nPORTAL_POSTGRES_SENHA=%s\n' "$senha" >> .env
    fi
    echo "Senha do banco do servidor gerada e guardada no .env."
  fi
}

# ---------- 3. portal, Caddy e banco ----------
garantir_senha_postgres
# Pasta dos backups (fechada: tem senhas das solucoes e copias dos bancos).
PASTA_BACKUP=$(sed -n 's/^PORTAL_BACKUP_PASTA_HOST=//p' .env | tr -d '\r' | tail -1)
PASTA_BACKUP="${PASTA_BACKUP:-./backups}"
mkdir -p "$PASTA_BACKUP" 2>/dev/null || sudo mkdir -p "$PASTA_BACKUP"
chmod 700 "$PASTA_BACKUP" 2>/dev/null || sudo chmod 700 "$PASTA_BACKUP" || true
PG_ANTES=$("${DOCKER[@]}" inspect -f '{{.Id}}' log20-postgres 2>/dev/null || true)
azul "Subindo portal, Caddy e banco"
"${DOCKER[@]}" compose up -d --build --quiet-pull
# O codigo do portal e montado no container: recriar e o que carrega o codigo e o .env novos.
"${DOCKER[@]}" compose up -d --force-recreate --no-deps portal
PG_DEPOIS=$("${DOCKER[@]}" inspect -f '{{.Id}}' log20-postgres 2>/dev/null || true)
if [ -n "$PG_ANTES" ] && [ "$PG_ANTES" != "$PG_DEPOIS" ]; then
  # Os dados ficam no volume log20-postgres-dados e continuam la; so o container foi trocado.
  aviso "O container do PostgreSQL foi recriado (configuracao dele mudou). Os dados continuam no volume log20-postgres-dados."
fi

echo "Aguardando o portal responder..."
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://127.0.0.1:8000/" 2>/dev/null; then break; fi
  sleep 2
done
if ! curl -fsS -o /dev/null "http://127.0.0.1:8000/" 2>/dev/null; then
  erro "O portal nao respondeu. Ultimas linhas do log:"
  "${DOCKER[@]}" compose logs --tail 30 portal >&2
  exit 1
fi

# ---------- 3b. Supabase fixo da primeira versao ----------
# Hoje cada solucao com [supabase] no manifesto ganha um Supabase proprio (log20-sb-<id>-*), criado
# pelo portal no Iniciar. A pilha fixa antiga (log20-supabase-*) nao e mais atualizada nem usada.
# O script nao a desliga sozinho: so avisa (os dados dela ficam nos volumes log20-supabase-*).
if "${DOCKER[@]}" ps --format '{{.Names}}' | grep -q '^log20-supabase-'; then
  aviso "O Supabase fixo antigo (containers log20-supabase-*) ainda esta ligado e nao e mais usado:"
  aviso "  cada sistema agora tem o seu (log20-sb-<id>-*). Para liberar a memoria dele, rode:"
  aviso "  ${DOCKER[*]} stop \$(${DOCKER[*]} ps -q --filter name=^log20-supabase-)"
  aviso "  Os dados dele continuam nos volumes log20-supabase-* ate voce apaga-los."
fi

# ---------- 4. solucoes ----------
azul "Instalando e iniciando as solucoes"
set +e
"${DOCKER[@]}" compose exec -T portal python -m marketplace sincronizar "${SINC[@]}"
RESULTADO=$?
set -e

azul "Pronto"
echo "  Portal: http://$IP:8000"
if [ $RESULTADO -ne 0 ]; then
  aviso "Alguma solucao falhou (veja acima). Um repositorio novo precisa estar no token de leitura desta VM."
  exit $RESULTADO
fi
