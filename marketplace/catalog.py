"""Catalogo de solucoes: cada solucao e descrita por um arquivo `solucao.toml`.

Para publicar uma nova solucao no portal basta criar
`solucoes/<id>/solucao.toml` seguindo o modelo da Central do Colaborador.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
TIPOS_CONFIG = {"texto", "segredo", "booleano"}
EXTENSOES_LOGO = {".png", ".jpg", ".jpeg", ".svg", ".webp"}
# Repositorio por HTTPS, sem usuario/senha na URL (o token vai em PORTAL_GIT_TOKEN).
REPO_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/-]+$")
PUBLICO_RE = re.compile(r"^/[A-Za-z0-9._~/-]*\*?$")
REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}$")


# Prefixos que os empacotadores de frontend so leem na construcao (e gravam no site): sempre vao
# para o docker build, mesmo sem `construcao = true` no manifesto.
VARS_PUBLICAS_BUILD = re.compile(r"^(VITE_|REACT_APP_|NEXT_PUBLIC_|VUE_APP_|NUXT_PUBLIC_|PUBLIC_|EXPO_PUBLIC_)")


def memoria_mb(valor) -> int:
    """'512m', '1g', '1.5g', '768' (MB) ou 768 -> megabytes. 0 = sem limite."""
    texto = str(valor).strip().lower().replace(",", ".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|mb|mib|g|gb|gib)?", texto)
    if not m:
        raise ValueError(f"memoria invalida: '{valor}' (use, por exemplo, 512m ou 1.5g)")
    n = float(m.group(1)) * (1024 if (m.group(2) or "m").startswith("g") else 1)
    mb = int(round(n))
    if mb and mb < 64:
        raise ValueError("memoria minima: 64m")
    return mb


def cpus_validas(valor) -> float:
    """0.5, 1, 2... (nucleos). 0 = sem limite."""
    try:
        n = round(float(str(valor).replace(",", ".")), 2)
    except ValueError as exc:
        raise ValueError(f"cpus invalido: '{valor}' (use, por exemplo, 0.5 ou 2)") from exc
    if n and not 0.1 <= n <= 256:
        raise ValueError("cpus deve ficar entre 0.1 e 256")
    return n


class ManifestoInvalido(ValueError):
    pass


@dataclass
class Configuracao:
    nome: str
    rotulo: str
    tipo: str = "texto"
    padrao: str = ""
    ajuda: str = ""
    # True: a solucao nao inicia enquanto o admin nao preencher (o portal avisa qual campo).
    obrigatoria: bool = False
    # True: o valor entra na CONSTRUCAO da imagem (docker build --build-arg), alem do ambiente.
    # Para sites que gravam as variaveis no codigo ao compilar (Vite, React, Next NEXT_PUBLIC_...).
    construcao: bool = False

    @property
    def na_construcao(self) -> bool:
        return self.construcao or bool(VARS_PUBLICAS_BUILD.match(self.nome))


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
    # Caminhos que abrem SEM login no portal (ex.: links publicos enviados por e-mail).
    caminhos_publicos: list[str] = field(default_factory=list)
    # Dockerfile guardado com o manifesto (gerado pelo assistente), usado no lugar do do repositorio.
    dockerfile_portal: Path | None = None
    # [execucao] externo = "host:porta": servico que o servidor sobe por fora do portal (ex.: o
    # Supabase em deploy/supabase). O portal so publica a porta, controla o acesso e mostra o
    # estado; nao constroi, nao inicia nem para.
    externo: str = ""
    # requer = "supabase": a solucao so entra no catalogo quando alguma outra usa o Supabase do
    # servidor ([supabase] no manifesto) ou com PORTAL_SUPABASE=sim no .env (ex.: o Studio).
    requer: str = ""
    # [build] variaveis_na_construcao = true: site estatico (Vite/React...). Nada le variavel quando
    # o site roda, entao TODAS as variaveis (fixas e configuracoes) vao para a construcao.
    variaveis_na_construcao: bool = False
    # True: manifesto criado pelo assistente neste servidor (dados/solucoes-locais), fora do Git.
    local: bool = False
    # Banco no PostgreSQL do servidor: {configuracao: modelo com {db_host}, {db_senha}...}.
    # O portal cria banco + usuario e preenche essas configuracoes (so as vazias, uma vez).
    banco: dict[str, str] | None = None
    # [backup]: incluir no backup diario (padrao sim) e banco externo (configuracao com a URL
    # postgresql://, ex.: Supabase) a copiar com pg_dump, opcionalmente so alguns esquemas.
    backup_incluir: bool = True
    backup_externo: str = ""
    backup_esquemas: list[str] = field(default_factory=list)
    # [recursos]: limites sugeridos por quem fez a solucao (None = padrao do servidor).
    recursos_memoria_mb: int | None = None
    recursos_cpus: float | None = None
    # [supabase]: o portal cria um Supabase so para esta solucao e preenche as variaveis indicadas
    # (nome da variavel que recebe cada valor; vazio = nao usa): url (endereco publico, o mesmo do
    # sistema), chave_anon (chave publica), url_interna e chave_servico (so para codigo que roda no
    # servidor; a chave de servico nunca vai para o site).
    supabase: dict[str, str] | None = None
    # [supabase] realtime = true: a pilha ganha o servico de tempo real (supabase-js .channel())
    supabase_realtime: bool = False

    @property
    def container(self) -> str:
        return f"log20-{self.id}"

    @property
    def nomes_supabase(self) -> set[str]:
        """Variaveis preenchidas pelo portal com o Supabase da solucao (nao precisam de valor)."""
        return {n for n in (self.supabase or {}).values() if n}

    def faltando(self, valores: dict[str, str]) -> list[str]:
        """Rotulos das configuracoes obrigatorias ainda sem valor."""
        return [c.rotulo for c in self.configuracoes
                if c.obrigatoria and not valores.get(c.nome) and c.nome not in self.nomes_supabase]

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

    execucao = _exigir(dados, "execucao", caminho)
    servico_externo = str(execucao.get("externo", "")).strip()
    if servico_externo and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}:\d{1,5}", servico_externo):
        raise ManifestoInvalido(f"{caminho}: externo deve ser 'container:porta' (ex.: log20-supabase-gw:8000)")
    build = dados.get("build", {}) if servico_externo else _exigir(dados, "build", caminho)

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
    elif servico_externo:
        contexto = None
    else:
        contexto = (base / _exigir(build, "contexto", caminho)).resolve()
    dockerfile_portal = None
    if build.get("dockerfile_portal"):
        dockerfile_portal = (base / str(build["dockerfile_portal"])).resolve()
        if base.resolve() not in dockerfile_portal.parents or not dockerfile_portal.is_file():
            raise ManifestoInvalido(f"{caminho}: dockerfile_portal deve ser um arquivo dentro da pasta do manifesto")
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
                f"{caminho}: volume '{chave}' deve ser um caminho absoluto do container (campos soltos depois de [execucao.volumes] entram nessa tabela. Mova-os para o topo)"
            )
    publicos = execucao.get("caminhos_publicos", [])
    if not isinstance(publicos, list) or not all(isinstance(c, str) and PUBLICO_RE.match(c) for c in publicos):
        raise ManifestoInvalido(
            f"{caminho}: caminhos_publicos deve ser uma lista como [\"/links/*\"]: caminhos que comecam "
            "com /, sem espacos, com * apenas no fim"
        )
    if any(c in ("/", "/*") for c in publicos):
        raise ManifestoInvalido(f"{caminho}: caminhos_publicos nao pode liberar o sistema inteiro ('/' ou '/*')")
    for chave, valor in execucao.get("ambiente", {}).items():
        if isinstance(valor, (dict, list)):
            raise ManifestoInvalido(f"{caminho}: variavel de ambiente '{chave}' deve ser texto")

    banco = None
    if "banco" in dados:
        sec = dados["banco"]
        if not isinstance(sec, dict) or sec.get("tipo", "postgres") != "postgres":
            raise ManifestoInvalido(f"{caminho}: [banco] aceita apenas tipo = \"postgres\"")
        mapa = sec.get("configuracoes", {})
        if not isinstance(mapa, dict) or not mapa:
            raise ManifestoInvalido(f"{caminho}: [banco.configuracoes] deve dizer qual configuracao recebe cada dado")
        nomes_config = {str(i.get("nome")) for i in dados.get("configuracoes", [])}
        for nome, modelo in mapa.items():
            if nome not in nomes_config:
                raise ManifestoInvalido(f"{caminho}: [banco.configuracoes] cita '{nome}', que nao esta em [[configuracoes]]")
            sobra = re.sub(r"\{db_(host|porta|nome|usuario|senha|url)\}", "", str(modelo))
            if "{" in sobra or "}" in sobra:
                raise ManifestoInvalido(
                    f"{caminho}: modelo invalido em [banco.configuracoes] {nome}; use {{db_host}}, {{db_porta}}, "
                    "{db_nome}, {db_usuario}, {db_senha} ou {db_url}"
                )
        banco = {str(k): str(v) for k, v in mapa.items()}

    sec_rec = dados.get("recursos", {})
    if not isinstance(sec_rec, dict):
        raise ManifestoInvalido(f"{caminho}: [recursos] deve ser uma tabela")
    try:
        rec_mem = memoria_mb(sec_rec["memoria"]) if "memoria" in sec_rec else None
        rec_cpus = cpus_validas(sec_rec["cpus"]) if "cpus" in sec_rec else None
    except ValueError as exc:
        raise ManifestoInvalido(f"{caminho}: [recursos] {exc}") from exc

    sec_backup = dados.get("backup", {})
    if not isinstance(sec_backup, dict):
        raise ManifestoInvalido(f"{caminho}: [backup] deve ser uma tabela")
    externo = str(sec_backup.get("banco_externo", "")).strip()
    if externo and externo not in {str(i.get("nome")) for i in dados.get("configuracoes", [])}:
        raise ManifestoInvalido(f"{caminho}: [backup] banco_externo cita '{externo}', que nao esta em [[configuracoes]]")
    esquemas = sec_backup.get("esquemas", [])
    if not isinstance(esquemas, list) or not all(isinstance(e, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", e) for e in esquemas):
        raise ManifestoInvalido(f"{caminho}: [backup] esquemas deve ser uma lista como [\"public\"]")

    supabase = None
    supabase_realtime = False
    if "supabase" in dados:
        sec_sb = dados["supabase"]
        if not isinstance(sec_sb, dict):
            raise ManifestoInvalido(f"{caminho}: [supabase] deve ser uma tabela")
        if servico_externo:
            raise ManifestoInvalido(f"{caminho}: [supabase] nao combina com externo")
        supabase_realtime = sec_sb.get("realtime", False)
        if not isinstance(supabase_realtime, bool):
            raise ManifestoInvalido(f"{caminho}: [supabase] realtime deve ser true ou false")
        supabase = {"url": str(sec_sb.get("url", "VITE_SUPABASE_URL")).strip(),
                    "chave_anon": str(sec_sb.get("chave_anon", "VITE_SUPABASE_ANON_KEY")).strip(),
                    "url_interna": str(sec_sb.get("url_interna", "")).strip(),
                    "chave_servico": str(sec_sb.get("chave_servico", "")).strip()}
        for campo, nome in supabase.items():
            if nome and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", nome):
                raise ManifestoInvalido(f"{caminho}: [supabase] {campo} deve ser o nome de uma variavel (ex.: VITE_SUPABASE_URL)")
        for campo in ("url_interna", "chave_servico"):
            if supabase[campo] and VARS_PUBLICAS_BUILD.match(supabase[campo]):
                raise ManifestoInvalido(
                    f"{caminho}: [supabase] {campo} = {supabase[campo]}: variaveis com esse prefixo vao para o site e qualquer pessoa leria o valor, use um nome sem prefixo de frontend")
        if bool(build.get("variaveis_na_construcao", False)) and supabase["chave_servico"]:
            raise ManifestoInvalido(f"{caminho}: [supabase] chave_servico nao pode ser usada num site estatico (variaveis_na_construcao), ela ficaria visível para o navegador")

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
                obrigatoria=bool(item.get("obrigatoria", False)),
                construcao=bool(item.get("construcao", False)),
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
        porta_container=int(execucao.get("porta_container") or servico_externo.rsplit(":", 1)[-1]) if servico_externo
        else int(_exigir(execucao, "porta_container", caminho)),
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
        externo=servico_externo,
        requer=str(dados.get("requer", "")).strip().lower(),
        ref=ref if repositorio else "",
        subpasta=subpasta,
        caminhos_publicos=list(publicos),
        dockerfile_portal=dockerfile_portal,
        variaveis_na_construcao=bool(build.get("variaveis_na_construcao", False)),
        banco=banco,
        backup_incluir=bool(sec_backup.get("incluir", True)),
        backup_externo=externo,
        backup_esquemas=list(esquemas),
        recursos_memoria_mb=rec_mem,
        recursos_cpus=rec_cpus,
        supabase=supabase,
        supabase_realtime=supabase_realtime,
    )


def _ligado(recurso: str, solucoes: list[Solucao]) -> bool:
    """O recurso do servidor esta em uso: "supabase" quando alguma solucao tem [supabase]; ou
    PORTAL_<RECURSO>=sim no ambiente (forca o recurso ligado)."""
    if recurso == "supabase" and any(s.supabase for s in solucoes):
        return True
    valor = os.environ.get(f"PORTAL_{recurso.upper()}", "").strip().lower()
    return valor in {"1", "sim", "true", "yes", "on"}


def carregar_catalogo(pasta: Path, pasta_local: Path | None = None) -> tuple[dict[str, Solucao], list[str]]:
    """Le todas as solucoes. Manifestos com erro sao reportados, nao derrubam o portal.

    `pasta` = solucoes/ do repositorio do portal; `pasta_local` = as criadas pelo assistente neste
    servidor (dados/solucoes-locais). Com o mesmo id nas duas, vale a do repositorio.
    """
    solucoes: dict[str, Solucao] = {}
    erros: list[str] = []
    portas: dict[int, str] = {}
    caminhos = [(c, False) for c in sorted(pasta.glob("*/solucao.toml"))]
    if pasta_local and pasta_local.is_dir():
        caminhos += [(c, True) for c in sorted(pasta_local.glob("*/solucao.toml"))]
    lidas: list[tuple[Path, Solucao]] = []
    for caminho, local in caminhos:
        try:
            sol = carregar_manifesto(caminho)
        except (ManifestoInvalido, KeyError, ValueError) as exc:
            erros.append(str(exc))
            continue
        sol.local = local
        lidas.append((caminho, sol))
    todas = [s for _, s in lidas]
    for caminho, sol in lidas:
        local = sol.local
        if sol.requer and not _ligado(sol.requer, todas):
            continue  # recurso do servidor sem uso (ex.: Studio sem solucao com Supabase): nao aparece
        if sol.id in solucoes:
            if local:
                erros.append(f"{caminho}: a solucao '{sol.id}' ja esta no repositorio do portal. A copia criada neste servidor foi ignorada e pode ser removida")
            else:
                erros.append(f"{caminho}: id '{sol.id}' repetido")
            continue
        if sol.porta_host in portas:
            erros.append(f"{caminho}: porta {sol.porta_host} ja usada por '{portas[sol.porta_host]}'"
                         + ("; troque a porta de uma das duas (a deste servidor, em solucao.toml)" if local else ""))
            continue
        portas[sol.porta_host] = sol.id
        solucoes[sol.id] = sol
    return solucoes, erros
