"""API e interface web do Portal de Solucoes LOG20."""

from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config as config_mod
from . import assistente, docker_cli, proxy
from .auth import Autenticacao, ErroAuth, ErroLote, Usuario
from .catalog import Solucao, carregar_catalogo
from .manager import ACOES, Armazenamento, Gerenciador

CFG = config_mod.carregar()
PASTA_WEB = Path(__file__).resolve().parent / "web"
COOKIE = "portal_sessao"

app = FastAPI(
    title="Portal de Solucoes LOG20",
    version="0.3.0",
    docs_url="/docs" if CFG.docs_api else None,
    redoc_url=None,
    openapi_url="/openapi.json" if CFG.docs_api else None,
)
app.mount("/static", StaticFiles(directory=PASTA_WEB), name="static")

AUTH = Autenticacao(CFG.pasta_dados / "portal.db", sessao_horas=CFG.sessao_horas)
_estado: dict = {}


def _carregar() -> None:
    solucoes, erros = carregar_catalogo(CFG.pasta_solucoes, CFG.pasta_solucoes_locais)
    if CFG.faixa_portas:
        inicio, fim = CFG.faixa_portas
        for sid, sol in list(solucoes.items()):
            if not inicio <= sol.porta_host <= fim:
                erros.append(f"{sol.manifesto}: porta_host {sol.porta_host} fora da faixa publicada pelo Caddy "
                             f"({inicio}-{fim}); a solucao foi ignorada")
                del solucoes[sid]
    _estado["erros"] = erros
    _estado["gerenciador"] = Gerenciador(solucoes, Armazenamento(CFG.pasta_dados), CFG)
    _estado["proxy"] = _atualizar_proxy(list(solucoes.values()))


def _atualizar_proxy(solucoes) -> dict | None:
    """Regera a configuracao do Caddy a partir do catalogo (so quando o portal gerencia o Caddy)."""
    if not CFG.caddy_container:
        return None
    try:
        r = proxy.aplicar(proxy.gerar(solucoes, CFG.portal_upstream, COOKIE), CFG.caddy_pasta, CFG.caddy_container)
    except Exception as exc:  # noqa: BLE001 - o portal continua no ar mesmo se o Caddy falhar
        r = proxy.Resultado(False, f"Falha ao atualizar o Caddy: {exc}", erro=True)
    print(f"[caddy] {r.mensagem}", flush=True)
    return {"aplicado": r.aplicado, "mensagem": r.mensagem, "erro": r.erro}


_carregar()


def ger() -> Gerenciador:
    return _estado["gerenciador"]


# ---------------- protecoes gerais ----------------

@app.middleware("http")
async def protecoes(request: Request, call_next):
    caminho = request.url.path
    # CSRF: alteracoes so podem vir de paginas do proprio portal.
    if request.method not in ("GET", "HEAD", "OPTIONS") and caminho.startswith("/api/"):
        origem = request.headers.get("origin")
        if origem:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host")
            if urlparse(origem).netloc != host:
                return JSONResponse({"detail": "Origem nao permitida."}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if caminho.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    if not caminho.startswith(("/docs", "/openapi")):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
    return resp


@app.exception_handler(ErroAuth)
async def _erro_auth(_: Request, exc: ErroAuth):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(ErroLote)
async def _erro_lote(_: Request, exc: ErroLote):
    return JSONResponse({"detail": str(exc), "problemas": exc.problemas}, status_code=400)


# ---------------- sessao e permissoes ----------------

def usuario_atual(request: Request) -> Usuario | None:
    return AUTH.sessao(request.cookies.get(COOKIE))


def exigir_login(request: Request) -> Usuario:
    usuario = usuario_atual(request)
    if usuario is None:
        raise HTTPException(401, "Faca login para continuar.")
    return usuario


def exigir_liberado(usuario: Usuario = Depends(exigir_login)) -> Usuario:
    if usuario.trocar_senha:
        raise HTTPException(403, "Troque a sua senha para continuar.")
    return usuario


def exigir_admin(usuario: Usuario = Depends(exigir_liberado)) -> Usuario:
    if not usuario.admin:
        raise HTTPException(403, "Acesso restrito a administradores.")
    return usuario


def _publico(u: Usuario) -> dict:
    return {"id": u.id, "usuario": u.usuario, "nome": u.nome, "perfil": u.perfil, "trocar_senha": u.trocar_senha}


def _definir_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        COOKIE, token,
        max_age=CFG.sessao_horas * 3600,
        httponly=True,
        samesite="lax",
        secure=CFG.cookie_seguro,
        domain=CFG.cookie_dominio or None,
        path="/",
    )


# ---------------- paginas ----------------

@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    """Pagina inicial com ?v=<data do arquivo> no CSS e no JS: depois de um `git pull`, o
    navegador baixa a versao nova em vez de usar a guardada em cache."""
    pagina = (PASTA_WEB / "index.html").read_text(encoding="utf-8")
    for nome in ("style.css", "app.js"):
        versao = int((PASTA_WEB / nome).stat().st_mtime)
        pagina = pagina.replace(f"/static/{nome}", f"/static/{nome}?v={versao}")
    return HTMLResponse(pagina, headers={"Cache-Control": "no-cache"})


# ---------------- autenticacao ----------------

@app.post("/api/auth/login")
def api_login(request: Request, response: Response, usuario: str = Body(...), senha: str = Body(...)) -> dict:
    ip = request.client.host if request.client else "?"
    u, token = AUTH.login(usuario, senha, ip)
    _definir_cookie(response, token)
    return _publico(u)


@app.post("/api/auth/logout")
def api_logout(request: Request, response: Response) -> dict:
    AUTH.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/", domain=CFG.cookie_dominio or None)
    return {"ok": True}


@app.get("/api/auth/eu")
def api_eu(usuario: Usuario = Depends(exigir_login)) -> dict:
    return _publico(usuario)


@app.post("/api/auth/senha")
def api_trocar_senha(request: Request, atual: str = Body(...), nova: str = Body(...),
                     usuario: Usuario = Depends(exigir_login)) -> dict:
    AUTH.trocar_senha(usuario.id, atual, nova, manter_token=request.cookies.get(COOKIE))
    return {"ok": True}


def _pagina_aviso(titulo: str, texto: str, status: int) -> HTMLResponse:
    """Pagina simples mostrada pelo proxy quando o acesso a uma solucao e negado."""
    portal = html.escape(CFG.url_publica or "/")
    corpo = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(titulo)}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#eef2f3;color:#12262e;
font:16px/1.55 "Segoe UI",system-ui,sans-serif}}main{{max-width:420px;margin:24px;padding:28px;background:#fff;
border:1px solid #d5dee1;border-top:5px solid #003a4d;border-radius:10px}}h1{{margin:0 0 8px;font-size:22px}}
a{{display:inline-block;margin-top:16px;padding:10px 16px;border-radius:6px;background:#003a4d;color:#fff;
text-decoration:none;font-weight:600}}</style></head><body><main><h1>{html.escape(titulo)}</h1>
<p>{html.escape(texto)}</p><a href="{portal}">Ir para o Portal de Soluções</a></main></body></html>"""
    return HTMLResponse(corpo, status_code=status, headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        "Cache-Control": "no-store",
    })


def _url_original(request: Request) -> str:
    """Endereco que a pessoa tentou abrir, informado pelo proxy (Caddy) nos cabecalhos X-Forwarded-*."""
    host = request.headers.get("x-forwarded-host")
    if not host:
        return ""
    proto = request.headers.get("x-forwarded-proto", "http")
    return f"{proto}://{host}{request.headers.get('x-forwarded-uri', '/')}"


@app.get("/api/auth/verificar", include_in_schema=False)
def api_verificar(request: Request, solucao: str):
    """Para proxy reverso (forward auth): libera a solucao so para quem tem acesso.

    200 libera; sem login, navegacao vai para o login do portal (e volta depois) e chamadas
    de API recebem 401; logado sem permissao recebe 403.
    """
    usuario = usuario_atual(request)
    navegacao = "text/html" in request.headers.get("accept", "")
    if usuario is None or usuario.trocar_senha:
        if navegacao and CFG.url_publica:
            destino = f"{CFG.url_publica}/#/login"
            original = _url_original(request)
            if original:
                destino += "?voltar=" + quote(original, safe="")
            return RedirectResponse(destino, status_code=302)
        if navegacao:
            return _pagina_aviso("Entre no portal primeiro",
                                 "Este sistema só abre para quem está conectado ao Portal de Soluções.", 401)
        return JSONResponse({"detail": "Sessao do portal expirada. Entre novamente."}, status_code=401)
    if solucao not in ger().solucoes or not AUTH.pode_acessar(usuario, solucao):
        if navegacao:
            return _pagina_aviso("Acesso não liberado",
                                 f"{usuario.nome}, este sistema não está liberado para você. "
                                 "Peça acesso ao administrador do portal.", 403)
        return JSONResponse({"detail": "Sem acesso a este sistema."}, status_code=403)
    return PlainTextResponse("ok", headers={"X-Portal-Usuario": usuario.usuario})


# ---------------- area do usuario ----------------

def _logo_url(sol: Solucao) -> str | None:
    if sol.logo is None:
        return None
    versao = int(sol.logo.stat().st_mtime) if sol.logo.exists() else 0
    return f"/api/solucoes/{sol.id}/logo?v={versao}"


@app.get("/api/solucoes/{sid}/logo", include_in_schema=False)
def api_logo(sid: str, usuario: Usuario = Depends(exigir_liberado)):
    sol = ger().solucoes.get(sid)
    if sol is None or sol.logo is None or not sol.logo.is_file() or not AUTH.pode_acessar(usuario, sid):
        raise HTTPException(404, "Logo nao encontrada")
    resp = FileResponse(sol.logo)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    # SVG enviado por terceiros nao pode executar script.
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return resp


@app.get("/api/meus-sistemas")
def api_meus_sistemas(usuario: Usuario = Depends(exigir_liberado)) -> dict:
    docker_ok = docker_cli.status().ok
    permitidas = None if usuario.admin else AUTH.acessos(usuario.id)
    itens = []
    for sol in ger().solucoes.values():
        if permitidas is not None and sol.id not in permitidas:
            continue
        estado = ger().status(sol)["estado"] if docker_ok else "indisponivel"
        itens.append({
            "id": sol.id,
            "nome": sol.nome,
            "resumo": sol.resumo,
            "categoria": sol.categoria,
            "url": ger().url(sol) + sol.caminho_abrir,
            "logo": _logo_url(sol),
            "disponivel": estado == "rodando",
        })
    return {"sistemas": itens}


# ---------------- administracao: solucoes ----------------

def _solucao(sid: str) -> Solucao:
    sol = ger().solucoes.get(sid)
    if sol is None:
        raise HTTPException(404, f"Solucao '{sid}' nao encontrada")
    return sol


def _resumo(sol: Solucao, docker_ok: bool) -> dict:
    status = ger().status(sol) if docker_ok else {"estado": "indisponivel", "detalhe": "Docker indisponivel"}
    return {
        "id": sol.id,
        "nome": sol.nome,
        "resumo": sol.resumo,
        "categoria": sol.categoria,
        "versao": sol.versao,
        "tags": sol.tags,
        "url": ger().url(sol) + sol.caminho_abrir,
        "logo": _logo_url(sol),
        "status": status,
        "local": sol.local,
    }


@app.get("/api/docker")
def api_docker(_: Usuario = Depends(exigir_admin)) -> dict:
    st = docker_cli.status()
    return {"ok": st.ok, "versao": st.versao, "erro": st.erro}


@app.get("/api/solucoes")
def api_listar(_: Usuario = Depends(exigir_admin)) -> dict:
    docker_ok = docker_cli.status().ok
    return {
        "solucoes": [_resumo(s, docker_ok) for s in ger().solucoes.values()],
        "erros_catalogo": _estado["erros"],
        "proxy": _estado.get("proxy"),
    }


@app.post("/api/catalogo/recarregar")
def api_recarregar(_: Usuario = Depends(exigir_admin)) -> dict:
    ocupadas = [sid for sid, t in ger().tarefas.items() if t.estado == "executando"]
    if ocupadas:
        raise HTTPException(409, f"Aguarde as operacoes em andamento: {', '.join(ocupadas)}")
    _carregar()
    return {"total": len(ger().solucoes), "erros_catalogo": _estado["erros"], "proxy": _estado.get("proxy")}


@app.get("/api/solucoes/{sid}")
def api_detalhe(sid: str, _: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    docker_ok = docker_cli.status().ok
    valores = ger().armazenamento.configuracoes(sol)
    return {
        **_resumo(sol, docker_ok),
        "descricao": sol.descricao,
        "responsavel": sol.responsavel,
        "imagem": sol.imagem,
        "porta": sol.porta_host,
        "url_publica": ger().armazenamento.url_publica(sol.id),
        "codigo": {"repositorio": sol.repositorio, "ref": sol.ref} if sol.repositorio else None,
        "versao_instalada": ger().armazenamento.versao_instalada(sol.id),
        # Banco criado no PostgreSQL do servidor (sem a senha, que nunca sai do portal).
        "banco": (lambda b: {"nome": b["nome"], "usuario": b["usuario"], "servidor": b.get("servidor", "")}
                  if b else None)(ger().armazenamento.banco(sol.id)) if sol.banco else None,
        "url_padrao": f"{CFG.url_base}:{sol.porta_host}",
        "local": sol.local,
        "dockerfile_portal": bool(sol.dockerfile_portal),
        "acessos_demo": [vars(a) for a in sol.acessos_demo],
        "configuracoes": [
            {
                "nome": c.nome,
                "rotulo": c.rotulo,
                "tipo": c.tipo,
                "ajuda": c.ajuda,
                "obrigatoria": c.obrigatoria,
                # Segredos nunca saem do portal: so informamos se existe valor.
                "valor": None if c.tipo == "segredo" else valores.get(c.nome, ""),
                "definido": bool(valores.get(c.nome)),
            }
            for c in sol.configuracoes
        ],
    }


@app.post("/api/solucoes/{sid}/acoes/{acao}", status_code=202)
def api_acao(sid: str, acao: str, _: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    if acao not in ACOES:
        raise HTTPException(400, f"Acao invalida. Use: {', '.join(sorted(ACOES))}")
    st = docker_cli.status()
    if not st.ok:
        raise HTTPException(503, st.erro)
    try:
        tarefa = ger().executar(sol, acao)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"acao": tarefa.acao, "estado": tarefa.estado}


@app.get("/api/solucoes/{sid}/logs")
def api_logs(sid: str, _: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    if not docker_cli.status().ok:
        tarefa = ger().tarefas.get(sid)
        return {
            "operacao": None if tarefa is None else {
                "acao": tarefa.acao, "estado": tarefa.estado, "erro": tarefa.erro, "log": "\n".join(tarefa.log)},
            "aplicacao": "",
        }
    return ger().logs(sol)


@app.put("/api/solucoes/{sid}/configuracoes")
def api_configurar(sid: str, valores: dict = Body(..., embed=True), _: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    try:
        ger().armazenamento.atualizar_configuracoes(sol, valores)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "aviso": "Reinicie a solucao para aplicar as mudancas."}


@app.delete("/api/solucoes/{sid}/configuracoes/{nome}")
def api_limpar_segredo(sid: str, nome: str, _: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    if nome not in {c.nome for c in sol.configuracoes}:
        raise HTTPException(404, "Configuracao desconhecida")
    ger().armazenamento.limpar_segredo(sol, nome)
    return {"ok": True}


@app.put("/api/solucoes/{sid}/endereco")
def api_endereco(sid: str, url_publica: str = Body("", embed=True), _: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    try:
        ger().armazenamento.definir_url_publica(sol.id, url_publica)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "url": ger().url(sol), "aviso": "Reinicie a solucao para aplicar o novo endereco."}


# ---------------- administracao: assistente "Nova solucao" ----------------

@app.post("/api/assistente/analisar")
def api_assistente_analisar(
    repositorio: str = Body(...), ref: str = Body("main"), subpasta: str = Body(""),
    _: Usuario = Depends(exigir_admin),
) -> dict:
    """Baixa o repositorio, descobre a tecnologia e sugere manifesto, Dockerfile e avisos."""
    try:
        return assistente.analisar_repositorio(CFG, ger().solucoes, repositorio, ref, subpasta)
    except assistente.ErroAssistente as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/assistente/criar", status_code=201)
def api_assistente_criar(dados: dict = Body(..., embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    """Grava a solucao neste servidor (dados/solucoes-locais) e recarrega o catalogo."""
    ocupadas = [sid for sid, t in ger().tarefas.items() if t.estado == "executando"]
    if ocupadas:
        raise HTTPException(409, f"Aguarde as operacoes em andamento: {', '.join(ocupadas)}")
    try:
        r = assistente.criar(CFG, ger().solucoes, dados)
    except assistente.ErroAssistente as exc:
        raise HTTPException(400, str(exc)) from exc
    _carregar()
    if r["id"] not in ger().solucoes:
        erros = [e for e in _estado["erros"] if r["id"] in e]
        raise HTTPException(400, f"Arquivos gravados, mas o catálogo recusou a solução: {'; '.join(erros) or 'veja Soluções'}")
    AUTH.registrar(autor, "Criou solução pelo assistente", f"{r['id']} a partir de {dados.get('repositorio', '')}")
    return {**r, "proxy": _estado.get("proxy")}


@app.get("/api/solucoes/{sid}/arquivos-locais")
def api_arquivos_locais(sid: str, _: Usuario = Depends(exigir_admin)) -> dict:
    try:
        return assistente.arquivos(CFG, sid)
    except assistente.ErroAssistente as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/solucoes/{sid}/local")
def api_remover_local(sid: str, autor: Usuario = Depends(exigir_admin)) -> dict:
    """Tira do catalogo uma solucao criada pelo assistente (o container e os dados precisam ser removidos antes)."""
    sol = ger().solucoes.get(sid)
    if sol is not None and not sol.local:
        raise HTTPException(400, "Esta solução vem do repositório do portal; remova o manifesto pelo Git.")
    if sol is not None and docker_cli.status().ok and docker_cli.container_inspect(sol.container):
        raise HTTPException(409, "Remova o container antes (Parar e depois Remover container).")
    try:
        assistente.remover(CFG, sid)
    except assistente.ErroAssistente as exc:
        raise HTTPException(404, str(exc)) from exc
    _carregar()
    AUTH.registrar(autor, "Removeu solução criada pelo assistente", sid)
    return {"ok": True}


# ---------------- administracao: usuarios ----------------

def _validar_solucoes(ids: list[str]) -> list[str]:
    desconhecidas = [s for s in ids if s not in ger().solucoes]
    if desconhecidas:
        raise HTTPException(400, f"Solucoes desconhecidas: {', '.join(desconhecidas)}")
    return ids


def _nomes_solucoes(ids) -> str:
    return ", ".join(ger().solucoes[s].nome if s in ger().solucoes else s for s in sorted(ids))


def _lista_curta(nomes: list[str], limite: int = 8) -> str:
    resto = len(nomes) - limite
    return ", ".join(nomes[:limite]) + (f" e mais {resto}" if resto > 0 else "")


@app.get("/api/usuarios")
def api_usuarios(_: Usuario = Depends(exigir_admin)) -> dict:
    return {
        "usuarios": AUTH.listar(),
        "solucoes": [
            {"id": s.id, "nome": s.nome, "categoria": s.categoria, "logo": _logo_url(s)}
            for s in ger().solucoes.values()
        ],
    }


@app.post("/api/usuarios", status_code=201)
def api_criar_usuario(
    usuario: str = Body(...),
    nome: str = Body(...),
    perfil: str = Body("usuario"),
    solucoes: list[str] | None = Body(None),
    autor: Usuario = Depends(exigir_admin),
) -> dict:
    solucoes = _validar_solucoes(solucoes or [])
    novo, senha = AUTH.criar_usuario(usuario, nome, perfil)
    if not novo.admin:
        AUTH.definir_acessos(novo.id, solucoes)
    detalhe = f"{novo.usuario} ({'administrador' if novo.admin else 'usuário'})"
    if solucoes and not novo.admin:
        detalhe += f" com acesso a {_nomes_solucoes(solucoes)}"
    AUTH.registrar(autor, "Criou usuário", detalhe)
    return {"usuario": _publico(novo), "senha_temporaria": senha}


@app.post("/api/usuarios/lote", status_code=201)
def api_criar_lote(
    usuarios: list[dict] = Body(...),
    solucoes: list[str] | None = Body(None),
    autor: Usuario = Depends(exigir_admin),
) -> dict:
    """Cria varios usuarios (todos ou nenhum) e devolve as senhas temporarias uma unica vez."""
    solucoes = _validar_solucoes(solucoes or [])
    criados = AUTH.criar_lote(usuarios, solucoes)
    detalhe = f"{len(criados)} usuário(s): {_lista_curta([u.usuario for u, _ in criados])}"
    if solucoes:
        detalhe += f". Acesso a {_nomes_solucoes(solucoes)}"
    AUTH.registrar(autor, "Criou usuários em lote", detalhe)
    return {"criados": [{**_publico(u), "senha_temporaria": s} for u, s in criados]}


@app.patch("/api/usuarios/{uid}")
def api_editar_usuario(
    uid: int,
    nome: str | None = Body(None),
    perfil: str | None = Body(None),
    ativo: bool | None = Body(None),
    solucoes: list[str] | None = Body(None),
    autor: Usuario = Depends(exigir_admin),
) -> dict:
    antes = AUTH.obter(uid)
    if antes is None:
        raise HTTPException(404, "Usuario nao encontrado")
    if solucoes is not None:
        _validar_solucoes(solucoes)
    acessos_antes = AUTH.acessos(uid)
    atualizado = AUTH.atualizar(uid, nome=nome, perfil=perfil, ativo=ativo, autor=autor)
    if solucoes is not None:
        AUTH.definir_acessos(uid, [] if atualizado.admin else solucoes)

    mudancas = []
    if atualizado.nome != antes.nome:
        mudancas.append(f"nome para '{atualizado.nome}'")
    if atualizado.perfil != antes.perfil:
        mudancas.append("perfil para administrador" if atualizado.admin else "perfil para usuário")
    if atualizado.ativo != antes.ativo:
        mudancas.append("reativou" if atualizado.ativo else "desativou")
    if solucoes is not None:
        depois = AUTH.acessos(uid)
        if depois - acessos_antes:
            mudancas.append(f"liberou {_nomes_solucoes(depois - acessos_antes)}")
        if acessos_antes - depois:
            mudancas.append(f"retirou {_nomes_solucoes(acessos_antes - depois)}")
    if mudancas:
        AUTH.registrar(autor, "Editou usuário", f"{antes.usuario}: {'; '.join(mudancas)}")
    return {"usuario": _publico(atualizado)}


@app.post("/api/usuarios/{uid}/redefinir-senha")
def api_redefinir_senha(uid: int, autor: Usuario = Depends(exigir_admin)) -> dict:
    if uid == autor.id:
        raise HTTPException(400, "Para mudar a sua propria senha use 'Trocar senha'.")
    alvo = AUTH.obter(uid)
    senha = AUTH.redefinir_senha(uid)
    AUTH.registrar(autor, "Redefiniu senha", alvo.usuario if alvo else str(uid))
    return {"senha_temporaria": senha}


@app.post("/api/usuarios/situacao")
def api_situacao_lote(
    usuarios: list[int] = Body(...),
    ativo: bool = Body(...),
    autor: Usuario = Depends(exigir_admin),
) -> dict:
    """Ativa ou desativa varios usuarios. Quem nao pode mudar (ex.: o proprio admin) volta em `erros`."""
    alterados, erros = [], []
    for uid in dict.fromkeys(usuarios):
        atual = AUTH.obter(uid)
        if atual is None:
            continue
        if atual.ativo == ativo:
            continue
        try:
            AUTH.atualizar(uid, ativo=ativo, autor=autor)
            alterados.append(atual.usuario)
        except ErroAuth as exc:
            erros.append(f"{atual.usuario}: {exc}")
    if alterados:
        AUTH.registrar(autor, "Reativou usuários" if ativo else "Desativou usuários",
                       _lista_curta(alterados))
    return {"alterados": len(alterados), "erros": erros}


# ---------------- administracao: acessos ----------------

@app.post("/api/acessos")
def api_acessos(
    usuarios: list[int] = Body(...),
    solucoes: list[str] = Body(...),
    conceder: bool = Body(...),
    autor: Usuario = Depends(exigir_admin),
) -> dict:
    """Libera (conceder=true) ou retira sistemas de varios usuarios de uma vez."""
    _validar_solucoes(solucoes)
    alterados = AUTH.alterar_acessos(usuarios, solucoes, conceder)
    if alterados:
        nomes = AUTH.nomes(usuarios)
        AUTH.registrar(
            autor,
            "Liberou acesso" if conceder else "Retirou acesso",
            f"{_nomes_solucoes(solucoes)} para {_lista_curta([nomes[u] for u in usuarios if u in nomes])}",
        )
    return {"alterados": alterados}


@app.get("/api/historico")
def api_historico(limite: int = 200, _: Usuario = Depends(exigir_admin)) -> dict:
    return {"eventos": AUTH.historico(limite)}
