"""Linha de comando do portal.

    python -m marketplace                 inicia o portal (http://localhost:8000)
    python -m marketplace criar-admin     cria um administrador
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
    args = parser.parse_args()
    sys.exit(criar_admin(args) if args.comando == "criar-admin" else servir(args))


if __name__ == "__main__":
    main()
