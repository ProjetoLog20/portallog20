"""API e interface web do Portal de Solucoes LOG20."""

from __future__ import annotations

import contextlib
import html
import os
import re
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config as config_mod
from . import assistente, docker_cli, proxy
from . import supabase as sb
from . import supabase_copia, supabase_estrutura
from . import fontes as fontes_mod
from .backup import Backups, ErroBackup, formatar_tamanho, resumo
from .catalog import cpus_validas, memoria_mb
from .uso import INFRA, Uso
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
    _limpar_somente_admin(solucoes)


def _limpar_somente_admin(solucoes: dict) -> None:
    """Liberacoes antigas de solucoes que agora sao so de administradores deixam de existir."""
    so_admin = [sid for sid, s in solucoes.items() if s.somente_admin]
    try:
        retiradas = AUTH.retirar_de_todos(so_admin)
    except NameError:  # primeira carga: AUTH ainda nao existe (a limpeza roda no startup)
        return
    if retiradas:
        por_sol: dict[str, list[str]] = {}
        for usuario, sid in retiradas:
            por_sol.setdefault(sid, []).append(usuario)
        for sid, usuarios in por_sol.items():
            AUTH.registrar(None, "Retirou acesso",
                           f"{solucoes[sid].nome} para {', '.join(sorted(usuarios))} (agora só administradores)")


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


# Backup diario e restauracao (tela Backups). O agendador roda dentro do portal.
BACKUPS = Backups(CFG, ger)


# Uso de memoria, CPU e disco (tela Uso): coletor em segundo plano.
USO = Uso(CFG, ger, lambda: BACKUPS.pasta)


@app.on_event("startup")
def _iniciar_agendador() -> None:
    _limpar_somente_admin(ger().solucoes)
    BACKUPS.iniciar_agendador()
    USO.iniciar()


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
        # O prazo de verdade fica no servidor (vence depois de PORTAL_SESSAO_HORAS sem uso, inclusive
        # o uso dos sistemas pelo Caddy, que nao repassa cookies novos): o do navegador e o maximo.
        max_age=Autenticacao.SESSAO_MAX_S,
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
                                 "Este sistema só é exibido para quem está conectado ao Portal de Soluções.", 401)
        return JSONResponse({"detail": "Sessao do portal expirada. Entre novamente."}, status_code=401)
    if solucao not in ger().solucoes or not _pode_abrir(usuario, ger().solucoes[solucao]):
        if navegacao and solucao in ger().solucoes and ger().solucoes[solucao].somente_admin:
            return _pagina_aviso("Só para administradores",
                                 f"{usuario.nome}, este sistema dá acesso total aos bancos de dados e só abre "
                                 "para administradores do portal.", 403)
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
    if sol is None or sol.logo is None or not sol.logo.is_file() or not _pode_abrir(usuario, sol):
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
        if permitidas is not None and (sol.id not in permitidas or sol.somente_admin):
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
        "externo": bool(sol.externo),
        "somente_admin": sol.somente_admin,
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
        "campos_banco": sorted(sol.banco) if sol.banco else [],
        "supabase": _detalhe_supabase(sol, docker_ok),
        "externo": bool(sol.externo),
        "somente_admin": sol.somente_admin,
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
                "construcao": sol.variaveis_na_construcao or c.na_construcao,
                # Segredos nunca saem do portal: so informamos se existe valor.
                "valor": None if c.tipo == "segredo" else valores.get(c.nome, ""),
                "definido": bool(valores.get(c.nome)),
                # Preenchida pelo portal com o Supabase da solucao: a tela mostra, mas nao edita.
                "gerenciada": c.nome in sol.nomes_supabase,
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
    de_build = [c.rotulo for c in sol.configuracoes
                if (sol.variaveis_na_construcao or c.na_construcao) and c.nome in valores]
    if de_build:
        return {"ok": True, "acao": "reconstruir",
                "aviso": f"{', '.join(de_build)} entra(m) no sistema na criação. Atualize-o para valer."}
    return {"ok": True, "acao": "iniciar", "aviso": "Reinicie a solucao para aplicar as mudancas."}


def _studio() -> Solucao | None:
    return next((s for s in ger().solucoes.values() if s.externo == f"{sb.STUDIO}:3000"), None)


def _detalhe_supabase(sol: Solucao, docker_ok: bool) -> dict | None:
    if not sol.supabase:
        return None
    studio = _studio()
    estado = sb.estado(sol.id) if docker_ok else None
    rotulos = {"url": "endereço, o mesmo do sistema", "chave_anon": "chave pública (anon)",
               "url_interna": "endereço na rede interna, para o servidor do sistema",
               "chave_servico": "chave de serviço (service_role), só no servidor"}
    imp = supabase_copia.ultima_importacao(CFG.pasta_dados, sol.id)
    if imp:
        try:
            BACKUPS.obter(imp["backup"])
            imp["existe"] = True
        except ErroBackup:
            imp["existe"] = False
    return {
        "variaveis": [{"nome": nome, "conteudo": rotulos[campo]} for campo, nome in sol.supabase.items() if nome],
        "estado": estado,
        "studio": {"porta": studio.porta_host, "url": ger().url(studio) + "/"} if studio else None,
        "volumes": sb.volumes(sol.id),
        "realtime": sol.supabase_realtime,
        "importacao": imp,
        "login": _login_publico(sol.id),
    }


def _login_publico(sid: str) -> dict:
    """Configuracao de login e e-mail para a tela (sem a senha do e-mail)."""
    lg = sb.ler_login(CFG.pasta_dados, sid)
    _, origem = sb.smtp_efetivo(lg)
    dados = {k: v for k, v in vars(lg).items() if k != "smtp_senha"}
    dados.update({"tem_senha": bool(lg.smtp_senha), "origem_smtp": origem,
                  "servidor_global": sb.smtp_global()["servidor"]})
    return dados


SMTP_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")


def _validar_login(dados: dict, atual: sb.LoginConfig) -> sb.LoginConfig:
    servidor = str(dados.get("smtp_servidor") or "").strip()
    cfg = sb.LoginConfig(cadastro_aberto=bool(dados.get("cadastro_aberto", True)))
    if not servidor:
        return cfg  # sem servidor: usa o e-mail do .env (e esquece a senha guardada)
    if not SMTP_HOST_RE.match(servidor):
        raise HTTPException(400, "Servidor de e-mail inválido (ex.: smtp.office365.com).")
    porta = str(dados.get("smtp_porta") or "587").strip()
    if not porta.isdigit() or not 1 <= int(porta) <= 65535:
        raise HTTPException(400, "Porta do e-mail inválida (normalmente 587 ou 465).")
    remetente = str(dados.get("smtp_remetente") or "").strip()
    if not EMAIL_RE.match(remetente):
        raise HTTPException(400, "Informe o e-mail do remetente (ex.: nao-responda@log20.com.br).")
    usuario = str(dados.get("smtp_usuario") or "").strip()[:200]
    senha = str(dados.get("smtp_senha") or "")
    if not senha and usuario and atual.smtp_servidor == servidor and atual.smtp_usuario == usuario:
        senha = atual.smtp_senha  # em branco mantem a guardada (mesmo servidor e usuario)
    cfg.smtp_servidor, cfg.smtp_porta, cfg.smtp_usuario, cfg.smtp_senha = servidor, porta, usuario, senha[:500]
    cfg.smtp_remetente, cfg.smtp_nome = remetente, " ".join(str(dados.get("smtp_nome") or "").split())[:80]
    return cfg


@app.put("/api/solucoes/{sid}/supabase/login")
def api_supabase_login(sid: str, dados: dict = Body(..., embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    """E-mail do login (convite, confirmacao, esqueci a senha) e cadastro aberto/fechado do Supabase
    da solucao. Se a pilha ja existe, so o container do login e recriado (os dados ficam)."""
    sol = _solucao(sid)
    if not sol.supabase:
        raise HTTPException(400, "Esta solucao nao usa o Supabase do servidor.")
    if any(x and x.get("estado") == "executando"
           for x in (supabase_copia.situacao(sid), supabase_estrutura.situacao(sid))):
        raise HTTPException(409, "Há uma importação em andamento. Espere terminar.")
    atual = sb.ler_login(CFG.pasta_dados, sid)
    novo = _validar_login(dados, atual)
    sb.gravar_login(CFG.pasta_dados, sid, novo)
    aplicado = False
    if sb.gateway_no_ar(sid):
        try:
            sb.garantir(sid, ger().url(sol), CFG.pasta_dados, CFG.rede_docker, lambda _: None,
                        realtime=sol.supabase_realtime)
            aplicado = True
        except (sb.ErroSupabase, docker_cli.DockerError) as exc:
            raise HTTPException(409, f"Configuracao salva, mas o login nao reiniciou: {exc}") from exc
    AUTH.registrar(autor, "Alterou login e e-mail do Supabase",
                   f"{sid}: e-mail {'do sistema' if novo.smtp_servidor else 'do servidor'}, "
                   f"cadastro {'aberto' if novo.cadastro_aberto else 'fechado'}")
    return {"ok": True, "aplicado": aplicado, "login": _login_publico(sid)}


@app.post("/api/solucoes/{sid}/supabase/login/testar")
def api_supabase_login_testar(sid: str, dados: dict = Body(..., embed=True), _: Usuario = Depends(exigir_admin)) -> dict:
    """Conecta no servidor de e-mail e confere usuario e senha, sem enviar nada."""
    import smtplib
    import ssl

    sol = _solucao(sid)
    if not sol.supabase:
        raise HTTPException(400, "Esta solucao nao usa o Supabase do servidor.")
    atual = sb.ler_login(CFG.pasta_dados, sid)
    cfg = _validar_login(dados, atual) if str(dados.get("smtp_servidor") or "").strip() else atual
    smtp, origem = sb.smtp_efetivo(cfg)
    if origem == "nenhum":
        raise HTTPException(400, "Nenhum servidor de e-mail configurado (nem neste sistema, nem no .env).")
    porta = int(smtp["porta"] or 587)
    try:
        ctx = ssl.create_default_context()
        if porta == 465:
            con = smtplib.SMTP_SSL(smtp["servidor"], porta, timeout=15, context=ctx)
        else:
            con = smtplib.SMTP(smtp["servidor"], porta, timeout=15)
            con.ehlo()
            if con.has_extn("starttls"):
                con.starttls(context=ctx)
                con.ehlo()
        with con:
            if smtp["usuario"]:
                con.login(smtp["usuario"], smtp["senha"])
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(400, "O servidor de e-mail recusou o usuário ou a senha.") from None
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(400, f"Não foi possível conectar em {smtp['servidor']}:{porta}: {exc}") from None
    return {"ok": True, "mensagem": f"Conexão com {smtp['servidor']}:{porta} OK"
            + (" e usuário/senha aceitos." if smtp["usuario"] else " (sem usuário).")
            + (" Usando o e-mail do servidor (.env)." if origem == "servidor" else "")}


@app.post("/api/solucoes/{sid}/supabase/desfazer")
def api_supabase_desfazer(sid: str, confirmacao: str = Body("", embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    """Volta o Supabase da solucao (banco, usuarios, arquivos e chaves) ao backup feito antes da
    ultima importacao. Antes, a restauracao faz outro backup de seguranca (da para desfazer o desfazer)."""
    sol = _solucao(sid)
    if not sol.supabase:
        raise HTTPException(400, "Esta solucao nao usa o Supabase do servidor.")
    if confirmacao.strip() != sid:
        raise HTTPException(400, f"Para confirmar, digite o identificador da solução: {sid}")
    imp = supabase_copia.ultima_importacao(CFG.pasta_dados, sid)
    if not imp:
        raise HTTPException(404, "Nenhuma importação registrada para este sistema.")
    copia = supabase_copia.situacao(sid)
    est = supabase_estrutura.situacao(sid)
    if any(x and x.get("estado") == "executando" for x in (copia, est)):
        raise HTTPException(409, "Há uma importação em andamento. Espere terminar.")
    t = ger().tarefas.get(sid)
    if t and t.estado == "executando":
        raise HTTPException(409, f"A solucao tem uma operacao em andamento: {t.acao}")
    nome = imp["backup"]
    try:
        BACKUPS.obter(nome)

        def rodar(tarefa) -> None:
            tarefa.etapa = f"Desfazendo a importação de {sol.nome}"
            BACKUPS.restaurar_solucao(nome, sid, banco=False, arquivos=False, supabase=True,
                                      registrar=tarefa.registrar)
            supabase_copia.arquivo_importacao(CFG.pasta_dados, sid).unlink(missing_ok=True)

        BACKUPS.em_segundo_plano(f"desfazer importacao de {sid}", rodar)
    except ErroBackup as exc:
        raise HTTPException(409, str(exc)) from exc
    AUTH.registrar(autor, "Desfez a importação do Supabase", f"{sid}: voltou ao backup {nome}")
    return {"ok": True}


@app.post("/api/solucoes/{sid}/supabase/studio")
def api_abrir_studio(sid: str, autor: Usuario = Depends(exigir_admin)) -> dict:
    """Aponta o Supabase Studio (um so no servidor) para a pilha desta solucao."""
    sol = _solucao(sid)
    if not sol.supabase:
        raise HTTPException(400, "Esta solucao nao usa o Supabase do servidor.")
    studio = _studio()
    if studio is None:
        raise HTTPException(400, "A solucao 'supabase-studio' nao esta no catalogo deste portal.")
    try:
        sb.abrir_studio(sol.id, sol.nome, ger().url(sol), CFG.pasta_dados, CFG.rede_docker, lambda _: None)
    except (sb.ErroSupabase, docker_cli.DockerError) as exc:
        raise HTTPException(409, str(exc)) from exc
    AUTH.registrar(autor, "Abriu o Supabase Studio", sid)
    return {"ok": True, "url": ger().url(studio) + "/"}


@app.post("/api/solucoes/{sid}/supabase/copiar", status_code=202)
def api_copiar_supabase(sid: str, dados: dict = Body(..., embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    """Copia tabelas, usuarios, regras e arquivos de outro Supabase (ex.: o da nuvem) para o desta
    solucao. Substitui o que estiver no local (com copia de seguranca antes). A senha e a chave
    da origem so passam pela memoria: nao sao gravadas nem registradas."""
    sol = _solucao(sid)
    if not sol.supabase:
        raise HTTPException(400, "Esta solucao nao usa o Supabase do servidor.")
    if str(dados.get("confirmacao") or "").strip() != sid:
        raise HTTPException(400, f"Para confirmar, digite o identificador da solução: {sid}")
    est = supabase_estrutura.situacao(sid)
    if est and est.get("estado") == "executando":
        raise HTTPException(409, "Há uma criação de estrutura em andamento. Espere terminar.")
    try:
        origem, partes = supabase_copia.validar(dados)
        supabase_copia.iniciar(sid, sol.nome, origem, partes, CFG.pasta_dados, BACKUPS.pasta,
                               seguranca=lambda reg: _backup_antes_de_importar(sid, reg))
    except supabase_copia.ErroCopia as exc:
        raise HTTPException(400, str(exc)) from exc
    AUTH.registrar(autor, "Copiou dados para o Supabase do sistema",
                   f"{sid}: {', '.join(k for k, v in partes.items() if v)} de {origem.usuario}@{origem.host}")
    return {"ok": True, "copia": supabase_copia.situacao(sid)}


def _backup_antes_de_importar(sid: str, registrar) -> str:
    man = BACKUPS.fazer("antes-de-importar", registrar, somente=[sid])
    if not man["ok"]:
        raise ErroBackup("; ".join(man["erros"])[:500])
    if not any(i["tipo"] == "supabase-volume" and i.get("chave") == "dados" and i.get("solucao") == sid
               for i in man["itens"]):
        raise ErroBackup("o backup nao trouxe o Supabase do sistema")
    return man["nome"]


def _raiz_codigo(sol: Solucao) -> Path:
    """Pasta do codigo da solucao no servidor (a do ultimo Instalar/Atualizar)."""
    if sol.repositorio:
        raiz = fontes_mod.pasta_do_repositorio(CFG.pasta_dados / "fontes", sol.repositorio, sol.ref)
    else:
        raiz = sol.contexto_build
    if not raiz or not Path(raiz).is_dir():
        raise HTTPException(409, "O código deste sistema ainda não foi baixado. Clique em Instalar e tente de novo.")
    return Path(raiz)


@app.get("/api/solucoes/{sid}/supabase/estrutura")
def api_estrutura_supabase(sid: str, _: Usuario = Depends(exigir_admin)) -> dict:
    """Arquivos .sql do repositorio, se o banco esta vazio e a ultima execucao."""
    sol = _solucao(sid)
    if not sol.supabase:
        raise HTTPException(400, "Esta solucao nao usa o Supabase do servidor.")
    execucao = supabase_estrutura.situacao(sid)
    raiz = _raiz_codigo(sol)
    try:
        tabelas = supabase_estrutura.tabelas_public(CFG.pasta_dados, sid)
    except (supabase_copia.ErroCopia, docker_cli.DockerError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"arquivos": supabase_estrutura.listar(raiz, sol.subpasta), "tabelas": tabelas, "execucao": execucao}


@app.post("/api/solucoes/{sid}/supabase/estrutura", status_code=202)
def api_criar_estrutura(sid: str, arquivos: list[str] = Body(..., embed=True), confirmacao: str = Body("", embed=True),
                        autor: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    if not sol.supabase:
        raise HTTPException(400, "Esta solucao nao usa o Supabase do servidor.")
    if confirmacao.strip() != sid:
        raise HTTPException(400, f"Para confirmar, digite o identificador da solução: {sid}")
    copia = supabase_copia.situacao(sid)
    if copia and copia.get("estado") == "executando":
        raise HTTPException(409, "Há uma importação em andamento. Espere terminar.")
    raiz = _raiz_codigo(sol)
    try:
        supabase_estrutura.iniciar(sid, raiz, arquivos, CFG.pasta_dados,
                                   seguranca=lambda reg: _backup_antes_de_importar(sid, reg))
    except supabase_copia.ErroCopia as exc:
        raise HTTPException(400, str(exc)) from exc
    AUTH.registrar(autor, "Criou a estrutura do Supabase pelos .sql", f"{sid}: {len(arquivos)} arquivo(s)")
    return {"ok": True, "execucao": supabase_estrutura.situacao(sid)}


@app.get("/api/solucoes/{sid}/supabase/estrutura/situacao")
def api_estrutura_situacao(sid: str, _: Usuario = Depends(exigir_admin)) -> dict:
    _solucao(sid)
    return {"execucao": supabase_estrutura.situacao(sid)}


@app.get("/api/solucoes/{sid}/supabase/copiar")
def api_situacao_copia(sid: str, _: Usuario = Depends(exigir_admin)) -> dict:
    _solucao(sid)
    return {"copia": supabase_copia.situacao(sid)}


@app.post("/api/solucoes/{sid}/banco/restaurar")
def api_restaurar_banco(sid: str, autor: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    if not sol.banco:
        raise HTTPException(400, "Esta solucao nao usa o banco do servidor.")
    ja_havia = ger().armazenamento.restaurar_banco(sol)
    AUTH.registrar(autor, "Voltou a solução para o banco do servidor", sid)
    return {"ok": True, "aviso": "Reinicie a solucao para aplicar." if ja_havia
            else "O banco sera criado e preenchido no proximo Iniciar."}


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


@app.get("/api/assistente/solucoes/{sid}")
def api_assistente_ler(sid: str, _: Usuario = Depends(exigir_admin)) -> dict:
    """Valores atuais de uma solucao criada pelo assistente, para a tela de edicao."""
    try:
        return assistente.para_edicao(CFG, _solucao(sid))
    except assistente.ErroAssistente as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/assistente/solucoes/{sid}")
def api_assistente_editar(sid: str, dados: dict = Body(..., embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    sol = _solucao(sid)
    tarefa = ger().tarefas.get(sid)
    if tarefa and tarefa.estado == "executando":
        raise HTTPException(409, "Aguarde a operação em andamento nesta solução.")
    try:
        antes = assistente.para_edicao(CFG, sol)
        r = assistente.editar(CFG, ger().solucoes, sid, dados)
    except assistente.ErroAssistente as exc:
        raise HTTPException(400, str(exc)) from exc
    _carregar()
    depois = assistente.para_edicao(CFG, _solucao(sid))
    def do_build(e: dict) -> tuple:
        construcao = sorted(c["nome"] for c in e["configuracoes"] if c.get("construcao"))
        return (e["dockerfile"], e["arquivos"], e["repositorio"], e["ref"], e["subpasta"],
                e.get("variaveis_na_construcao"), construcao, e["ambiente"] if e.get("variaveis_na_construcao") else None)

    mudou_build = do_build(antes) != do_build(depois)
    AUTH.registrar(autor, "Editou solução do assistente", sid)
    return {**r, "aviso": "Para aplicar, clique em Atualizar: o Dockerfile, a origem do código ou as variáveis da construção mudaram." if mudou_build
            else "Para aplicar, clique em Reiniciar.", "acao": "reconstruir" if mudou_build else "iniciar"}


@app.get("/api/solucoes/{sid}/arquivos-locais.zip", include_in_schema=False)
def api_arquivos_zip(sid: str, _: Usuario = Depends(exigir_admin)) -> Response:
    try:
        conteudo = assistente.zip_arquivos(CFG, sid)
    except assistente.ErroAssistente as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(conteudo, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="solucao-{sid}.zip"', "Cache-Control": "no-store"})


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
        raise HTTPException(400, "Esta solução vem do repositório do portal. Remova o manifesto pelo Git.")
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

def _validar_solucoes(ids: list[str], conceder: bool = True) -> list[str]:
    desconhecidas = [s for s in ids if s not in ger().solucoes]
    if desconhecidas:
        raise HTTPException(400, f"Solucoes desconhecidas: {', '.join(desconhecidas)}")
    so_admin = [ger().solucoes[s].nome for s in ids if ger().solucoes[s].somente_admin]
    if so_admin and conceder:
        raise HTTPException(400, f"{', '.join(so_admin)}: só administradores do portal acessam. "
                                 "Para liberar, torne a pessoa administradora.")
    return ids


def _pode_abrir(usuario: Usuario, sol: Solucao) -> bool:
    """Admin abre tudo; os demais, o que foi liberado em Acessos e nao e so de administradores."""
    return usuario.admin or (not sol.somente_admin and AUTH.pode_acessar(usuario, sol.id))


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
            {"id": s.id, "nome": s.nome, "categoria": s.categoria, "logo": _logo_url(s),
             "somente_admin": s.somente_admin}
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
    acessos_antes = AUTH.acessos(uid)
    if solucoes is not None:
        _validar_solucoes(solucoes, conceder=False)
        _validar_solucoes([s for s in solucoes if s not in acessos_antes])  # so as novas liberacoes
        # Liberacao antiga de solucao so de administradores nao bloqueia a edicao: sai da lista.
        solucoes = [s for s in solucoes if not ger().solucoes[s].somente_admin]
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
    _validar_solucoes(solucoes, conceder)
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


# ---------------- backups ----------------

def _tarefa_backup() -> dict | None:
    t = BACKUPS.tarefa
    if t is None:
        return None
    return {"acao": t.acao, "estado": t.estado, "etapa": t.etapa, "erro": t.erro, "inicio": t.inicio,
            "fim": t.fim, "log": "\n".join(t.log)}


@app.get("/api/backups")
def api_backups(_: Usuario = Depends(exigir_admin)) -> dict:
    from dataclasses import asdict

    proximo = BACKUPS.proximo()
    disco = BACKUPS.disco()
    solucoes = []
    for sol in sorted(ger().solucoes.values(), key=lambda s: s.nome.lower()):
        solucoes.append({"id": sol.id, "nome": sol.nome, "banco": bool(sol.banco), "volumes": list(sol.volumes),
                         "externo": sol.backup_externo, "supabase": bool(sol.supabase),
                         "manifesto_inclui": sol.backup_incluir})
    return {
        "pasta": str(BACKUPS.pasta),
        "copia": str(CFG.backup_copia) if CFG.backup_copia else None,
        "postgres": bool(CFG.postgres_container),
        "disco": {**disco, "livre_texto": formatar_tamanho(disco.get("livre", 0)),
                  "total_texto": formatar_tamanho(disco.get("total", 0))},
        "ajustes": asdict(BACKUPS.ajustes()),
        "padroes": asdict(BACKUPS.padroes()),
        "personalizado": BACKUPS.personalizado(),
        "estado": BACKUPS.estado(),
        "proximo": proximo.isoformat(timespec="minutes") if proximo else None,
        "tarefa": _tarefa_backup(),
        "backups": [resumo(m) for m in BACKUPS.listar()],
        "importacoes": BACKUPS.listar_importacoes(),
        "importacao_max_gb": IMPORTACAO_MAX_GB,
        "importacao_versao": BACKUPS.versao_servidor(),
        "solucoes": solucoes,
    }


@app.put("/api/backups/ajustes")
def api_backups_ajustes(ajustes: dict | None = Body(None, embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    from dataclasses import asdict

    try:
        aj = BACKUPS.salvar_ajustes(ajustes)
    except (ErroBackup, ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    AUTH.registrar(autor, "Alterou os ajustes de backup" if ajustes is not None else "Voltou os ajustes de backup ao padrao")
    return {"ok": True, "ajustes": asdict(aj)}


@app.post("/api/backups")
def api_backup_agora(solucao: str = Body("", embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    """Sem `solucao`: backup completo. Com `solucao`: backup isolado so dela."""
    if solucao:
        sol = _solucao(solucao)
        if not sol.backup_incluir or solucao in BACKUPS.ajustes().excluir:
            raise HTTPException(409, f"{sol.nome} esta fora do backup (manifesto ou Ajustes).")

        def rodar(tarefa) -> None:
            tarefa.etapa = f"Backup de {sol.nome}"
            man = BACKUPS.fazer_solucao(solucao, tarefa.registrar)
            if not man["ok"]:
                tarefa.erro = f"{len(man['erros'])} item(ns) com erro: " + "; ".join(man["erros"])[:500]

        try:
            BACKUPS.em_segundo_plano(f"backup de {sol.nome}", rodar)
        except ErroBackup as exc:
            raise HTTPException(409, str(exc)) from exc
        AUTH.registrar(autor, "Fez backup de uma solucao", solucao)
        return {"ok": True}
    try:
        BACKUPS.em_segundo_plano("backup manual", lambda tarefa: BACKUPS._rodar_backup(tarefa, "manual"))
    except ErroBackup as exc:
        raise HTTPException(409, str(exc)) from exc
    AUTH.registrar(autor, "Fez backup manual")
    return {"ok": True}


@app.post("/api/backups/apagar")
def api_backups_apagar_todos(confirmacao: str = Body("", embed=True), solucao: str = Body("", embed=True),
                             autor: Usuario = Depends(exigir_admin)) -> dict:
    """Apaga de uma vez todos os backups (ou so os isolados de uma solucao), menos os fixados."""
    if confirmacao.strip().upper() != "APAGAR":
        raise HTTPException(400, "Digite APAGAR para confirmar.")
    if solucao:
        _solucao(solucao)
    t = BACKUPS.tarefa
    if t and t.estado == "executando":
        raise HTTPException(409, f"Ja ha uma operacao em andamento: {t.acao}")
    try:
        r = BACKUPS.apagar_todos(solucao or None)
    except (ErroBackup, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
    AUTH.registrar(autor, "Apagou backups de uma vez",
                   f"{len(r['apagados'])} backup(s)" + (f" isolados de {solucao}" if solucao else "")
                   + (f"; {r['fixados']} fixado(s) mantido(s)" if r["fixados"] else "")
                   + (f"; {len(r['erros'])} com erro" if r["erros"] else ""))
    return {"ok": True, "apagados": len(r["apagados"]), "fixados": r["fixados"], "erros": r["erros"]}


@app.post("/api/backups/{nome}/verificar")
def api_backup_verificar(nome: str, _: Usuario = Depends(exigir_admin)) -> dict:
    try:
        BACKUPS.obter(nome)

        def rodar(tarefa) -> None:
            tarefa.etapa = "Conferindo"
            r = BACKUPS.verificar(nome, tarefa.registrar)
            if not r["ok"]:
                tarefa.erro = "; ".join(r["problemas"])[:500]

        BACKUPS.em_segundo_plano(f"verificar {nome}", rodar)
    except ErroBackup as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.post("/api/backups/{nome}/restaurar")
def api_backup_restaurar(nome: str, solucao: str = Body(..., embed=True), banco: bool = Body(True, embed=True),
                         arquivos: bool = Body(True, embed=True), supabase: bool = Body(True, embed=True),
                         confirmacao: str = Body("", embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    if confirmacao.strip() != solucao:
        raise HTTPException(400, "Digite o identificador da solucao para confirmar.")
    if not banco and not arquivos and not supabase:
        raise HTTPException(400, "Escolha o que restaurar.")
    _solucao(solucao)
    t = ger().tarefas.get(solucao)
    if t and t.estado == "executando":
        raise HTTPException(409, f"A solucao tem uma operacao em andamento: {t.acao}")
    try:
        BACKUPS.obter(nome)

        def rodar(tarefa) -> None:
            tarefa.etapa = f"Restaurando {solucao}"
            BACKUPS.restaurar_solucao(nome, solucao, banco=banco, arquivos=arquivos, supabase=supabase,
                                      registrar=tarefa.registrar)

        BACKUPS.em_segundo_plano(f"restaurar {solucao}", rodar)
    except ErroBackup as exc:
        raise HTTPException(409, str(exc)) from exc
    partes = " e ".join(p for p, ok in (("banco", banco), ("arquivos", arquivos), ("Supabase", supabase)) if ok)
    AUTH.registrar(autor, "Restaurou backup", f"{solucao} ({partes}) a partir de {nome}")
    return {"ok": True}


@app.put("/api/backups/{nome}/fixar")
def api_backup_fixar(nome: str, fixado: bool = Body(..., embed=True), autor: Usuario = Depends(exigir_admin)) -> dict:
    try:
        BACKUPS.fixar(nome, fixado)
    except ErroBackup as exc:
        raise HTTPException(404, str(exc)) from exc
    AUTH.registrar(autor, "Fixou backup" if fixado else "Desfixou backup", nome)
    return {"ok": True}


@app.delete("/api/backups/{nome}")
def api_backup_apagar(nome: str, autor: Usuario = Depends(exigir_admin)) -> dict:
    try:
        BACKUPS.remover(nome)
    except ErroBackup as exc:
        raise HTTPException(409, str(exc)) from exc
    AUTH.registrar(autor, "Apagou backup", nome)
    return {"ok": True}


# ---------------- importar arquivo (.sql, .dump, .zip, .tar.gz) ----------------

IMPORTACAO_MAX_GB = float(os.environ.get("PORTAL_IMPORTACAO_MAX_GB", "").strip() or 10)


@app.post("/api/backups/importacoes")
async def api_importacao_enviar(request: Request, nome: str = "", autor: Usuario = Depends(exigir_admin)) -> dict:
    """Recebe o arquivo no corpo da requisicao (sem formulario), gravando direto no disco."""
    from starlette.concurrency import run_in_threadpool

    try:
        iid, destino = BACKUPS.nova_importacao(nome)
    except ErroBackup as exc:
        raise HTTPException(400, str(exc)) from exc
    limite = int(IMPORTACAO_MAX_GB * 1024 ** 3)
    total = 0
    try:
        with open(destino, "wb") as fh:
            async for parte in request.stream():
                total += len(parte)
                if total > limite:
                    raise ErroBackup(f"Arquivo maior que o limite de {IMPORTACAO_MAX_GB:g} GB (PORTAL_IMPORTACAO_MAX_GB).")
                fh.write(parte)
        if total == 0:
            raise ErroBackup("O arquivo chegou vazio.")
        info = await run_in_threadpool(BACKUPS.analisar_importacao, iid, destino)
    except ErroBackup as exc:
        with contextlib.suppress(ErroBackup):
            BACKUPS.apagar_importacao(iid)
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        with contextlib.suppress(ErroBackup):
            BACKUPS.apagar_importacao(iid)
        raise
    AUTH.registrar(autor, "Enviou arquivo para importar", f"{info['arquivo']} ({formatar_tamanho(total)})")
    return info


@app.post("/api/backups/importacoes/{iid}/aplicar")
def api_importacao_aplicar(iid: str, solucao: str = Body(..., embed=True), chave: str = Body("", embed=True),
                           juntar: bool = Body(False, embed=True), confirmacao: str = Body("", embed=True),
                           autor: Usuario = Depends(exigir_admin)) -> dict:
    if confirmacao.strip() != solucao:
        raise HTTPException(400, "Digite o identificador da solucao para confirmar.")
    _solucao(solucao)
    t = ger().tarefas.get(solucao)
    if t and t.estado == "executando":
        raise HTTPException(409, f"A solucao tem uma operacao em andamento: {t.acao}")
    try:
        info = BACKUPS.importacao(iid)

        def rodar(tarefa) -> None:
            tarefa.etapa = f"Importando em {solucao}"
            BACKUPS.aplicar_importacao(iid, solucao, chave=chave, juntar=juntar, registrar=tarefa.registrar)

        BACKUPS.em_segundo_plano(f"importar {info['arquivo']} em {solucao}", rodar)
    except ErroBackup as exc:
        raise HTTPException(409, str(exc)) from exc
    AUTH.registrar(autor, "Importou arquivo", f"{info['arquivo']} em {solucao}" + (f" ({chave})" if chave else " (banco)"))
    return {"ok": True}


@app.delete("/api/backups/importacoes/{iid}")
def api_importacao_apagar(iid: str, _: Usuario = Depends(exigir_admin)) -> dict:
    try:
        BACKUPS.apagar_importacao(iid)
    except ErroBackup as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


# ---------------- uso de recursos e limites ----------------

def _mb_para_bytes(mb: int) -> int:
    return int(mb) * 1024 * 1024


@app.get("/api/uso")
def api_uso(horas: float = 24, _: Usuario = Depends(exigir_admin)) -> dict:
    import math

    from . import banco as banco_pg

    horas = min(max(horas, 0.25), CFG.uso_dias * 24)
    atual = USO.atual()
    tam = USO.tamanhos()
    hist = USO.historico(horas)
    picos = hist.pop("picos_24h")
    alvos = []
    for sol in sorted(ger().solucoes.values(), key=lambda s: s.nome.lower()):
        if sol.externo:
            continue  # sem container proprio (ex.: Supabase Studio): os containers aparecem como infraestrutura
        c = atual["conteineres"].get(sol.id) or {}
        lim = ger().limites(sol)
        guardado = ger().armazenamento.banco(sol.id) or {}
        nome_banco = guardado.get("nome") or (banco_pg.nomes(sol.id)[0] if sol.banco else "")
        imagem = sol.imagem if ":" in sol.imagem.rsplit("/", 1)[-1] else f"{sol.imagem}:latest"
        pico = picos.get(sol.id) or {}
        sugestao = None
        if pico.get("mem"):
            sugestao = max(256, math.ceil(pico["mem"] * 1.5 / (128 * 1024 * 1024)) * 128)
        alvos.append({
            "id": sol.id, "nome": sol.nome, "tipo": "solucao", "status": ger().status(sol),
            "rodando": sol.id in atual["conteineres"],
            "cpu": c.get("cpu"), "mem": c.get("mem"), "mem_limite_docker": c.get("mem_limite"),
            "processos": c.get("processos"), "limites": lim,
            "manifesto": {"memoria_mb": sol.recursos_memoria_mb, "cpus": sol.recursos_cpus},
            "pico_mem_24h": pico.get("mem"), "pico_cpu_24h": pico.get("cpu"), "sugestao_mb": sugestao,
            "disco": {"volumes": sum(tam["volumes"].get(sol.nome_volume(k), 0) for k in sol.volumes),
                      "banco": tam["bancos"].get(nome_banco, 0) if nome_banco else 0,
                      "imagem": tam["imagens"].get(imagem, 0)},
        })
    from .uso import ALVO_SUPABASE, SUPABASE_ANTIGO

    infra = dict(INFRA)
    for sol in sorted(ger().solucoes.values(), key=lambda s: s.nome.lower()):
        if sol.supabase:
            infra[ALVO_SUPABASE + sol.id] = f"Supabase de {sol.nome}"
    if SUPABASE_ANTIGO in atual["conteineres"]:
        infra[SUPABASE_ANTIGO] = "Supabase fixo antigo (log20-supabase, substituído)"
    for nome, rotulo in infra.items():
        if nome == "log20-sb-studio" and nome not in atual["conteineres"]:
            continue
        c = atual["conteineres"].get(nome) or {}
        pico = picos.get(nome) or {}
        item = {"id": nome, "nome": rotulo, "tipo": "infra", "rodando": nome in atual["conteineres"],
                "cpu": c.get("cpu"), "mem": c.get("mem"), "mem_limite_docker": c.get("mem_limite"),
                "pico_mem_24h": pico.get("mem"), "pico_cpu_24h": pico.get("cpu")}
        if nome.startswith(ALVO_SUPABASE):
            sid = nome[len(ALVO_SUPABASE):]
            item["disco"] = {"volumes": sum(tam["volumes"].get(v, 0) for v in sb.volumes(sid)), "banco": 0, "imagem": 0}
        alvos.append(item)
    return {
        "ts": atual["ts"], "maquina": atual["maquina"], "alvos": alvos, "historico": hist, "tamanhos": tam,
        "padroes": {"memoria_mb": CFG.limite_memoria_mb, "cpus": CFG.limite_cpus, "processos": CFG.limite_processos},
        "coleta": {"intervalo_s": CFG.uso_intervalo_s, "dias": CFG.uso_dias, "erro": USO.erro},
    }


@app.put("/api/solucoes/{sid}/recursos")
def api_recursos(sid: str, memoria: str | int | None = Body(None, embed=True), cpus: str | float | None = Body(None, embed=True),
                 autor: Usuario = Depends(exigir_admin)) -> dict:
    """memoria: '768m', '1.5g', 0 (sem limite) ou null (volta ao padrao); cpus: 0.5, 2, 0 ou null."""
    sol = _solucao(sid)
    try:
        mem = None if memoria in (None, "") else memoria_mb(memoria)
        cp = None if cpus in (None, "") else cpus_validas(cpus)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    maquina_mb = (USO.atual()["maquina"].get("mem_total") or 0) // (1024 * 1024)
    if mem and maquina_mb and mem > maquina_mb:
        raise HTTPException(400, f"A VM tem {maquina_mb} MB de memoria. O limite nao pode passar disso.")
    nucleos = os.cpu_count() or 1
    if cp and cp > nucleos:
        raise HTTPException(400, f"A VM tem {nucleos} nucleo(s) de CPU. O limite nao pode passar disso.")
    ger().armazenamento.salvar_recursos(sid, mem, cp)
    aviso = ger().aplicar_limites(sol)
    lim = ger().limites(sol)
    AUTH.registrar(autor, "Mudou limites", f"{sid}: memoria {lim['memoria_mb'] or 'sem limite'} MB, CPU {lim['cpus'] or 'sem limite'}")
    return {"ok": True, "aviso": aviso, "limites": lim}


@app.post("/api/uso/limpar-docker")
def api_limpar_docker(autor: Usuario = Depends(exigir_admin)) -> dict:
    r = USO.limpar_docker()
    AUTH.registrar(autor, "Limpou o cache do Docker", f"{formatar_tamanho(r['liberado'])} liberados")
    return {**r, "liberado_texto": formatar_tamanho(r["liberado"])}
