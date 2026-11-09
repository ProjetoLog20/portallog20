"""Camada fina sobre a linha de comando `docker`.

Usar a CLI (em vez do SDK Python) evita dependencias extras e funciona igual no
Windows, Linux e macOS: basta o Docker Desktop/Engine estar instalado.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

_WINDOWS_FALLBACKS = [
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    r"C:\ProgramData\DockerDesktop\version-bin\docker.exe",
]


class DockerError(RuntimeError):
    """Falha ao executar um comando docker."""


def docker_bin() -> str:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in _WINDOWS_FALLBACKS:
        if Path(candidate).exists():
            return candidate
    raise DockerError(
        "Comando 'docker' nao encontrado. Instale o Docker Desktop ou adicione-o ao PATH."
    )


def _run(args: list[str], timeout: float = 60, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        return subprocess.run(
            [docker_bin(), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=full_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f"Tempo esgotado executando: docker {' '.join(args[:2])}") from exc


def _check(result: subprocess.CompletedProcess, what: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DockerError(f"{what}: {detail or 'erro desconhecido'}")
    return result.stdout


@dataclass
class DockerStatus:
    ok: bool
    versao: str | None = None
    erro: str | None = None


def status() -> DockerStatus:
    try:
        result = _run(["version", "--format", "{{.Server.Version}}"], timeout=15)
    except DockerError as exc:
        return DockerStatus(ok=False, erro=str(exc))
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        msg = detail[-1] if detail else "Docker nao respondeu"
        return DockerStatus(ok=False, erro=f"O Docker nao esta rodando. Abra o Docker Desktop. ({msg})")
    return DockerStatus(ok=True, versao=result.stdout.strip())


def image_exists(image: str) -> bool:
    return _run(["image", "inspect", image], timeout=30).returncode == 0


def container_inspect(name: str) -> dict | None:
    result = _run(["container", "inspect", name], timeout=30)
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout or "[]")
    return data[0] if data else None


def stream(args: list[str], on_line: Callable[[str], None], env: dict[str, str] | None = None) -> int:
    """Executa um comando longo (ex.: build) repassando cada linha de saida."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.Popen(
        [docker_bin(), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=full_env,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    return proc.wait()


def build(context: Path, dockerfile: Path, image: str, on_line: Callable[[str], None],
          contextos: dict[str, Path] | None = None) -> None:
    """`contextos`: contextos extras do BuildKit (COPY --from=<nome> no Dockerfile)."""
    extras = [a for nome, pasta in (contextos or {}).items() for a in ("--build-context", f"{nome}={pasta}")]
    code = stream(
        ["build", "--progress=plain", "-t", image, "-f", str(dockerfile), *extras, str(context)],
        on_line,
        env={"DOCKER_BUILDKIT": "1"},
    )
    if code != 0:
        raise DockerError(f"docker build terminou com codigo {code}")


def run_container(
    *,
    name: str,
    image: str,
    host_port: int,
    container_port: int,
    bind: str = "127.0.0.1",
    publicar: bool = True,
    rede: str = "",
    env: dict[str, str],
    volumes: dict[str, str],
    labels: dict[str, str],
) -> str:
    """Cria e inicia o container.

    As variaveis de ambiente sao passadas como `-e NOME` (sem valor) e lidas do
    ambiente do processo docker, para que segredos nao aparecam na linha de comando.

    Com `rede`, o container entra nessa rede do Docker e e alcancado pelo nome; com
    `publicar=False`, nenhuma porta fica aberta na maquina (so o proxy na mesma rede chega nele).
    """
    args = [
        "run", "-d",
        "--name", name,
        "--restart", "unless-stopped",
    ]
    if publicar:
        args += ["-p", f"{bind}:{host_port}:{container_port}"]
    if rede:
        args += ["--network", rede, "--network-alias", name]
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    for key in env:
        args += ["-e", key]
    for volume, target in volumes.items():
        args += ["-v", f"{volume}:{target}"]
    args.append(image)
    return _check(_run(args, timeout=120, env=env), "Falha ao iniciar o container").strip()


def stop(name: str, timeout: int = 30) -> None:
    _check(_run(["stop", "-t", str(timeout), name], timeout=timeout + 30), "Falha ao parar o container")


def remove(name: str) -> None:
    result = _run(["rm", "-f", name], timeout=60)
    if result.returncode != 0 and "No such container" not in (result.stderr or ""):
        _check(result, "Falha ao remover o container")


def executar_em(container: str, args: list[str], timeout: float = 60) -> tuple[bool, str]:
    """docker exec: roda um comando dentro de um container. Devolve (ok, saida)."""
    resultado = _run(["exec", container, *args], timeout=timeout)
    saida = "\n".join(p for p in (resultado.stdout, resultado.stderr) if p).strip()
    return resultado.returncode == 0, saida


def logs(name: str, tail: int = 300) -> str:
    # stream() junta stdout e stderr do container na ordem em que foram escritos.
    lines: list[str] = []
    code = stream(["logs", "--tail", str(tail), name], lines.append)
    return "\n".join(lines) if code == 0 else ""


def remove_volumes(volumes: Iterable[str]) -> None:
    for volume in volumes:
        result = _run(["volume", "rm", "-f", volume], timeout=60)
        if result.returncode != 0:
            _check(result, f"Falha ao remover o volume {volume}")


def para_arquivo(args: list[str], destino: Path, env: dict[str, str] | None = None, timeout: float = 3600) -> None:
    """Roda `docker <args>` gravando a saida (binaria) direto num arquivo, sem passar pela memoria.

    Usado pelo backup (pg_dump, tar de volumes). `env` entra no ambiente do processo docker:
    com `-e NOME` nos args, o valor chega ao container sem aparecer na linha de comando.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    with open(destino, "wb") as saida:
        try:
            proc = subprocess.run([docker_bin(), *args], stdout=saida, stderr=subprocess.PIPE,
                                  timeout=timeout, env=full_env)
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"Tempo esgotado executando: docker {' '.join(args[:3])}") from exc
    if proc.returncode != 0:
        detalhe = proc.stderr.decode("utf-8", "replace").strip()
        raise DockerError(detalhe[-800:] or f"docker terminou com codigo {proc.returncode}")


def de_arquivo(args: list[str], origem: Path, env: dict[str, str] | None = None, timeout: float = 3600) -> str:
    """Roda `docker <args>` lendo a entrada de um arquivo (pg_restore, tar -x). Devolve a saida."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    with open(origem, "rb") as entrada:
        try:
            proc = subprocess.run([docker_bin(), *args], stdin=entrada, capture_output=True,
                                  timeout=timeout, env=full_env)
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"Tempo esgotado executando: docker {' '.join(args[:3])}") from exc
    saida = "\n".join(p.decode("utf-8", "replace") for p in (proc.stdout, proc.stderr) if p).strip()
    if proc.returncode != 0:
        raise DockerError(saida[-800:] or f"docker terminou com codigo {proc.returncode}")
    return saida


def volume_existe(nome: str) -> bool:
    return _run(["volume", "inspect", nome], timeout=30).returncode == 0
