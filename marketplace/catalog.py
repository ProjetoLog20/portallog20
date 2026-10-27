"""Catalogo de solucoes: cada solucao e descrita por um arquivo `solucao.toml`.

Para publicar uma nova solucao no portal basta criar
`solucoes/<id>/solucao.toml` seguindo o modelo da Central do Colaborador.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
TIPOS_CONFIG = {"texto", "segredo", "booleano"}
EXTENSOES_LOGO = {".png", ".jpg", ".jpeg", ".svg", ".webp"}
# Repositorio por HTTPS, sem usuario/senha na URL (o token vai em PORTAL_GIT_TOKEN).
REPO_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/-]+$")
REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}$")


class ManifestoInvalido(ValueError):
    pass


@dataclass
class Configuracao:
    nome: str
    rotulo: str
    tipo: str = "texto"
    padrao: str = ""
    ajuda: str = ""


@dataclass
class AcessoDemo:
    perfil: str
    usuario: str
    senha: str


@dataclass
class Solucao:
    id: str
    nome: str
    resumo: str
    descricao: str
    categoria: str
    versao: str
    responsavel: str
    # Codigo local (pasta) OU repositorio Git; com repositorio, o portal clona/atualiza antes do build.
    contexto_build: Path | None
    dockerfile_nome: str
    imagem: str
    porta_container: int
    porta_host: int
    caminho_saude: str
    caminho_abrir: str
    ambiente: dict[str, str]
    volumes: dict[str, str]
    segredos: list[str]
    configuracoes: list[Configuracao] = field(default_factory=list)
    acessos_demo: list[AcessoDemo] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    manifesto: Path | None = None
    logo: Path | None = None
    repositorio: str = ""
    ref: str = ""
    subpasta: str = "."

    @property
    def container(self) -> str:
        return f"log20-{self.id}"

    def nome_volume(self, chave: str) -> str:
        return f"log20-{self.id}-{chave}"

    def volumes_docker(self) -> dict[str, str]:
        return {self.nome_volume(k): destino for k, destino in self.volumes.items()}


def _exigir(dados: dict, chave: str, origem: Path):
    if chave not in dados or dados[chave] in (None, ""):
        raise ManifestoInvalido(f"{origem}: campo obrigatorio '{chave}' ausente")
    return dados[chave]


def carregar_manifesto(caminho: Path) -> Solucao:
    with caminho.open("rb") as fh:
        try:
            dados = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ManifestoInvalido(f"{caminho}: TOML invalido ({exc})") from exc

    base = caminho.parent
    sid = str(_exigir(dados, "id", caminho))
    if not ID_RE.match(sid):
        raise ManifestoInvalido(f"{caminho}: id '{sid}' deve usar letras minusculas, numeros e hifens")

    build = _exigir(dados, "build", caminho)
    execucao = _exigir(dados, "execucao", caminho)

    repositorio = str(build.get("repositorio", "")).strip()
    ref = str(build.get("ref", "main")).strip()
    subpasta = str(build.get("subpasta", ".")).strip() or "."
    if repositorio:
        if not REPO_RE.match(repositorio):
            raise ManifestoInvalido(
                f"{caminho}: repositorio deve ser uma URL https:// sem usuario ou senha "
                "(o acesso a repositorios privados vem de PORTAL_GIT_TOKEN)"
            )
        if not REF_RE.match(ref) or ".." in ref:
            raise ManifestoInvalido(f"{caminho}: ref '{ref}' invalido (use o nome de um branch ou tag)")
        if Path(subpasta).is_absolute() or ".." in Path(subpasta).parts:
            raise ManifestoInvalido(f"{caminho}: subpasta deve ser um caminho dentro do repositorio")
        contexto = None
    else:
        contexto = (base / _exigir(build, "contexto", caminho)).resolve()
    dockerfile_nome = str(build.get("dockerfile", "Dockerfile"))
    if Path(dockerfile_nome).is_absolute() or ".." in Path(dockerfile_nome).parts:
        raise ManifestoInvalido(f"{caminho}: dockerfile deve ser um caminho dentro do codigo da solucao")

    logo = None
    if dados.get("logo"):
        logo = (base / str(dados["logo"])).resolve()
        if logo.suffix.lower() not in EXTENSOES_LOGO:
            raise ManifestoInvalido(f"{caminho}: logo deve ser {', '.join(sorted(EXTENSOES_LOGO))}")
        if base.resolve() not in logo.parents:
            raise ManifestoInvalido(f"{caminho}: a logo deve ficar dentro da pasta da solucao")
        if not logo.is_file():
            raise ManifestoInvalido(f"{caminho}: arquivo de logo nao encontrado ({logo.name})")

    for chave, valor in execucao.get("volumes", {}).items():
        if not isinstance(valor, str) or not valor.startswith("/"):
            raise ManifestoInvalido(
                f"{caminho}: volume '{chave}' deve ser um caminho absoluto do container "
                "(campos soltos depois de [execucao.volumes] entram nessa tabela; mova-os para o topo)"
            )
    for chave, valor in execucao.get("ambiente", {}).items():
        if isinstance(valor, (dict, list)):
            raise ManifestoInvalido(f"{caminho}: variavel de ambiente '{chave}' deve ser texto")

    configuracoes = []
    for item in dados.get("configuracoes", []):
        tipo = item.get("tipo", "texto")
        if tipo not in TIPOS_CONFIG:
            raise ManifestoInvalido(f"{caminho}: tipo de configuracao '{tipo}' invalido")
        configuracoes.append(
            Configuracao(
                nome=str(_exigir(item, "nome", caminho)),
                rotulo=str(item.get("rotulo", item["nome"])),
                tipo=tipo,
                padrao=str(item.get("padrao", "")),
                ajuda=str(item.get("ajuda", "")),
            )
        )

    return Solucao(
        id=sid,
        nome=str(_exigir(dados, "nome", caminho)),
        resumo=str(dados.get("resumo", "")),
        # Quebra simples vira espaco; linha em branco separa paragrafos.
        descricao="\n\n".join(
            " ".join(p.split()) for p in re.split(r"\n\s*\n", str(dados.get("descricao", "")).strip())
        ),
        categoria=str(dados.get("categoria", "Geral")),
        versao=str(dados.get("versao", "")),
        responsavel=str(dados.get("responsavel", "")),
        contexto_build=contexto,
        dockerfile_nome=dockerfile_nome,
        imagem=str(build.get("imagem", f"log20/{sid}:local")),
        porta_container=int(_exigir(execucao, "porta_container", caminho)),
        porta_host=int(_exigir(execucao, "porta_host", caminho)),
        caminho_saude=str(execucao.get("caminho_saude", "/")),
        caminho_abrir=str(execucao.get("caminho_abrir", "/")),
        ambiente={k: str(v) for k, v in execucao.get("ambiente", {}).items()},
        volumes={k: str(v) for k, v in execucao.get("volumes", {}).items()},
        segredos=[str(s) for s in dados.get("segredos", [])],
        configuracoes=configuracoes,
        acessos_demo=[
            AcessoDemo(perfil=str(a["perfil"]), usuario=str(a["usuario"]), senha=str(a["senha"]))
            for a in dados.get("acessos_demo", [])
        ],
        tags=[str(t) for t in dados.get("tags", [])],
        manifesto=caminho,
        logo=logo,
        repositorio=repositorio,
        ref=ref if repositorio else "",
        subpasta=subpasta,
    )


def carregar_catalogo(pasta: Path) -> tuple[dict[str, Solucao], list[str]]:
    """Le todas as solucoes. Manifestos com erro sao reportados, nao derrubam o portal."""
    solucoes: dict[str, Solucao] = {}
    erros: list[str] = []
    portas: dict[int, str] = {}
    for caminho in sorted(pasta.glob("*/solucao.toml")):
        try:
            sol = carregar_manifesto(caminho)
        except (ManifestoInvalido, KeyError, ValueError) as exc:
            erros.append(str(exc))
            continue
        if sol.id in solucoes:
            erros.append(f"{caminho}: id '{sol.id}' repetido")
            continue
        if sol.porta_host in portas:
            erros.append(f"{caminho}: porta {sol.porta_host} ja usada por '{portas[sol.porta_host]}'")
            continue
        portas[sol.porta_host] = sol.id
        solucoes[sol.id] = sol
    return solucoes, erros
