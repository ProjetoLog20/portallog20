"""Banco PostgreSQL do servidor: um banco e um usuario por solucao.

O portal nao fala com o PostgreSQL pela rede nem precisa de driver: roda `psql` dentro do
container do banco (`docker exec`), como o superusuario local. Os nomes saem do id da
solucao (so letras minusculas, numeros e _) e a senha e hexadecimal, entao nada digitado por
alguem entra no SQL.
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

from . import docker_cli

PORTA = 5432
_NOME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


class ErroBanco(RuntimeError):
    pass


@dataclass
class Credenciais:
    host: str
    porta: int
    nome: str
    usuario: str
    senha: str

    @property
    def url(self) -> str:
        return f"postgresql://{self.usuario}:{quote(self.senha, safe='')}@{self.host}:{self.porta}/{self.nome}"

    def valores(self) -> dict[str, str]:
        return {"db_host": self.host, "db_porta": str(self.porta), "db_nome": self.nome,
                "db_usuario": self.usuario, "db_senha": self.senha, "db_url": self.url}


def nomes(sid: str) -> tuple[str, str]:
    """Id da solucao -> (banco, usuario). "jornada-lideranca" -> ("jornada_lideranca", "app_jornada_lideranca")."""
    base = sid.replace("-", "_")
    nome, usuario = base, f"app_{base}"
    for n in (nome, usuario):
        if not _NOME_RE.match(n):
            raise ErroBanco(f"Nome de banco invalido gerado a partir de '{sid}': {n}")
    return nome, usuario


def nova_senha() -> str:
    return secrets.token_hex(24)


def _psql(container: str, sql: str) -> str:
    ok, saida = docker_cli.executar_em(
        container, ["psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", "-tAc", sql], timeout=60
    )
    if not ok:
        raise ErroBanco(f"PostgreSQL recusou o comando: {saida[-400:]}")
    return saida.strip()


def aguardar(container: str, segundos: int = 60) -> None:
    info = docker_cli.container_inspect(container)
    if not info or not info.get("State", {}).get("Running"):
        raise ErroBanco(
            f"O banco do servidor ({container}) nao esta rodando. Na pasta do portal: docker compose up -d postgres"
        )
    prazo = time.time() + segundos
    while True:
        ok, _ = docker_cli.executar_em(container, ["pg_isready", "-U", "postgres"], timeout=10)
        if ok:
            return
        if time.time() > prazo:
            raise ErroBanco(f"O banco do servidor ({container}) nao respondeu em {segundos}s")
        time.sleep(2)


def garantir(container: str, sid: str, senha: str | None, registrar: Callable[[str], None]) -> tuple[Credenciais, bool]:
    """Cria (ou confere) o usuario e o banco da solucao. Devolve (credenciais, banco_novo).

    Com `senha` (ja guardada pelo portal), so cria o que faltar. Sem ela, gera uma nova e
    aplica ao usuario (existente ou nao). Nunca apaga nada.
    """
    aguardar(container)
    nome, usuario = nomes(sid)
    senha_nova = senha is None
    senha = senha or nova_senha()
    if not re.fullmatch(r"[0-9a-f]{16,128}", senha):
        raise ErroBanco("Senha guardada do banco em formato inesperado. Apague-a do portal para gerar outra.")

    existe_usuario = _psql(container, f"SELECT 1 FROM pg_roles WHERE rolname = '{usuario}'") == "1"
    if not existe_usuario:
        _psql(container, f"CREATE ROLE {usuario} LOGIN PASSWORD '{senha}'")
        registrar(f"Banco: usuario {usuario} criado.")
    elif senha_nova:
        _psql(container, f"ALTER ROLE {usuario} WITH LOGIN PASSWORD '{senha}'")
        registrar(f"Banco: senha do usuario {usuario} renovada.")

    existe_banco = _psql(container, f"SELECT 1 FROM pg_database WHERE datname = '{nome}'") == "1"
    if not existe_banco:
        _psql(container, f"CREATE DATABASE {nome} OWNER {usuario} ENCODING 'UTF8' TEMPLATE template0")
        # Outros usuarios do servidor (outras solucoes) nao conectam neste banco.
        _psql(container, f"REVOKE ALL ON DATABASE {nome} FROM PUBLIC")
        registrar(f"Banco: banco {nome} criado (vazio) no PostgreSQL do servidor.")
    else:
        registrar(f"Banco: usando o banco {nome} do PostgreSQL do servidor.")
    return Credenciais(host=container, porta=PORTA, nome=nome, usuario=usuario, senha=senha), not existe_banco


def preencher(modelos: dict[str, str], cred: Credenciais) -> dict[str, str]:
    """{"DB_HOST": "{db_host}"} -> {"DB_HOST": "log20-postgres"}."""
    valores = cred.valores()
    return {nome: re.sub(r"\{(db_[a-z]+)\}", lambda m: valores[m.group(1)], modelo) for nome, modelo in modelos.items()}
