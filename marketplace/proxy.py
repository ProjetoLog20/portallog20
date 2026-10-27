"""Configuracao do Caddy gerada pelo portal a partir dos manifestos.

Cada solucao do catalogo vira um bloco `:<porta_host>` que pergunta ao portal se a pessoa
esta logada e tem acesso (forward auth) e encaminha para `<container>:<porta_container>`
na rede interna. O arquivo e escrito em `dados/caddy/Caddyfile`, pasta que o container do
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
    '<a href="http://{http.request.host}:PORTA_PORTAL/" style="display:inline-block;margin-top:16px;padding:10px 16px;'
    'border-radius:6px;background:#003a4d;color:#fff;text-decoration:none;font-weight:600">Ir para o Portal de Soluções</a>'
    "</main></body></html>"
)


@dataclass
class Resultado:
    aplicado: bool
    mensagem: str
    erro: bool = False


def gerar(solucoes: list[Solucao], portal_upstream: str) -> str:
    """Monta o Caddyfile. A saida e deterministica (mesma entrada, mesmo texto)."""
    porta_portal = portal_upstream.rsplit(":", 1)[-1] if ":" in portal_upstream else "8000"
    pagina = _PAGINA_FORA_DO_AR.replace("PORTA_PORTAL", porta_portal)
    linhas = [
        "# GERADO PELO PORTAL DE SOLUCOES - nao edite: as mudancas sao sobrescritas.",
        "# Para publicar uma solucao, crie o manifesto em solucoes/ e use 'Recarregar catalogo'.",
        "",
        "(solucao) {",
        "\t# Antes de cada pedido, pergunta ao portal se a pessoa esta logada e tem acesso.",
        f"\tforward_auth {portal_upstream} {{",
        "\t\turi /api/auth/verificar?solucao={args[0]}",
        "\t\tcopy_headers X-Portal-Usuario",
        "\t}",
        "\treverse_proxy {args[1]}",
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
    for sol in sorted(solucoes, key=lambda s: s.porta_host):
        linhas += [
            "",
            f"# {_comentario(sol.nome)}",
            f":{sol.porta_host} {{",
            f"\timport solucao {sol.id} {sol.container}:{sol.porta_container}",
            "}",
        ]
    return "\n".join(linhas) + "\n"


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
            return Resultado(False, f"Configuracao nova do Caddy invalida; a anterior continua em uso. {_resumo(saida)}",
                             erro=True)

    novo.replace(atual)

    if not caddy_no_ar:
        return Resultado(True, "Configuracao do Caddy gravada; vale quando o Caddy iniciar.")
    ok, saida = docker_cli.executar_em(
        container, ["caddy", "reload", "--config", f"{PASTA_NO_CADDY}/{ARQUIVO}", "--adapter", "caddyfile"]
    )
    if not ok:
        return Resultado(True, f"Configuracao gravada, mas o Caddy nao recarregou: {_resumo(saida)}", erro=True)
    return Resultado(True, "Caddy atualizado com o catalogo atual.")


def _rodando(container: str) -> bool:
    info = docker_cli.container_inspect(container)
    return bool(info and info.get("State", {}).get("Running"))


def _resumo(saida: str) -> str:
    linhas = [l for l in saida.splitlines() if l.strip()]
    erros = [l for l in linhas if "error" in l.lower()] or linhas
    return (erros[-1] if erros else "sem detalhes")[:400]
