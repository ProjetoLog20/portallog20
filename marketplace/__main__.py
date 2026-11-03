"""Linha de comando do portal.

    python -m marketplace                 inicia o portal (http://localhost:8000)
    python -m marketplace criar-admin     cria um administrador
    python -m marketplace validar         confere os manifestos em solucoes/ sem subir nada
    python -m marketplace sincronizar     instala/inicia as solucoes do catalogo (ver --help)
"""

from __future__ import annotations

import argparse
import copy
import getpass
import logging
import os
import sys

from . import config as config_mod
from .auth import Autenticacao, ErroAuth


def _auth() -> Autenticacao:
    cfg = config_mod.carregar()
    return Autenticacao(cfg.pasta_dados / "portal.db", sessao_horas=cfg.sessao_horas)


def criar_admin(args: argparse.Namespace) -> int:
    auth = _auth()
    usuario = args.usuario or input("Usuario do administrador: ").strip()
    nome = args.nome or input("Nome: ").strip() or usuario
    senha = os.environ.get("PORTAL_ADMIN_SENHA") or getpass.getpass("Senha (minimo 8 caracteres): ")
    if not os.environ.get("PORTAL_ADMIN_SENHA") and getpass.getpass("Repita a senha: ") != senha:
        print("As senhas nao conferem.", file=sys.stderr)
        return 1
    try:
        u, _ = auth.criar_usuario(usuario, nome, "admin", senha=senha, trocar_senha=False)
    except ErroAuth as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Administrador '{u.usuario}' criado. Entre no portal com esse usuario.")
    return 0


FAIXA_PADRAO = "8101-8130"  # a mesma do docker-compose.yml


def validar(_: argparse.Namespace) -> int:
    """Confere os manifestos: erros impedem a publicacao; avisos sao lembretes."""
    from pathlib import Path

    from .catalog import carregar_catalogo

    cfg = config_mod.carregar()
    faixa = cfg.faixa_portas or config_mod._faixa(FAIXA_PADRAO)
    solucoes, erros = carregar_catalogo(cfg.pasta_solucoes)
    avisos: list[str] = []
    for sol in solucoes.values():
        texto = Path(sol.manifesto).read_text(encoding="utf-8")
        if not faixa[0] <= sol.porta_host <= faixa[1]:
            erros.append(f"{sol.manifesto}: porta_host {sol.porta_host} fora da faixa {faixa[0]}-{faixa[1]}")
        if "<<TROCAR>>" in texto:
            avisos.append(f"{sol.manifesto}: ainda ha marcas <<TROCAR>> (confira se trocou os valores)")
        if sol.repositorio and "NOME-DO-REPOSITORIO" in sol.repositorio:
            erros.append(f"{sol.manifesto}: troque o endereco do repositorio do modelo")
        if sol.id == "nova-solucao":
            avisos.append(f"{sol.manifesto}: o id ainda e o do modelo ('nova-solucao')")
        if sol.caminho_saude == "/":
            avisos.append(f"{sol.manifesto}: sem caminho_saude especifico; o portal vai testar '/'")
    for msg in erros:
        print(f"ERRO   {msg}")
    for msg in avisos:
        print(f"AVISO  {msg}")
    print(f"\n{len(solucoes)} solucao(oes) lida(s), {len(erros)} erro(s), {len(avisos)} aviso(s).")
    for sol in sorted(solucoes.values(), key=lambda s: s.porta_host):
        origem = f"{sol.repositorio} ({sol.ref})" if sol.repositorio else str(sol.contexto_build)
        print(f"  {sol.porta_host}  {sol.id:<34} {origem}")
    return 1 if erros else 0


class SemConsultasRotineiras(logging.Filter):
    """Esconde do terminal as consultas GET bem-sucedidas da API (a pagina se atualiza sozinha).

    Erros e acoes (login, iniciar, parar...) continuam aparecendo.
    Defina PORTAL_LOG_COMPLETO=1 para ver tudo.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _, metodo, caminho, _, status = record.args
        except (TypeError, ValueError):
            return True
        return not (metodo == "GET" and str(caminho).startswith("/api/") and int(status) < 400)


def _config_log() -> dict:
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    if os.environ.get("PORTAL_LOG_COMPLETO") not in ("1", "true"):
        config.setdefault("filters", {})["sem_rotina"] = {"()": "marketplace.__main__.SemConsultasRotineiras"}
        config["handlers"]["access"]["filters"] = ["sem_rotina"]
    return config


def servir(args: argparse.Namespace) -> int:
    import uvicorn

    # Endereco do proprio portal, usado para mandar ao login quem tenta abrir uma solucao protegida.
    os.environ.setdefault("PORTAL_URL_PUBLICA", f"http://localhost:{args.porta}")
    if _auth().total_admins_ativos() == 0:
        print("\n  Nenhum administrador cadastrado. Em outro terminal, rode:\n"
              "      python -m marketplace criar-admin\n")
    print(f"Portal de Solucoes LOG20 em http://localhost:{args.porta}")
    uvicorn.run(
        "marketplace.main:app",
        host=args.host,
        port=args.porta,
        reload=args.recarregar,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("PORTAL_PROXIES", "127.0.0.1"),
        log_config=_config_log(),
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m marketplace", description="Portal de Solucoes LOG20")
    parser.add_argument("--host", default=os.environ.get("PORTAL_HOST", "127.0.0.1"),
                        help="Use 0.0.0.0 para aceitar acessos de outras maquinas.")
    parser.add_argument("--porta", type=int, default=int(os.environ.get("PORTAL_PORTA", "8000")))
    parser.add_argument("--recarregar", action="store_true", help="Reinicia ao editar o codigo (desenvolvimento).")
    sub = parser.add_subparsers(dest="comando")
    p_admin = sub.add_parser("criar-admin", help="Cria um usuario administrador")
    p_admin.add_argument("--usuario")
    p_admin.add_argument("--nome")
    sub.add_parser("validar", help="Confere os manifestos das solucoes")
    p_sinc = sub.add_parser(
        "sincronizar",
        help="Instala e inicia as solucoes do catalogo (as paradas ficam paradas)",
        description="Instala o que falta, reinicia o que esta com problema e regera o Caddy.",
    )
    p_sinc.add_argument("--atualizar", action="store_true",
                        help="Reconstroi as solucoes com o ultimo commit do branch do manifesto")
    p_sinc.add_argument("--reiniciar", action="store_true",
                        help="Reinicia tambem as que estao no ar (ex.: depois de mudar o IP)")
    p_sinc.add_argument("--incluir-paradas", action="store_true", help="Inicia tambem as paradas")
    p_sinc.add_argument("solucoes", nargs="*", help="Ids das solucoes (padrao: todas)")
    args = parser.parse_args()
    if args.comando == "sincronizar":
        from .sincronizar import executar

        sys.exit(executar(args.atualizar, args.reiniciar, args.incluir_paradas, args.solucoes))
    if args.comando == "criar-admin":
        sys.exit(criar_admin(args))
    if args.comando == "validar":
        sys.exit(validar(args))
    sys.exit(servir(args))


if __name__ == "__main__":
    main()
