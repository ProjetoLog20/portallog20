#!/usr/bin/env bash
# Prepara uma VM/servidor Ubuntu para rodar o Portal de Solucoes LOG20.
#
#   git clone https://github.com/<usuario>/portal-solucoes-log20.git
#   cd portal-solucoes-log20
#   bash deploy/preparar-vm.sh                # detecta o IP sozinho
#   bash deploy/preparar-vm.sh 192.168.0.50   # ou informe o IP que os usuarios vao usar
#
# O que faz: instala o Docker (se faltar), cria o .env (pede o token de leitura do GitHub
# para o portal clonar as solucoes), abre as portas no firewall (se o ufw estiver ativo),
# sobe portal + Caddy e, se ainda nao houver, cria o administrador.
# O codigo das solucoes nao precisa estar na VM: o portal clona dos repositorios.
# Pode rodar de novo quando quiser: ele so aplica o que estiver faltando.

set -euo pipefail

PASTA_PORTAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PASTA_PORTAL"

azul() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
aviso() { printf '\033[1;33m%s\033[0m\n' "$*"; }

# ---------- conferencias ----------
if [ ! -f docker-compose.yml ] || [ ! -d solucoes ]; then
  echo "Rode este script de dentro da pasta do portal (onde ficam docker-compose.yml e solucoes/)." >&2
  exit 1
fi

# ---------- Docker ----------
if ! command -v docker >/dev/null 2>&1; then
  azul "Instalando o Docker (pode pedir a sua senha)"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

# Ate a pessoa sair e entrar de novo na sessao, o grupo "docker" ainda nao vale: usa sudo.
if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=(sudo docker)
fi

if ! "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  echo "O plugin 'docker compose' nao foi encontrado. Instale com: sudo apt-get install -y docker-compose-plugin" >&2
  exit 1
fi

# ---------- .env ----------
IP="${1:-}"
if [ -z "$IP" ]; then
  IP="$(hostname -I | awk '{print $1}')"
fi
if [ -z "$IP" ]; then
  echo "Nao consegui descobrir o IP. Rode de novo informando: bash deploy/preparar-vm.sh <ip>" >&2
  exit 1
fi

if [ -f .env ]; then
  azul "Usando o .env que ja existe (apague-o para gerar de novo)"
  grep -E '^PORTAL_URL_(BASE|PUBLICA)=' .env || true
else
  azul "Criando .env para o IP $IP"
  echo "O portal precisa de um token de LEITURA do GitHub para clonar os repositorios privados"
  echo "das solucoes (github.com > Settings > Developer settings > Fine-grained tokens,"
  echo "permissao 'Contents: Read-only'). Deixe em branco se os repositorios forem publicos."
  read -r -s -p "Token (nao aparece ao digitar): " TOKEN; echo
  umask 077
  sed -e "s#^PORTAL_URL_BASE=.*#PORTAL_URL_BASE=http://$IP#" \
      -e "s#^PORTAL_URL_PUBLICA=.*#PORTAL_URL_PUBLICA=http://$IP:8000#" \
      variaveis.exemplo > .env
  if [ -n "$TOKEN" ]; then
    # O token vai por variavel de ambiente (nao aparece na lista de processos).
    TOKEN_NOVO="$TOKEN" python3 - <<'PY'
import os, pathlib
p = pathlib.Path(".env")
linhas = ["PORTAL_GIT_TOKEN=" + os.environ["TOKEN_NOVO"] if l.startswith("PORTAL_GIT_TOKEN=") else l
          for l in p.read_text().splitlines()]
p.write_text("\n".join(linhas) + "\n")
PY
  fi
  unset TOKEN
  chmod 600 .env
fi

# ---------- firewall ----------
PORTAS=$(grep -oE '"[0-9]+:[0-9]+"' docker-compose.yml | tr -d '"' | cut -d: -f1 | sort -u)
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  azul "Liberando no firewall as portas: $(echo $PORTAS)"
  for p in $PORTAS; do sudo ufw allow "$p"/tcp >/dev/null; done
fi

# ---------- subir ----------
azul "Construindo e subindo portal + Caddy (a primeira vez leva alguns minutos)"
"${DOCKER[@]}" compose up -d --build

azul "Aguardando o portal responder (ate 3 minutos)"
PRONTO=0
for _ in $(seq 1 90); do
  # Silencioso: enquanto o portal inicia, a conexao e recusada/reiniciada, o que e normal.
  if curl -fs -o /dev/null "http://127.0.0.1:8000/"; then PRONTO=1; break; fi
  printf '.'
  sleep 2
done
echo
if [ "$PRONTO" != "1" ]; then
  aviso "O portal nao respondeu. Situacao dos containers e ultimas linhas do log:"
  "${DOCKER[@]}" compose ps
  "${DOCKER[@]}" compose logs --tail 40 portal
  exit 1
fi

# ---------- administrador ----------
TEM_ADMIN=$("${DOCKER[@]}" compose exec -T portal python -c \
  "from marketplace.__main__ import _auth; print(_auth().total_admins_ativos())" 2>/dev/null | tr -d '\r' || echo 0)
if [ "${TEM_ADMIN:-0}" = "0" ]; then
  azul "Criando o administrador do portal"
  "${DOCKER[@]}" compose exec portal python -m marketplace criar-admin
fi

# ---------- pronto ----------
azul "Pronto!"
cat <<FIM

  Portal:        http://$IP:8000
  Soluções:      http://$IP:8101  (Central do Colaborador)
                 http://$IP:8102  (Central do Colaborador - Homologação)

  1. Abra o portal no navegador do seu computador e entre com o administrador.
  2. Em Soluções, clique em "Instalar e iniciar" (o primeiro build leva alguns minutos).
  3. Em Usuários, crie as pessoas e libere os sistemas de cada uma.

  As soluções só abrem para quem está logado no portal e tem acesso liberado.

  Comandos úteis (nesta pasta):
    ${DOCKER[*]} compose logs -f portal     # acompanhar o portal
    ${DOCKER[*]} compose restart caddy      # depois de editar deploy/Caddyfile.servidor
    ${DOCKER[*]} compose down               # parar portal e Caddy (as soluções continuam)
FIM

if [ "${DOCKER[0]}" = "sudo" ]; then
  aviso "Dica: saia e entre de novo na VM para usar 'docker' sem sudo."
fi
