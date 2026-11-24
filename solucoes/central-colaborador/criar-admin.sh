#!/usr/bin/env bash
# Cria o primeiro administrador da Central do Colaborador quando ela usa o PostgreSQL do servidor
# (o banco novo comeca sem nenhum usuario). Rodar uma vez, na VM, na pasta do portal, com a
# Central ja iniciada pelo portal:
#
#   bash solucoes/central-colaborador/criar-admin.sh
#
# Pede matricula, nome e uma senha temporaria: no primeiro acesso a propria Central pede para trocar.
# Nao altera usuarios que ja existem. Os dados vao pela entrada padrao (nao aparecem na linha de
# comando nem no historico).

set -euo pipefail

CONTAINER="log20-central-colaborador"
azul() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
erro() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
ok() { printf '\033[1;32m%s\033[0m\n' "$*"; }

if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi

if [ "$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
  erro "A Central do Colaborador nao esta rodando ($CONTAINER). Inicie pelo portal e rode de novo."
  exit 1
fi
if ! "${DOCKER[@]}" exec "$CONTAINER" sh -c '[ -n "$DATABASE_URL" ]'; then
  erro "A Central esta sem DATABASE_URL (banco local SQLite): este comando e so para o PostgreSQL."
  exit 1
fi

azul "Primeiro administrador da Central do Colaborador"
read -r -p "Matricula (login): " MATRICULA
read -r -p "Nome completo: " NOME
read -r -s -p "Senha temporaria (minimo 8 caracteres): " SENHA; echo
read -r -s -p "Repita a senha: " SENHA2; echo
if [[ ! "$MATRICULA" =~ ^[A-Za-z0-9._-]{1,40}$ ]]; then erro "Matricula invalida (letras, numeros, . _ -)."; exit 1; fi
if [ -z "${NOME// /}" ]; then erro "Informe o nome."; exit 1; fi
if [ "${#SENHA}" -lt 8 ]; then erro "A senha precisa ter pelo menos 8 caracteres."; exit 1; fi
if [ "$SENHA" != "$SENHA2" ]; then erro "As senhas nao conferem."; exit 1; fi

# O script roda dentro do container, com o banco e o bcrypt da propria Central.
JS='
const path = require("path");
const base = "/app/backend";
const db = require(path.join(base, "src/db"));
const bcrypt = require(require.resolve("bcrypt", { paths: [base] }));
let entrada = "";
process.stdin.on("data", (c) => { entrada += c; });
process.stdin.on("end", async () => {
  try {
    const { matricula, nome, senha } = JSON.parse(entrada);
    const existe = await db.query("SELECT perfil FROM colaboradores WHERE matricula = ?", [matricula]);
    if (existe.rows.length) {
      console.error(`Ja existe um usuario com a matricula ${matricula} (perfil ${existe.rows[0].perfil}). Nada foi alterado.`);
      process.exit(2);
    }
    const hash = await bcrypt.hash(senha, 10);
    await db.query(
      "INSERT INTO colaboradores (matricula, nome_completo, senha_hash, perfil, status, primeiro_acesso, senha_temporaria) VALUES (?, ?, ?, ?, ?, ?, ?)",
      [matricula, nome, hash, "admin", "Ativo", true, true]);
    console.log(`Administrador ${matricula} criado.`);
    process.exit(0);
  } catch (e) {
    console.error("Nao foi possivel criar o administrador: " + e.message);
    console.error("As tabelas existem? Elas sao criadas pelo portal ao iniciar a Central (veja os logs do Iniciar).");
    process.exit(1);
  }
});
'

printf '%s' "$(MATRICULA="$MATRICULA" NOME="$NOME" SENHA="$SENHA" python3 -c 'import json,os; print(json.dumps({"matricula": os.environ["MATRICULA"], "nome": os.environ["NOME"], "senha": os.environ["SENHA"]}))')" \
  | "${DOCKER[@]}" exec -i "$CONTAINER" node -e "$JS"
ok "Pronto. Entre na Central com a matricula $MATRICULA e a senha temporaria; ela vai pedir uma senha nova."
