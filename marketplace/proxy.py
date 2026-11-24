"""Configuracao do Caddy gerada pelo portal a partir dos manifestos.

Cada solucao do catalogo vira um bloco `:<porta_host>` que pergunta ao portal se a pessoa
esta logada e tem acesso (forward auth) e encaminha para `<container>:<porta_container>`
na rede interna. Com HTTPS (certificado.py), os blocos viram `https://<nome>:<porta_host>` com o
certificado proprio, o portal ganha `https://<nome>` (443) e a porta 80 leva para o HTTPS.
O arquivo e escrito em `dados/caddy/Caddyfile`, pasta que o container do
Caddy le. Antes de trocar o arquivo em uso, o portal valida a versao nova com o proprio
Caddy (`docker exec ... caddy validate`); se houver erro, a configuracao anterior continua.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import docker_cli
from .catalog import Solucao

ARQUIVO = "Caddyfile"
ARQUIVO_NOVO = "Caddyfile.novo"
PASTA_NO_CADDY = "/etc/caddy"

# Pagina mostrada quando a solucao esta parada ou ainda nao foi instalada. Estilos inline,
# sem chaves, para nao serem confundidos com placeholders do Caddy.
_PAGINA_FORA_DO_AR = (
    '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sistema fora do ar</title></head>'
    "<body style=\"margin:0;min-height:100vh;display:grid;place-items:center;background:#eef2f3;color:#12262e;"
    "font:16px/1.55 'Segoe UI',system-ui,sans-serif\">"
    '<main style="max-width:420px;margin:24px;padding:28px;background:#fff;border:1px solid #d5dee1;'
    'border-top:5px solid #003a4d;border-radius:10px">'
    '<h1 style="margin:0 0 8px;font-size:22px">Sistema fora do ar</h1>'
    "<p>Este sistema não está disponível agora. Tente de novo em alguns minutos ou avise o administrador do portal.</p>"
    '<a href="ENDERECO_PORTAL" style="display:inline-block;margin-top:16px;padding:10px 16px;'
    'border-radius:6px;background:#003a4d;color:#fff;text-decoration:none;font-weight:600">Ir para o Portal de Soluções</a>'
    "</main></body></html>"
)


@dataclass
class Resultado:
    aplicado: bool
    mensagem: str
    erro: bool = False


def filtro_cookie(nome_cookie: str) -> str:
    """Expressao que tira um cookie do cabecalho Cookie sem mexer nos outros.

    "a=1; portal_sessao=X; b=2" -> "a=1; b=2". Sem barras invertidas, para nao depender
    das regras de escape do Caddyfile.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", nome_cookie):
        raise ValueError(f"Nome de cookie invalido: {nome_cookie}")
    return f"(^|; *){nome_cookie}=[^;]*;? *"


def gerar(solucoes: list[Solucao], portal_upstream: str, cookie_portal: str = "portal_sessao",
          extras: list[str] | None = None, https: dict | None = None) -> str:
    """Monta o Caddyfile. A saida e deterministica (mesma entrada, mesmo texto).

    `https` (de certificado.para_caddy): {"nome", "certificado", "chave", "impressao"}; None = HTTP."""
    porta_portal = portal_upstream.rsplit(":", 1)[-1] if ":" in portal_upstream else "8000"
    if https:
        nome = https["nome"]
        pagina = _PAGINA_FORA_DO_AR.replace("ENDERECO_PORTAL", f"https://{nome}/")
    else:
        pagina = _PAGINA_FORA_DO_AR.replace("ENDERECO_PORTAL", "http://{http.request.host}:" + porta_portal + "/")
    ordenadas = sorted(solucoes, key=lambda s: s.porta_host)
    linhas = [
        "# GERADO PELO PORTAL DE SOLUCOES - nao edite: as mudancas sao sobrescritas.",
        "# Para publicar uma solucao, crie o manifesto em solucoes/ e use 'Recarregar catalogo'.",
        "",
    ]
    if https:
        linhas += _inicio_https(https, ordenadas, portal_upstream)
    linhas += [
        "(solucao) {",
        "\t# Antes de cada pedido, pergunta ao portal se a pessoa esta logada e tem acesso.",
        f"\tforward_auth {portal_upstream} {{",
        "\t\turi /api/auth/verificar?solucao={args[0]}",
        "\t\tcopy_headers X-Portal-Usuario",
        "\t}",
        "\treverse_proxy {args[1]} {",
        "\t\t# A sessao do portal nao vai para as solucoes: todas ficam no mesmo endereco",
        "\t\t# (muda so a porta) e o navegador envia o cookie para todas. Sem isto, uma",
        "\t\t# solucao poderia usar a sessao de quem a abriu para agir no portal.",
        f'\t\theader_up Cookie "{filtro_cookie(cookie_portal)}" "$1"',
        "\t}",
        "",
        "\thandle_errors {",
        '\t\theader Content-Type "text/html; charset=utf-8"',
        '\t\theader Cache-Control "no-store"',
        "\t\trespond <<HTML",
        f"\t\t\t{pagina}",
        "\t\t\tHTML 503",
        "\t}",
        "}",
    ]
    for sol in ordenadas:
        upstream = sol.externo or f"{sol.container}:{sol.porta_container}"
        endereco = f"https://{https['nome']}:{sol.porta_host}" if https else f":{sol.porta_host}"
        linhas += ["", f"# {_comentario(sol.nome)}", f"{endereco} {{"]
        if https:
            linhas.append("\timport certificado")
        if not sol.caminhos_publicos:
            linhas.append(f"\timport solucao {sol.id} {upstream}")
            if sol.supabase:
                linhas += _bloco_supabase(sol)
        else:
            linhas += _bloco_com_publicos(sol, upstream, portal_upstream, cookie_portal, pagina)
        linhas.append("}")
    for bloco in extras or []:
        linhas += ["", bloco.rstrip("\n")]
    return "\n".join(linhas) + "\n"


def _inicio_https(https: dict, solucoes: list[Solucao], portal_upstream: str) -> list[str]:
    """Opcoes globais, o certificado e os sites do portal (443) e do redirecionamento (80)."""
    nome = https["nome"]
    portas = [":443"] + [f":{s.porta_host}" for s in solucoes]
    linhas = [
        f"# HTTPS com certificado proprio para {nome} (dados/certificados; impressao {https['impressao']}).",
        "{",
        "\t# O certificado vem dos arquivos: nada de certificado automatico. O redirecionamento da",
        "\t# porta 80 e o de cada porta (http:// na porta do sistema) estao definidos abaixo.",
        "\tauto_https disable_redirects",
    ]
    for porta in portas:
        linhas += [
            f"\tservers {porta} {{",
            "\t\tlistener_wrappers {",
            "\t\t\thttp_redirect",
            "\t\t\ttls",
            "\t\t}",
            "\t}",
        ]
    linhas += [
        "}",
        "",
        "(certificado) {",
        f"\ttls {https['certificado']} {https['chave']}",
        "}",
        "",
        "# Portal de Solucoes",
        f"https://{nome} {{",
        "\timport certificado",
        f"\treverse_proxy {portal_upstream}",
        "}",
        "",
        "# http:// (porta 80) leva para o portal em https",
        "http:// {",
        f"\tredir https://{nome}{{uri}} permanent",
        "}",
        "",
    ]
    return linhas


def _bloco_supabase(sol: Solucao, nivel: int = 1) -> list[str]:
    """API do Supabase da solucao no mesmo endereco do sistema ([supabase] no manifesto).

    /rest/v1, /auth/v1 e /storage/v1 vao para o gateway da pilha dela (log20-sb-<id>-gw). O login do
    portal ja foi conferido antes (forward_auth): so quem pode abrir o sistema chega aos dados. Nada
    do portal segue para o Supabase (nem o cabecalho do usuario nem os cookies, que ele nao usa)."""
    from .supabase import CAMINHOS_API, gateway

    t = "\t" * nivel
    return [
        f"{t}# Supabase deste sistema (dados, login e arquivos), so para quem passou pelo login do portal.",
        f"{t}@supabase path {' '.join(CAMINHOS_API)}",
        f"{t}handle @supabase {{",
        f"{t}\treverse_proxy {gateway(sol.id)} {{",
        f"{t}\t\theader_up -X-Portal-Usuario",
        f"{t}\t\theader_up -Cookie",
        f"{t}\t}}",
        f"{t}}}",
    ]


def _bloco_com_publicos(sol: Solucao, upstream: str, portal_upstream: str, cookie: str,
                        pagina: str) -> list[str]:
    """Site em que alguns caminhos abrem sem login no portal (links publicos da solucao).

    Escrito por extenso, sem snippets aninhados. Nos caminhos publicos o cabecalho
    X-Portal-Usuario e removido: sem ele, ninguem consegue se passar por um usuario do portal.
    """
    filtro = f'\t\t\theader_up Cookie "{filtro_cookie(cookie)}" "$1"'
    return [
        "\t# Caminhos publicos (manifesto: caminhos_publicos): sem login no portal.",
        f"\t@publico path {' '.join(sol.caminhos_publicos)}",
        "\thandle @publico {",
        f"\t\treverse_proxy {upstream} {{",
        "\t\t\theader_up -X-Portal-Usuario",
        filtro,
        "\t\t}",
        "\t}",
        *(_bloco_supabase_com_login(sol, portal_upstream) if sol.supabase else []),
        "\thandle {",
        f"\t\tforward_auth {portal_upstream} {{",
        f"\t\t\turi /api/auth/verificar?solucao={sol.id}",
        "\t\t\tcopy_headers X-Portal-Usuario",
        "\t\t}",
        f"\t\treverse_proxy {upstream} {{",
        filtro,
        "\t\t}",
        "\t}",
        "\thandle_errors {",
        '\t\theader Content-Type "text/html; charset=utf-8"',
        '\t\theader Cache-Control "no-store"',
        "\t\trespond <<HTML",
        f"\t\t\t{pagina}",
        "\t\t\tHTML 503",
        "\t}",
    ]


def _bloco_supabase_com_login(sol: Solucao, portal_upstream: str) -> list[str]:
    """Versao para sites com caminhos publicos: o login do portal fica dentro do handle."""
    from .supabase import CAMINHOS_API, gateway

    return [
        "\t# Supabase deste sistema (dados, login e arquivos), so para quem passou pelo login do portal.",
        f"\t@supabase path {' '.join(CAMINHOS_API)}",
        "\thandle @supabase {",
        f"\t\tforward_auth {portal_upstream} {{",
        f"\t\t\turi /api/auth/verificar?solucao={sol.id}",
        "\t\t}",
        f"\t\treverse_proxy {gateway(sol.id)} {{",
        "\t\t\theader_up -X-Portal-Usuario",
        "\t\t\theader_up -Cookie",
        "\t\t}",
        "\t}",
    ]


def _comentario(texto: str) -> str:
    return re.sub(r"[\r\n]+", " ", texto)


def aplicar(texto: str, pasta: Path, container: str) -> Resultado:
    """Escreve, valida e recarrega. Sem container do Caddy, so escreve o arquivo."""
    pasta.mkdir(parents=True, exist_ok=True)
    atual = pasta / ARQUIVO
    if atual.exists() and atual.read_text(encoding="utf-8") == texto:
        return Resultado(False, "Configuracao do Caddy ja estava atualizada.")

    novo = pasta / ARQUIVO_NOVO
    novo.write_text(texto, encoding="utf-8")

    caddy_no_ar = bool(container) and _rodando(container)
    if caddy_no_ar:
        ok, saida = docker_cli.executar_em(
            container, ["caddy", "validate", "--config", f"{PASTA_NO_CADDY}/{ARQUIVO_NOVO}", "--adapter", "caddyfile"]
        )
        if not ok:
            novo.unlink(missing_ok=True)
            return Resultado(False, f"Configuracao nova do Caddy invalida. A anterior continua em uso. {_resumo(saida)}",
                             erro=True)

    novo.replace(atual)

    if not caddy_no_ar:
        return Resultado(True, "Configuracao do Caddy salva. Será aplicada quando o Caddy iniciar.")
    ok, saida = docker_cli.executar_em(
        container, ["caddy", "reload", "--config", f"{PASTA_NO_CADDY}/{ARQUIVO}", "--adapter", "caddyfile"]
    )
    if not ok:
        return Resultado(True, f"Configuracao salva, mas o Caddy nao recarregou: {_resumo(saida)}", erro=True)
    return Resultado(True, "Caddy atualizado com o catalogo atual.")


def _rodando(container: str) -> bool:
    info = docker_cli.container_inspect(container)
    return bool(info and info.get("State", {}).get("Running"))


def _resumo(saida: str) -> str:
    linhas = [l for l in saida.splitlines() if l.strip()]
    erros = [l for l in linhas if "error" in l.lower()] or linhas
    return (erros[-1] if erros else "sem detalhes")[:400]
