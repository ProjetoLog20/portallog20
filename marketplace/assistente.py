"""Assistente "Nova solucao": analisa um repositorio e grava o manifesto (e o Dockerfile sugerido).

Os arquivos vao para dados/solucoes-locais/<id>/ (fora do Git do portal): a solucao entra no
catalogo deste servidor na hora. Para leva-la a outros servidores, os mesmos arquivos podem ser
baixados e colocados em solucoes/<id>/ no repositorio do portal.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from . import fontes
from .catalog import ID_RE, REF_RE, REPO_RE, ManifestoInvalido, Solucao, carregar_manifesto
from .config import Config
from .detector import analisar

MAX_DOCKERFILE = 100_000
TIPOS = {"texto", "segredo", "booleano"}
VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
VOLUME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

# O repositorio chega inteiro ao build; isto fica fora da imagem (historico do Git, senhas locais,
# dependencias instaladas na maquina de quem desenvolveu). O BuildKit le <Dockerfile>.dockerignore.
DOCKERIGNORE = """.git
.gitignore
.env
.env.*
!.env.example
**/node_modules
**/__pycache__
**/*.pyc
.venv
venv
*.sqlite3
*.db
.DS_Store
.idea
.vscode
"""


class ErroAssistente(ValueError):
    pass


def _nome_repo(repositorio: str) -> str:
    return Path(urlparse(repositorio).path).name.removesuffix(".git")


def validar_origem(repositorio: str, ref: str, subpasta: str) -> tuple[str, str, str]:
    repositorio, ref, subpasta = repositorio.strip(), (ref or "main").strip(), (subpasta or "").strip().strip("/")
    if not REPO_RE.match(repositorio):
        raise ErroAssistente("Informe o endereço HTTPS do repositório, ex.: https://github.com/empresa/projeto.git")
    if not REF_RE.match(ref) or ".." in ref:
        raise ErroAssistente("Branch ou tag inválido.")
    if subpasta and (subpasta.startswith("/") or ".." in Path(subpasta).parts or not re.fullmatch(r"[\w./-]+", subpasta)):
        raise ErroAssistente("A subpasta deve ser um caminho dentro do repositório (ex.: backend).")
    return repositorio, ref, subpasta


# Portas: as soluções do repositório do portal (Git) usam o começo da faixa, escolhidas à mão
# (8101-8115); as criadas pelo assistente em cada servidor começam em 8116. Assim uma solução
# nova no Git não cai na porta de uma que só existe num servidor.
INICIO_LOCAIS = 8116


def porta_livre(cfg: Config, solucoes: dict[str, Solucao]) -> int:
    usadas = {s.porta_host for s in solucoes.values()}
    inicio, fim = cfg.faixa_portas or (8101, 8199)
    if inicio < INICIO_LOCAIS <= fim:
        inicio = INICIO_LOCAIS
    for p in range(inicio, fim + 1):
        if p not in usadas:
            return p
    raise ErroAssistente(f"Não há porta livre na faixa {inicio}-{fim}. Amplie PORTAL_FAIXA_PORTAS e o docker-compose.")


def id_livre(base: str, solucoes: dict[str, Solucao], cfg: Config) -> str:
    candidato, n = base, 2
    while candidato in solucoes or (cfg.pasta_solucoes_locais / candidato).exists():
        candidato = f"{base[:37]}-{n}"
        n += 1
    return candidato


def analisar_repositorio(cfg: Config, solucoes: dict[str, Solucao], repositorio: str, ref: str, subpasta: str) -> dict:
    repositorio, ref, subpasta = validar_origem(repositorio, ref, subpasta)
    log: list[str] = []
    try:
        pasta, versao = fontes.atualizar(repositorio, ref, cfg.pasta_dados / "fontes", log.append,
                                         token=cfg.git_token, token_host=cfg.git_host)
    except Exception as exc:  # noqa: BLE001 - vira mensagem para quem esta na tela
        texto = str(exc)
        dica = ""
        if re.search(r"(?i)authentication|could not read username|403|not found|repository .* not found", texto):
            dica = (" Se o repositório for privado, ele precisa estar no token de leitura deste servidor "
                    "(PORTAL_GIT_TOKEN). Confira também o endereço e o branch.")
        raise ErroAssistente(f"Não consegui baixar o repositório: {texto[-300:]}.{dica}") from exc
    alvo = (pasta / subpasta).resolve() if subpasta else pasta
    if alvo != pasta.resolve() and pasta.resolve() not in alvo.parents or not alvo.is_dir():
        raise ErroAssistente(f"A subpasta '{subpasta}' não existe no repositório.")
    nome = _nome_repo(repositorio) if not subpasta else Path(subpasta).name
    analise = analisar(alvo, nome).como_dict()
    analise["id_sugerido"] = id_livre(analise["id_sugerido"], solucoes, cfg)
    analise["porta_host"] = porta_livre(cfg, solucoes)
    analise["origem"] = {"repositorio": repositorio, "ref": ref, "subpasta": subpasta,
                         "commit": versao.commit, "resumo_commit": versao.resumo}
    return analise


# ---------------------------------------------------------------- gerar o manifesto

def _s(valor) -> str:
    """String TOML (as regras de escape das strings basicas do TOML sao as do JSON)."""
    return json.dumps(str(valor), ensure_ascii=False)


def _chave(nome: str) -> str:
    return nome if re.fullmatch(r"[A-Za-z0-9_-]+", nome) else _s(nome)


def gerar_manifesto(d: dict, com_dockerfile: bool) -> str:
    """Monta o solucao.toml a partir dos campos da tela (ja validados)."""
    data = time.strftime("%d/%m/%Y")
    descricao = d.get("descricao") or (
        f"{d['resumo']}\n\nCriada pelo assistente do portal em {data} a partir de {d['repositorio']} "
        f"(branch {d['ref']}). Tecnologia identificada: {d.get('tecnologia', 'não informada')}."
    )
    linhas = [
        "# Criado pelo assistente \"Nova solucao\" do Portal de Solucoes LOG20.",
        "# Para usar em outros servidores, coloque esta pasta em solucoes/<id>/ no repositorio do portal.",
        "",
        f"id = {_s(d['id'])}",
        f"nome = {_s(d['nome'])}",
        f"resumo = {_s(d['resumo'])}",
        f"categoria = {_s(d['categoria'])}",
        'versao = "1.0.0"',
        f"responsavel = {_s(d.get('responsavel') or 'LOG20')}",
        f"tags = [{', '.join(_s(t) for t in d.get('tags', []))}]",
        *([f"tecnologia = {_s(d['tecnologia'])}"] if d.get("tecnologia") else []),
        f"segredos = [{', '.join(_s(s) for s in d['segredos'])}]",
        f'descricao = """\n{descricao.replace(chr(92), chr(92) * 2).replace(chr(34) * 3, chr(39) * 3)}\n"""',
        "",
        "[build]",
        f"repositorio = {_s(d['repositorio'])}",
        f"ref = {_s(d['ref'])}",
    ]
    if d.get("subpasta"):
        linhas.append(f"subpasta = {_s(d['subpasta'])}")
    if com_dockerfile:
        linhas.append('# Dockerfile sugerido pelo assistente, guardado nesta pasta (o repositorio nao precisa ter um).')
        linhas.append('dockerfile_portal = "Dockerfile"')
    if d.get("variaveis_na_construcao"):
        linhas.append("# Site estatico: as variaveis entram no site na construcao (Atualizar depois de mudar).")
        linhas.append("variaveis_na_construcao = true")
    linhas += [
        "",
        "[execucao]",
        f"porta_container = {int(d['porta_container'])}",
        f"porta_host = {int(d['porta_host'])}",
        f"caminho_saude = {_s(d['caminho_saude'])}",
        f"caminho_abrir = {_s(d.get('caminho_abrir') or '/')}",
        "",
        "[execucao.ambiente]",
        *[f"{_chave(k)} = {_s(v)}" for k, v in d["ambiente"].items()],
        "",
        "[execucao.volumes]",
        *[f"{_chave(k)} = {_s(v)}" for k, v in d["volumes"].items()],
    ]
    if d.get("banco"):
        linhas += ["", "# Banco no PostgreSQL do servidor, criado pelo portal na primeira instalacao.", "[banco]",
                   'tipo = "postgres"', "", "[banco.configuracoes]",
                   *[f"{_chave(k)} = {_s(v)}" for k, v in d["banco"].items()]]
    for c in d["configuracoes"]:
        linhas += ["", "[[configuracoes]]", f"nome = {_s(c['nome'])}", f"rotulo = {_s(c.get('rotulo') or c['nome'])}",
                   f"tipo = {_s(c.get('tipo') or 'texto')}"]
        if c.get("padrao"):
            linhas.append(f"padrao = {_s(c['padrao'])}")
        if c.get("ajuda"):
            linhas.append(f"ajuda = {_s(c['ajuda'])}")
        if c.get("obrigatoria"):
            linhas.append("obrigatoria = true")
        if c.get("construcao"):
            linhas.append("construcao = true")
    return "\n".join(linhas) + "\n"


def _limpar(d: dict, cfg: Config, solucoes: dict[str, Solucao], editando: str | None = None) -> dict:
    """Confere e normaliza o que veio da tela. Erros viram ErroAssistente com mensagem clara."""
    repositorio, ref, subpasta = validar_origem(d.get("repositorio", ""), d.get("ref", ""), d.get("subpasta", ""))
    sid = str(d.get("id", "")).strip()
    if not ID_RE.match(sid):
        raise ErroAssistente("Identificador: 2 a 41 caracteres, letras minúsculas, números e hífens (ex.: controle-frota).")
    if editando:
        if sid != editando:
            raise ErroAssistente("O identificador não muda depois de criado.")
    elif sid in solucoes or (cfg.pasta_solucoes_locais / sid).exists():
        raise ErroAssistente(f"Já existe uma solução com o identificador '{sid}'.")
    nome = " ".join(str(d.get("nome", "")).split())
    if not nome:
        raise ErroAssistente("Informe o nome da solução.")
    try:
        porta_c, porta_h = int(d.get("porta_container")), int(d.get("porta_host"))
    except (TypeError, ValueError) as exc:
        raise ErroAssistente("As portas devem ser números.") from exc
    if not 1 <= porta_c <= 65535:
        raise ErroAssistente("Porta da aplicação inválida.")
    inicio, fim = cfg.faixa_portas or (1024, 65535)
    if not inicio <= porta_h <= fim:
        raise ErroAssistente(f"A porta pública deve ficar na faixa {inicio}-{fim}.")
    if any(s.porta_host == porta_h and s.id != editando for s in solucoes.values()):
        raise ErroAssistente(f"A porta {porta_h} já é usada por outra solução.")
    saude = str(d.get("caminho_saude") or "/").strip()
    if not saude.startswith("/") or re.search(r"\s", saude):
        raise ErroAssistente("A rota de saúde deve começar com / (ex.: /health).")
    ambiente = {str(k).strip(): str(v) for k, v in (d.get("ambiente") or {}).items() if str(k).strip()}
    volumes = {str(k).strip(): str(v).strip() for k, v in (d.get("volumes") or {}).items() if str(k).strip()}
    for k in ambiente:
        if not VAR_RE.match(k):
            raise ErroAssistente(f"Nome de variável inválido: {k}")
    for k, v in volumes.items():
        if not VOLUME_RE.match(k) or not v.startswith("/"):
            raise ErroAssistente(f"Volume '{k}': apelido em minúsculas e caminho absoluto no container (ex.: /app/data).")
    segredos = [s for s in (str(x).strip() for x in d.get("segredos") or []) if s]
    configuracoes, vistos = [], set()
    for c in d.get("configuracoes") or []:
        nome_c, tipo = str(c.get("nome", "")).strip(), c.get("tipo") or "texto"
        if not VAR_RE.match(nome_c) or tipo not in TIPOS:
            raise ErroAssistente(f"Configuração inválida: {c.get('nome')}")
        if nome_c in vistos:
            continue
        vistos.add(nome_c)
        padrao = str(c.get("padrao") or "")
        if tipo == "booleano":
            padrao = "true" if padrao.lower() in ("true", "1", "sim", "on") else "false" if padrao else ""
        elif tipo == "segredo":
            padrao = ""  # segredo nao tem valor padrao no manifesto (iria para o Git)
        configuracoes.append({"nome": nome_c, "rotulo": " ".join(str(c.get("rotulo") or nome_c).split())[:120],
                              "tipo": tipo, "padrao": padrao[:500], "ajuda": " ".join(str(c.get("ajuda") or "").split())[:500],
                              "obrigatoria": bool(c.get("obrigatoria")), "construcao": bool(c.get("construcao"))})
    for nome_c in list(ambiente):
        if nome_c in vistos:
            ambiente.pop(nome_c)  # a configuracao editavel vale no lugar do valor fixo
    nomes_conf = {c["nome"] for c in configuracoes}
    for s in segredos:
        if not VAR_RE.match(s):
            raise ErroAssistente(f"Nome de segredo inválido: {s}")
    banco = None
    if d.get("usar_banco") and d.get("banco"):
        banco = {k: v for k, v in d["banco"].items()}
        for k in banco:
            if k not in nomes_conf:
                configuracoes.append({"nome": k, "rotulo": f"Banco: {k}", "tipo": "segredo" if "SENHA" in k or "PASSWORD" in k
                                      or k.endswith("URL") else "texto", "ajuda": "Preenchido pelo portal com o banco do servidor."})
    arquivos = {}
    for arq, conteudo in (d.get("arquivos") or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,60}", str(arq)) or arq in {"solucao.toml", "Dockerfile", "Dockerfile.dockerignore"}:
            raise ErroAssistente(f"Nome de arquivo auxiliar inválido: {arq}")
        if len(str(conteudo)) > MAX_DOCKERFILE:
            raise ErroAssistente(f"Arquivo auxiliar grande demais: {arq}")
        arquivos[str(arq)] = str(conteudo)
    dockerfile = d.get("dockerfile")
    if dockerfile is not None:
        dockerfile = str(dockerfile).replace("\r\n", "\n")
        if not dockerfile.strip():
            dockerfile = None
        elif len(dockerfile) > MAX_DOCKERFILE:
            raise ErroAssistente("Dockerfile grande demais.")
    return {"id": sid, "nome": nome, "resumo": " ".join(str(d.get("resumo", "")).split())[:200] or nome,
            "categoria": " ".join(str(d.get("categoria") or "Geral").split())[:60],
            "responsavel": " ".join(str(d.get("responsavel") or "LOG20").split())[:80],
            "tecnologia": str(d.get("tecnologia") or "")[:80], "repositorio": repositorio, "ref": ref, "subpasta": subpasta,
            "porta_container": porta_c, "porta_host": porta_h, "caminho_saude": saude, "ambiente": ambiente,
            "volumes": volumes, "segredos": segredos, "configuracoes": configuracoes, "banco": banco,
            "dockerfile": dockerfile, "arquivos": arquivos if dockerfile else {},
            "variaveis_na_construcao": bool(d.get("variaveis_na_construcao"))}


FIXOS = {"solucao.toml", "Dockerfile", "Dockerfile.dockerignore"}


def _validar_gerado(d: dict, toml: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        teste = Path(tmp) / d["id"]
        teste.mkdir()
        (teste / "solucao.toml").write_text(toml, encoding="utf-8")
        if d["dockerfile"]:
            (teste / "Dockerfile").write_text(d["dockerfile"], encoding="utf-8")
        try:
            carregar_manifesto(teste / "solucao.toml")
        except ManifestoInvalido as exc:
            raise ErroAssistente(f"O manifesto gerado não passou na validação: {str(exc).split(': ', 1)[-1]}") from exc


def _gravar(destino: Path, d: dict, toml: str) -> None:
    """Escreve a pasta da solucao; arquivos auxiliares que nao fazem mais parte sao apagados."""
    destino.mkdir(parents=True, exist_ok=True)
    manter = {"solucao.toml"}
    if d["dockerfile"]:
        (destino / "Dockerfile").write_text(d["dockerfile"], encoding="utf-8")
        (destino / "Dockerfile.dockerignore").write_text(DOCKERIGNORE, encoding="utf-8")
        for nome, conteudo in d["arquivos"].items():
            (destino / nome).write_text(conteudo, encoding="utf-8")
        manter |= {"Dockerfile", "Dockerfile.dockerignore", *d["arquivos"]}
    for velho in destino.iterdir():
        if velho.is_file() and velho.name not in manter:
            velho.unlink()
    # O manifesto por ultimo: se algo falhar antes, o catalogo continua com o anterior.
    tmp = destino / "solucao.toml.novo"
    tmp.write_text(toml, encoding="utf-8")
    tmp.replace(destino / "solucao.toml")


def criar(cfg: Config, solucoes: dict[str, Solucao], dados: dict) -> dict:
    """Grava dados/solucoes-locais/<id>/ (manifesto + Dockerfile), validado pelo mesmo leitor do catalogo."""
    d = _limpar(dados, cfg, solucoes)
    toml = gerar_manifesto(d, com_dockerfile=bool(d["dockerfile"]))
    _validar_gerado(d, toml)
    _gravar(cfg.pasta_solucoes_locais / d["id"], d, toml)
    return {"id": d["id"], "manifesto": toml, "dockerfile": d["dockerfile"], "arquivos": sorted(d["arquivos"])}


def editar(cfg: Config, solucoes: dict[str, Solucao], sid: str, dados: dict) -> dict:
    """Regrava uma solucao criada pelo assistente. O id nao muda; vale no proximo Reiniciar/Atualizar."""
    pasta = cfg.pasta_solucoes_locais / sid
    if not ID_RE.match(sid) or not (pasta / "solucao.toml").is_file():
        raise ErroAssistente("Só dá para editar aqui as soluções criadas pelo assistente neste servidor.")
    d = _limpar({**dados, "id": sid}, cfg, solucoes, editando=sid)
    toml = gerar_manifesto(d, com_dockerfile=bool(d["dockerfile"]))
    _validar_gerado(d, toml)
    _gravar(pasta, d, toml)
    return {"id": sid, "manifesto": toml, "dockerfile": d["dockerfile"], "arquivos": sorted(d["arquivos"])}


def para_edicao(cfg: Config, sol: Solucao) -> dict:
    """Os valores atuais da solucao no formato que a tela do assistente usa."""
    pasta = cfg.pasta_solucoes_locais / sol.id
    if not sol.local or not (pasta / "solucao.toml").is_file():
        raise ErroAssistente("Só dá para editar aqui as soluções criadas pelo assistente neste servidor.")
    bruto = tomllib.loads((pasta / "solucao.toml").read_text(encoding="utf-8"))
    df = pasta / "Dockerfile"
    auxiliares = {p.name: p.read_text(encoding="utf-8") for p in sorted(pasta.iterdir())
                  if p.is_file() and p.name not in FIXOS and not p.name.endswith(".novo")}
    return {
        "id": sol.id, "nome": sol.nome, "resumo": sol.resumo, "categoria": sol.categoria, "responsavel": sol.responsavel,
        "tecnologia": str(bruto.get("tecnologia", "")),
        "repositorio": sol.repositorio, "ref": sol.ref, "subpasta": "" if sol.subpasta == "." else sol.subpasta,
        "porta_container": sol.porta_container, "porta_host": sol.porta_host, "caminho_saude": sol.caminho_saude,
        "ambiente": dict(sol.ambiente), "volumes": dict(sol.volumes), "segredos": list(sol.segredos),
        "configuracoes": [{"nome": c.nome, "rotulo": c.rotulo, "tipo": c.tipo, "padrao": c.padrao, "ajuda": c.ajuda,
                           "obrigatoria": c.obrigatoria, "construcao": c.na_construcao} for c in sol.configuracoes],
        "banco": dict(sol.banco) if sol.banco else None, "usar_banco": bool(sol.banco),
        "variaveis_na_construcao": sol.variaveis_na_construcao,
        "tem_dockerfile": not df.is_file(), "dockerfile": df.read_text(encoding="utf-8") if df.is_file() else None,
        "arquivos": auxiliares,
    }


def zip_arquivos(cfg: Config, sid: str) -> bytes:
    """A pasta pronta para o Git: solucoes/<id>/... dentro de um .zip."""
    import io
    import zipfile

    conteudo = arquivos(cfg, sid)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, texto in conteudo.items():
            if nome.endswith(".novo"):
                continue
            info = zipfile.ZipInfo(f"solucoes/{sid}/{nome}", date_time=time.localtime()[:6])
            info.external_attr = (0o755 if nome == "portal-saude" else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, texto)
    return buffer.getvalue()


def arquivos(cfg: Config, sid: str) -> dict:
    pasta = cfg.pasta_solucoes_locais / sid
    if not ID_RE.match(sid) or not (pasta / "solucao.toml").is_file():
        raise ErroAssistente("Esta solução não foi criada pelo assistente neste servidor.")
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(pasta.iterdir()) if p.is_file()}


def remover(cfg: Config, sid: str) -> None:
    pasta = cfg.pasta_solucoes_locais / sid
    if not ID_RE.match(sid) or not pasta.is_dir():
        raise ErroAssistente("Esta solução não foi criada pelo assistente neste servidor.")
    shutil.rmtree(pasta)
