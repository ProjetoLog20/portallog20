"""Linha de comando do portal.

    python -m marketplace                 inicia o portal (http://localhost:8000)
    python -m marketplace criar-admin     cria um administrador
    python -m marketplace validar         confere os manifestos em solucoes/ sem subir nada
    python -m marketplace sincronizar     instala/inicia as solucoes do catalogo (ver --help)
    python -m marketplace backup          faz um backup agora (bancos, volumes, portal)
    python -m marketplace backups         lista os backups
    python -m marketplace verificar-backup <nome>   restauracao de teste, sem tocar no que esta em uso
    python -m marketplace restaurar <nome> ...      restaura (ver --help)
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
    solucoes, erros = carregar_catalogo(cfg.pasta_solucoes, cfg.pasta_solucoes_locais)
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
        origem = (f"{sol.repositorio} ({sol.ref})" if sol.repositorio else f"gerenciado pelo servidor: {sol.externo}" if sol.externo else str(sol.contexto_build))
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


def _backups():
    """Servico de backup fora do portal web (terminal / deploy/atualizar.sh)."""
    from .backup import Backups
    from .catalog import carregar_catalogo
    from .manager import Armazenamento, Gerenciador

    cfg = config_mod.carregar()
    estado: dict = {}

    def ger() -> Gerenciador:
        if "g" not in estado:
            solucoes, _ = carregar_catalogo(cfg.pasta_solucoes, cfg.pasta_solucoes_locais)
            estado["g"] = Gerenciador(solucoes, Armazenamento(cfg.pasta_dados), cfg)
        return estado["g"]

    def auditoria(acao: str, detalhe: str) -> None:
        _auth().registrar(None, acao, f"{detalhe} (pelo terminal)")

    servico = Backups(cfg, ger, auditoria)
    servico.recarregar = lambda: estado.pop("g", None)  # type: ignore[attr-defined]
    return servico


def cmd_backup(args: argparse.Namespace) -> int:
    from .backup import ErroBackup

    try:
        man = _backups().fazer(args.motivo, print, somente=args.solucoes or None)
    except ErroBackup as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    return 0 if man["ok"] else 2


def cmd_backups(_: argparse.Namespace) -> int:
    from .backup import formatar_tamanho

    servico = _backups()
    lista = servico.listar()
    print(f"Pasta: {servico.pasta}")
    if not lista:
        print("Nenhum backup ainda.")
    for m in lista:
        marca = "ok  " if m.get("ok") else "ERRO"
        fix = " [fixado]" if m.get("fixado") else ""
        print(f"  {marca} {m['nome']:<40} {formatar_tamanho(m.get('bytes', 0)):>9}  {len(m.get('itens', []))} arquivo(s){fix}")
    return 0


def cmd_verificar(args: argparse.Namespace) -> int:
    from .backup import ErroBackup

    try:
        r = _backups().verificar(args.nome, print)
    except ErroBackup as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    return 0 if r["ok"] else 2


def cmd_restaurar(args: argparse.Namespace) -> int:
    from .backup import ErroBackup

    servico = _backups()
    try:
        _, man = servico.obter(args.nome)
    except ErroBackup as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    if args.tudo:
        alvos = sorted({i["solucao"] for i in man["itens"] if i.get("solucao") and i["tipo"] in ("banco", "volume")})
        portal = True
    else:
        alvos = args.solucao or []
        portal = args.portal
    if not portal and not alvos:
        print("Diga o que restaurar: --portal, --solucao <id> (pode repetir) ou --tudo.", file=sys.stderr)
        return 1
    partes = []
    if portal:
        partes.append("os usuarios, acessos, configuracoes e segredos do PORTAL")
    for sid in alvos:
        o_que = [p for p, ok in (("banco", not args.sem_banco), ("arquivos", not args.sem_arquivos)) if ok]
        partes.append(f"{sid} ({' e '.join(o_que)})")
    print(f"Backup {args.nome} ({man.get('criado_em', '')})")
    print("Vai voltar ao estado do backup: " + "; ".join(partes) + ".")
    print("Os dados atuais disso serao SUBSTITUIDOS (das solucoes, antes e feito um backup de seguranca).")
    if not args.sim and input("Digite RESTAURAR para continuar: ").strip() != "RESTAURAR":
        print("Nada foi feito.")
        return 1
    try:
        if portal:
            servico.restaurar_portal(args.nome, print)
            servico.recarregar()
        if args.tudo and servico.cfg.postgres_container:
            print("\n==> Contas e bancos avulsos do PostgreSQL")
            servico.restaurar_servidor(args.nome, print)
        for sid in alvos:
            print(f"\n==> {sid}")
            servico.restaurar_solucao(args.nome, sid, banco=not args.sem_banco, arquivos=not args.sem_arquivos,
                                      registrar=print, seguranca=not args.tudo)
    except ErroBackup as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    if args.tudo:
        print("\nPronto. Agora: docker compose restart portal  e  bash deploy/atualizar.sh")
    return 0


def cmd_supabase(args: argparse.Namespace) -> int:
    from . import supabase

    env = config_mod.RAIZ / ".env"
    if args.acao == "preparar":
        mudou = supabase.preparar(env, int(os.environ.get("PORTAL_SUPABASE_PORTA_API", "") or 8111))
        print("Supabase: .env " + (f"atualizado ({', '.join(mudou)})" if mudou else "ja estava pronto"))
    problemas = supabase.conferir_chaves(env)
    for p in problemas:
        print(f"ERRO: {p}", file=sys.stderr)
    return 1 if problemas else 0


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
    p_bkp = sub.add_parser("backup", help="Faz um backup agora (bancos, volumes e dados do portal)")
    p_bkp.add_argument("--motivo", default="manual", choices=["manual", "antes-de-atualizar", "agendado"])
    p_bkp.add_argument("solucoes", nargs="*", help="So estas solucoes (padrao: tudo, inclusive o portal)")
    sub.add_parser("backups", help="Lista os backups")
    p_ver = sub.add_parser("verificar-backup", help="Confere um backup com uma restauracao de teste")
    p_ver.add_argument("nome")
    p_res = sub.add_parser(
        "restaurar", help="Restaura um backup",
        description="Exemplos:\n  restaurar 2026-11-09_023000_agendado --solucao orcamento-obz\n"
                    "  restaurar 2026-11-09_023000_agendado --portal\n"
                    "  restaurar 2026-11-09_023000_agendado --tudo     (VM nova: portal + todas as solucoes)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_res.add_argument("nome", help="Nome do backup (veja: python -m marketplace backups)")
    p_res.add_argument("--solucao", action="append", help="Id da solucao (pode repetir)")
    p_res.add_argument("--portal", action="store_true", help="Usuarios, acessos, configuracoes e segredos")
    p_res.add_argument("--tudo", action="store_true", help="Portal e todas as solucoes do backup")
    p_res.add_argument("--sem-banco", action="store_true", help="Nao restaura o banco")
    p_res.add_argument("--sem-arquivos", action="store_true", help="Nao restaura os volumes")
    p_res.add_argument("--sim", action="store_true", help="Nao pede confirmacao")
    p_sup = sub.add_parser("supabase", help="Supabase do servidor (deploy/supabase)")
    p_sup.add_argument("acao", choices=["preparar", "conferir"],
                       help="preparar: gera os segredos que faltam no .env; conferir: so confere as chaves")
    args = parser.parse_args()
    comandos = {"supabase": cmd_supabase, "backup": cmd_backup, "backups": cmd_backups, "verificar-backup": cmd_verificar,
                "restaurar": cmd_restaurar}
    if args.comando in comandos:
        sys.exit(comandos[args.comando](args))
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
