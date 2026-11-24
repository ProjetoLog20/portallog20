#!/usr/bin/env bash
# Liga, renova ou desliga o HTTPS do portal com um certificado proprio (um nome com portas:
# o portal em https://<nome> e cada sistema em https://<nome>:<porta>).
#
#   bash deploy/ligar-https.sh portal.log20.com.br certificado.pfx
#   bash deploy/ligar-https.sh portal.log20.com.br certificado.crt chave.key
#   bash deploy/ligar-https.sh --renovar certificado-novo.pfx      # troca o certificado (mesmo nome)
#   bash deploy/ligar-https.sh --desligar                          # volta para http://IP
#
# Opcoes: --sim (nao pergunta antes de aplicar), --sem-atualizar (so instala os arquivos e o .env;
# rode deploy/atualizar.sh depois).
#
# Aceita .pfx/.p12 (pede a senha do arquivo), certificado .crt/.cer/.pem (PEM ou DER) com a chave
# separada (.key/.pem, com ou sem senha). Se a TI mandar a cadeia intermediaria separada, junte antes:
#   cat certificado.crt cadeia.crt > certificado-completo.crt
#
# O que faz, em ordem:
#   1. confere ANTES de mexer em qualquer coisa: o certificado vale para o nome, a chave e dele, a
#      validade, e (ao ligar) se as portas 443 e 80 estao livres; com problema, para e explica;
#   2. guarda o certificado anterior (dados/certificados/anterior-<data>/) e uma copia do .env;
#   3. instala certificado.pem e chave.pem (sem senha) em dados/certificados/;
#   4. ajusta o .env (PORTAL_URL_BASE, PORTAL_URL_PUBLICA, PORTAL_BIND_PORTAL);
#   5. aplica: ao ligar/desligar roda deploy/atualizar.sh; ao renovar so regera o Caddy;
#   6. confere se https://<nome> responde com o certificado novo.

set -euo pipefail

PASTA_ORIGEM="$PWD"   # os arquivos informados sao relativos a pasta de onde o comando foi digitado
PASTA_PORTAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PASTA_PORTAL"

azul() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
aviso() { printf '\033[1;33m%s\033[0m\n' "$*"; }
erro() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
ok() { printf '\033[1;32m%s\033[0m\n' "$*"; }
ajuda() { sed -n '2,26p' "$0"; }

MODO="ligar"
SIM=0
ATUALIZAR=1
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --renovar) MODO="renovar"; shift ;;
    --desligar) MODO="desligar"; shift ;;
    --sim) SIM=1; shift ;;
    --sem-atualizar) ATUALIZAR=0; shift ;;
    -h|--help) ajuda; exit 0 ;;
    -*) erro "Opcao desconhecida: $1 (use --help)"; exit 1 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

if [ ! -f .env ] || [ ! -f docker-compose.yml ]; then
  erro "Nao foi possivel encontrar .env e docker-compose.yml. Rode a partir da pasta do portal."
  exit 1
fi
if ! grep -q '/certificados' docker-compose.yml; then
  erro "Esta versao do portal ainda nao tem o HTTPS. Atualize o codigo antes (git pull ou o branch certo)."
  exit 1
fi
command -v openssl >/dev/null || { erro "openssl nao encontrado (sudo apt install -y openssl)."; exit 1; }

# A pasta dados/ e do root (criada pelo container do portal).
if [ "$(id -u)" = 0 ]; then SUDO=(); else SUDO=(sudo); fi
if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi

PASTA_CERT="dados/certificados"
DATA="$(date +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT

valor_env() { sed -n "s/^$1=//p" .env | tr -d '\r' | tail -1; }

definir_env() {  # definir_env NOME VALOR: troca a linha ou acrescenta no fim
  if grep -q "^$1=" .env; then
    sed -i "s#^$1=.*#$1=$2#" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

tirar_env() {  # comenta a linha (sem apagar), para quem quiser ver o que havia
  if grep -q "^$1=" .env; then sed -i "s#^$1=#\# (desligar-https $DATA) $1=#" .env; fi
}

perguntar() {
  [ "$SIM" = 1 ] && return 0
  local r
  read -r -p "$1 [s/N] " r
  [[ "${r:-n}" =~ ^[sS] ]]
}

# ------------------------------------------------------------------ desligar
if [ "$MODO" = "desligar" ]; then
  IP="$(hostname -I | awk '{print $1}')"
  azul "Desligar o HTTPS"
  echo "O portal volta para http://$IP:8000 e os sistemas para http://$IP:<porta>."
  echo "Os arquivos do certificado ficam em $PASTA_CERT (sem uso)."
  perguntar "Continuar?" || { echo "Nada foi alterado."; exit 0; }
  cp .env ".env.antes-desligar-https-$DATA"
  definir_env PORTAL_URL_BASE "http://$IP"
  definir_env PORTAL_URL_PUBLICA "http://$IP:8000"
  tirar_env PORTAL_BIND_PORTAL
  if grep -qiE '^PORTAL_COOKIE_SEGURO=(1|true|sim|yes|on)' .env; then tirar_env PORTAL_COOKIE_SEGURO; fi
  ok "  .env ajustado (copia do anterior: .env.antes-desligar-https-$DATA)"
  if [ "$ATUALIZAR" = 1 ]; then
    bash deploy/atualizar.sh
  else
    aviso "Rode agora: bash deploy/atualizar.sh"
  fi
  exit 0
fi

# ------------------------------------------------------------------ ligar / renovar: entrada
if [ "$MODO" = "renovar" ]; then
  BASE="$(valor_env PORTAL_URL_BASE)"
  if [[ ! "$BASE" =~ ^https://([A-Za-z0-9.-]+)$ ]]; then
    erro "O HTTPS nao esta ligado (PORTAL_URL_BASE e '$BASE'). Para ligar: bash deploy/ligar-https.sh <nome> <certificado> [chave]"
    exit 1
  fi
  NOME="${BASH_REMATCH[1]}"
  [ ${#ARGS[@]} -ge 1 ] || { erro "Informe o certificado novo: bash deploy/ligar-https.sh --renovar certificado.pfx"; exit 1; }
  ARQ="${ARGS[0]}"; CHAVE="${ARGS[1]:-}"
else
  [ ${#ARGS[@]} -ge 2 ] || { ajuda; exit 1; }
  NOME="${ARGS[0],,}"; ARQ="${ARGS[1]}"; CHAVE="${ARGS[2]:-}"
  if [[ ! "$NOME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
    erro "Nome invalido: '$NOME' (ex.: portal.log20.com.br, sem https:// e sem porta)."
    exit 1
  fi
fi
absoluto() { case "$1" in /*|'') printf '%s' "$1" ;; *) printf '%s/%s' "$PASTA_ORIGEM" "$1" ;; esac; }
ARQ="$(absoluto "$ARQ")"; CHAVE="$(absoluto "$CHAVE")"
[ -f "$ARQ" ] || { erro "Arquivo nao encontrado: $ARQ"; exit 1; }
[ -z "$CHAVE" ] || [ -f "$CHAVE" ] || { erro "Arquivo nao encontrado: $CHAVE"; exit 1; }

# ------------------------------------------------------------------ conversao para PEM
azul "Lendo o certificado"
CERT="$TMP/certificado.pem"
KEY="$TMP/chave.pem"
umask 077

so_certificados() {  # so os blocos BEGIN/END CERTIFICATE (tira atributos e texto solto)
  [ -f "$1" ] || return 0
  awk '/-----BEGIN CERTIFICATE-----/{p=1} p{print} /-----END CERTIFICATE-----/{p=0}' "$1"
}

pkcs12() {  # pkcs12 <opcoes...>: tenta normal e, se falhar, com -legacy (pfx antigos do Windows)
  openssl pkcs12 -in "$ARQ" -passin env:SENHA_ARQUIVO "$@" 2>"$TMP/erro.txt" \
    || openssl pkcs12 -legacy -in "$ARQ" -passin env:SENHA_ARQUIVO "$@" 2>"$TMP/erro.txt"
}

case "${ARQ,,}" in
  *.pfx|*.p12)
    read -r -s -p "Senha do arquivo $(basename "$ARQ") (Enter se nao tiver): " SENHA_ARQUIVO; echo
    export SENHA_ARQUIVO
    if ! pkcs12 -clcerts -nokeys -out "$CERT"; then
      erro "Nao foi possivel abrir $ARQ (senha errada ou arquivo danificado): $(tail -1 "$TMP/erro.txt")"
      exit 1
    fi
    pkcs12 -cacerts -nokeys -out "$TMP/cadeia.pem" || true
    if ! pkcs12 -nocerts -nodes -out "$TMP/chave-bruta.pem" || ! grep -q 'PRIVATE KEY' "$TMP/chave-bruta.pem"; then
      erro "O arquivo $ARQ nao tem a chave privada. Peca a TI o .pfx com a chave."
      exit 1
    fi
    openssl pkey -in "$TMP/chave-bruta.pem" -out "$KEY"
    unset SENHA_ARQUIVO
    # A cadeia intermediaria vai depois do certificado (o navegador precisa dela).
    openssl x509 -in "$CERT" -out "$TMP/folha.pem"
    { cat "$TMP/folha.pem"; so_certificados "$TMP/cadeia.pem"; } > "$CERT"
    ;;
  *)
    [ -n "$CHAVE" ] || { erro "Informe tambem a chave: bash deploy/ligar-https.sh <nome> certificado.crt chave.key"; exit 1; }
    if grep -q 'BEGIN CERTIFICATE' "$ARQ"; then
      # Mantem a cadeia, se o arquivo tiver; tira so texto solto antes/depois dos blocos.
      so_certificados "$ARQ" > "$CERT"
    elif ! openssl x509 -inform der -in "$ARQ" -out "$CERT" 2>/dev/null; then
      erro "$ARQ nao e um certificado (PEM nem DER)."
      exit 1
    fi
    if grep -q 'ENCRYPTED' "$CHAVE"; then
      read -r -s -p "Senha da chave $(basename "$CHAVE"): " SENHA_CHAVE; echo
      export SENHA_CHAVE
      openssl pkey -in "$CHAVE" -passin env:SENHA_CHAVE -out "$KEY" 2>"$TMP/erro.txt" \
        || { erro "Nao foi possivel abrir a chave (senha errada?): $(tail -1 "$TMP/erro.txt")"; exit 1; }
      unset SENHA_CHAVE
    elif ! openssl pkey -in "$CHAVE" -out "$KEY" 2>/dev/null \
         && ! openssl pkey -inform der -in "$CHAVE" -out "$KEY" 2>/dev/null; then
      erro "$CHAVE nao e uma chave privada valida."
      exit 1
    fi
    ;;
esac
umask 022

# ------------------------------------------------------------------ conferencia
openssl x509 -in "$CERT" -noout >/dev/null 2>&1 || { erro "Certificado ilegivel depois da conversao."; exit 1; }
echo "  Para:        $(openssl x509 -in "$CERT" -noout -subject | sed 's/^subject= *//')"
echo "  Nomes:       $(openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | tail -n +2 | tr -d ' ' || true)"
echo "  Emitido por: $(openssl x509 -in "$CERT" -noout -issuer | sed 's/^issuer= *//')"
echo "  Vence em:    $(openssl x509 -in "$CERT" -noout -enddate | sed 's/^notAfter=//')"
echo "  Cadeia:      $(grep -c 'BEGIN CERTIFICATE' "$CERT") certificado(s) no arquivo"

PROBLEMAS=0
if ! openssl x509 -in "$CERT" -noout -checkhost "$NOME" | grep -q 'does match'; then
  erro "  O certificado NAO vale para $NOME. Peca a TI um certificado para esse nome (ou o curinga)."
  PROBLEMAS=1
fi
if [ "$(openssl x509 -in "$CERT" -noout -pubkey | openssl sha256)" != "$(openssl pkey -in "$KEY" -pubout | openssl sha256)" ]; then
  erro "  A chave NAO e deste certificado."
  PROBLEMAS=1
fi
if ! openssl x509 -in "$CERT" -noout -checkend 0 >/dev/null; then
  erro "  O certificado esta VENCIDO."
  PROBLEMAS=1
elif ! openssl x509 -in "$CERT" -noout -checkend $((30 * 86400)) >/dev/null; then
  aviso "  Atencao: vence em menos de 30 dias."
fi
if [ "$(openssl x509 -in "$CERT" -noout -subject_hash)" = "$(openssl x509 -in "$CERT" -noout -issuer_hash)" ] \
   && [ "$(grep -c 'BEGIN CERTIFICATE' "$CERT")" = 1 ]; then
  aviso "  Atencao: certificado autoassinado (de teste). Os navegadores vao mostrar aviso de site nao seguro."
elif [ "$(grep -c 'BEGIN CERTIFICATE' "$CERT")" = 1 ]; then
  aviso "  Atencao: falta a cadeia intermediaria (de quem emitiu: $(openssl x509 -in "$CERT" -noout -issuer | sed 's/^issuer= *//'))."
  aviso "  Sem ela, Firefox, celulares e alguns sistemas mostram erro de certificado. Junte o arquivo"
  aviso "  da cadeia que veio com o certificado (ex.: .ca-bundle) e rode de novo:"
  aviso "    cat certificado.crt cadeia.ca-bundle > certificado-completo.crt"
  if [ "$SIM" = 0 ] && ! perguntar "  Continuar mesmo sem a cadeia?"; then
    echo "Nada foi alterado."; exit 1
  fi
fi
[ "$PROBLEMAS" = 0 ] || { erro "Nada foi alterado."; exit 1; }
ok "  Certificado e chave conferem."

if [ "$MODO" = "ligar" ]; then
  azul "Conferindo a VM"
  P443="$(valor_env PORTAL_PORTA_HTTPS)"; P443="${P443:-443}"
  P80="$(valor_env PORTAL_PORTA_HTTP)"; P80="${P80:-80}"
  for porta in "$P443" "$P80"; do
    if command -v ss >/dev/null; then
      USO="$("${SUDO[@]}" ss -ltnpH "sport = :$porta" 2>/dev/null || true)"
    elif (exec 3<>"/dev/tcp/127.0.0.1/$porta") 2>/dev/null; then
      USO="algum programa responde na porta $porta"
    else
      USO=""
    fi
    # Ja ligado antes: quem ocupa a porta e o proprio Caddy do portal (docker-proxy), tudo bem.
    if [ -n "$USO" ] && ! grep -q 'docker-proxy' <<<"$USO" \
       && ! "${DOCKER[@]}" ps --filter name=log20-caddy --format '{{.Ports}}' 2>/dev/null | grep -q ":$porta->"; then
      erro "  A porta $porta ja esta em uso nesta VM:"
      erro "  $USO"
      erro "  Libere a porta ou use outra no .env (PORTAL_PORTA_HTTPS / PORTAL_PORTA_HTTP). Nada foi alterado."
      exit 1
    fi
  done
  ok "  Portas $P443 e $P80 livres."
  IPS=" $(hostname -I | xargs) "
  RESOLVE="$(getent ahostsv4 "$NOME" | awk '{print $1; exit}' || true)"
  if [ -z "$RESOLVE" ]; then
    aviso "  $NOME ainda nao existe no DNS. Os usuarios so vao chegar quando a TI criar o nome apontando para$IPS(esta VM)."
  elif [[ "$IPS" != *" $RESOLVE "* ]]; then
    aviso "  $NOME aponta para $RESOLVE, que nao e esta VM ($(xargs <<<"$IPS")). Confira o DNS com a TI."
  else
    ok "  $NOME aponta para esta VM ($RESOLVE)."
  fi
fi

# ------------------------------------------------------------------ aplicar
azul "Aplicar"
if [ "$MODO" = "ligar" ]; then
  echo "O portal vai ficar em https://$NOME e os sistemas em https://$NOME:<porta>."
  echo "A porta 8000 fecha para a rede (o portal passa a ser so pelo https)."
  echo "Os sistemas com Supabase (ex.: Panorama) serao reconstruidos com o endereco novo (alguns minutos)."
else
  echo "O certificado de https://$NOME sera trocado. Os sistemas continuam no ar."
fi
perguntar "Continuar?" || { echo "Nada foi alterado."; exit 0; }

"${SUDO[@]}" mkdir -p "$PASTA_CERT"
"${SUDO[@]}" chmod 700 "$PASTA_CERT"
if "${SUDO[@]}" test -f "$PASTA_CERT/certificado.pem" || "${SUDO[@]}" test -f "$PASTA_CERT/chave.pem"; then
  "${SUDO[@]}" mkdir -p "$PASTA_CERT/anterior-$DATA"
  "${SUDO[@]}" sh -c "mv '$PASTA_CERT'/certificado.pem '$PASTA_CERT'/chave.pem '$PASTA_CERT/anterior-$DATA/' 2>/dev/null || true"
  echo "  Certificado anterior guardado em $PASTA_CERT/anterior-$DATA/"
fi
"${SUDO[@]}" install -m 644 "$CERT" "$PASTA_CERT/certificado.pem"
"${SUDO[@]}" install -m 600 "$KEY" "$PASTA_CERT/chave.pem"
ok "  Arquivos instalados em $PASTA_CERT/"

if [ "$MODO" = "ligar" ]; then
  cp .env ".env.antes-https-$DATA"
  definir_env PORTAL_URL_BASE "https://$NOME"
  definir_env PORTAL_URL_PUBLICA "https://$NOME"
  definir_env PORTAL_BIND_PORTAL "127.0.0.1"
  if grep -qiE '^PORTAL_COOKIE_SEGURO=' .env; then definir_env PORTAL_COOKIE_SEGURO "true"; fi
  ok "  .env ajustado (copia do anterior: .env.antes-https-$DATA)"
  if [ "$ATUALIZAR" = 0 ]; then
    aviso "Rode agora: bash deploy/atualizar.sh"
    exit 0
  fi
  bash deploy/atualizar.sh
else
  if [ "$ATUALIZAR" = 0 ]; then
    aviso "Para usar o certificado novo: Solucoes > Recarregar catalogo (ou bash deploy/atualizar.sh)."
    exit 0
  fi
  azul "Recarregando o Caddy com o certificado novo"
  "${DOCKER[@]}" compose exec -T portal python -m marketplace sincronizar
fi

# ------------------------------------------------------------------ conferencia final
azul "Conferindo https://$NOME"
P443="$(valor_env PORTAL_PORTA_HTTPS)"; P443="${P443:-443}"
ESPERADO="$(openssl x509 -in "$CERT" -noout -fingerprint -sha256)"
for _ in $(seq 1 30); do
  SERVIDO="$(openssl s_client -connect "127.0.0.1:$P443" -servername "$NOME" </dev/null 2>/dev/null \
             | openssl x509 -noout -fingerprint -sha256 2>/dev/null || true)"
  if [ "$SERVIDO" = "$ESPERADO" ]; then
    RESPOSTA="$(curl -sk --max-time 10 --resolve "$NOME:$P443:127.0.0.1" "https://$NOME:$P443/api/saude" || true)"
    if [[ "$RESPOSTA" == *'"ok"'* ]]; then
      ok "  https://$NOME responde com o certificado novo."
      echo
      echo "  Portal:   https://$NOME"
      echo "  Sistemas: https://$NOME:<porta> (veja em Soluções)"
      echo "  Validade e avisos de vencimento: tela Alertas."
      exit 0
    fi
  fi
  sleep 2
done
erro "  https://$NOME ainda nao responde com o certificado novo."
erro "  Veja a tela Alertas (motivo do HTTPS) e os logs: ${DOCKER[*]} compose logs --tail 50 caddy portal"
exit 1
