"""Baixa e atualiza o codigo das solucoes a partir de repositorios Git.

Cada repositorio+ref vira uma pasta em `dados/fontes/`. Na primeira instalacao o portal faz
um clone raso; em "Atualizar" busca a versao mais nova do branch e descarta o que estiver
diferente na pasta (ela e so uma copia de trabalho do portal, ninguem edita ali).

Repositorios privados: o token de leitura vem de PORTAL_GIT_TOKEN e e enviado so para o
host configurado (PORTAL_GIT_HOST, padrao github.com), por variavel de ambiente do git,
sem aparecer na URL, na linha de comando nem nos logs.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

_WINDOWS_FALLBACKS = [r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe"]

_travas: dict[str, threading.Lock] = {}
_travas_lock = threading.Lock()


class ErroGit(RuntimeError):
    """Falha ao baixar o codigo de uma solucao."""


@dataclass
class Versao:
    commit: str
    resumo: str
    data: str


def git_bin() -> str:
    achado = shutil.which("git")
    if achado:
        return achado
    for candidato in _WINDOWS_FALLBACKS:
        if Path(candidato).exists():
            return candidato
    raise ErroGit("Comando 'git' nao encontrado. Instale o Git para instalar solucoes a partir de repositorios.")


def pasta_do_repositorio(base: Path, repositorio: str, ref: str) -> Path:
    url = urlparse(repositorio)
    legivel = re.sub(r"[^A-Za-z0-9]+", "-", f"{url.path.strip('/').removesuffix('.git')}-{ref}").strip("-")
    curto = hashlib.sha256(f"{repositorio}#{ref}".encode()).hexdigest()[:8]
    return base / f"{legivel[:60]}-{curto}"


def _trava(chave: str) -> threading.Lock:
    with _travas_lock:
        return _travas.setdefault(chave, threading.Lock())


def _ambiente(repositorio: str, token: str, token_host: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # nunca fica esperando senha
    env["GCM_INTERACTIVE"] = "never"  # Git Credential Manager (Windows) sem janelas
    host = urlparse(repositorio).hostname or ""
    if token and host.lower() == token_host.lower():
        credencial = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        env[f"GIT_CONFIG_KEY_{n}"] = f"http.https://{host}/.extraheader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"Authorization: Basic {credencial}"
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


def _executar(args: list[str], env: dict[str, str], ao_registrar: Callable[[str], None],
              segredos: tuple[str, ...] = ()) -> str:
    proc = subprocess.Popen(
        [git_bin(), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.stdout is not None
    linhas = []
    for linha in proc.stdout:
        linha = linha.rstrip("\n")
        for segredo in segredos:
            if segredo:
                linha = linha.replace(segredo, "***")
        linhas.append(linha)
        ao_registrar("  " + linha)
    if proc.wait() != 0:
        saida = "\n".join(linhas)
        if re.search(r"Authentication failed|could not read Username|terminal prompts disabled|"
                     r"Repository not found|403", saida, re.I):
            raise ErroGit(
                "Sem acesso ao repositorio. Confira a URL e, se ele for privado, defina "
                "PORTAL_GIT_TOKEN com um token de leitura do GitHub."
            )
        if re.search(r"Remote branch .* not found|couldn't find remote ref", saida, re.I):
            raise ErroGit("O branch ou tag informado em 'ref' nao existe no repositorio.")
        acao = args[2] if args[0] == "-C" else args[0]
        raise ErroGit(f"git {acao} falhou. Veja o log acima.")
    return "\n".join(linhas)


def atualizar(
    repositorio: str,
    ref: str,
    base: Path,
    ao_registrar: Callable[[str], None],
    token: str = "",
    token_host: str = "github.com",
) -> tuple[Path, Versao]:
    """Garante a pasta com o codigo do branch/tag `ref` e devolve onde ficou e em qual commit."""
    pasta = pasta_do_repositorio(base, repositorio, ref)
    env = _ambiente(repositorio, token, token_host)
    segredos = (token,)
    with _trava(str(pasta)):
        if (pasta / ".git").is_dir():
            ao_registrar(f"$ git fetch {repositorio} {ref}")
            _executar(["-C", str(pasta), "remote", "set-url", "origin", repositorio], env, ao_registrar, segredos)
            _executar(["-C", str(pasta), "fetch", "--depth", "1", "origin", ref], env, ao_registrar, segredos)
            _executar(["-C", str(pasta), "reset", "--hard", "FETCH_HEAD"], env, ao_registrar, segredos)
            _executar(["-C", str(pasta), "clean", "-fdxq"], env, ao_registrar, segredos)
        else:
            if pasta.exists():  # sobra de um clone interrompido
                shutil.rmtree(pasta)
            pasta.parent.mkdir(parents=True, exist_ok=True)
            ao_registrar(f"$ git clone --depth 1 --branch {ref} {repositorio}")
            _executar(["clone", "--depth", "1", "--branch", ref, repositorio, str(pasta)], env, ao_registrar, segredos)
        info = subprocess.run(
            [git_bin(), "-C", str(pasta), "log", "-1", "--format=%h%x1f%s%x1f%cI"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        ).stdout.strip().split("\x1f")
    commit, resumo, data = (info + ["", "", ""])[:3]
    return pasta, Versao(commit=commit, resumo=resumo, data=data)
