"""Cria a estrutura do banco do Supabase de uma solucao pelos arquivos .sql do repositorio dela.

Para sistemas que nunca tiveram um Supabase de origem para copiar (projeto novo). Tela: Gerenciar >
Visao geral > "Criar a estrutura pelos arquivos .sql". So com o banco vazio (nenhuma tabela no
esquema public). Antes, um backup isolado da solucao (como na importacao, com o "Desfazer a ultima
importacao"). Cada arquivo roda numa transacao, na ordem escolhida, como o usuario postgres; no
primeiro erro, para e mostra o arquivo e a mensagem (os anteriores ficam aplicados).
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Callable

from . import supabase as sb
from .supabase_copia import Copia, ErroCopia, Origem, _Banco, arquivo_importacao

# Pastas procuradas, a partir da raiz do repositorio e da subpasta da solucao.
PASTAS = ("supabase/migrations", "migrations", "sql", "database", "db", "supabase")
MAX_ARQUIVOS = 1000
MAX_BYTES = 20 * 1024 * 1024  # por arquivo

_execucoes: dict[str, Copia] = {}
_trava = threading.Lock()


def _chave_ordem(rel: str) -> tuple:
    """schema* primeiro, seed* por ultimo, o resto pelo nome com os numeros em ordem (2 antes de 10)."""
    nome = rel.rsplit("/", 1)[-1].lower()
    grupo = 0 if nome.startswith("schema") else 2 if nome.startswith(("seed", "dados_iniciais")) else 1
    partes = [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", rel.lower())]
    return (grupo, partes)


def listar(raiz: Path, subpasta: str = ".") -> list[dict]:
    """Os .sql das pastas conhecidas (sem descer em subpastas), na ordem sugerida."""
    raiz = raiz.resolve()
    vistos: dict[str, dict] = {}
    bases = [raiz] + ([(raiz / subpasta).resolve()] if subpasta not in ("", ".") else [])
    for base in bases:
        if base != raiz and raiz not in base.parents:
            continue
        for pasta in PASTAS:
            d = base / pasta
            if not d.is_dir() or d.is_symlink():
                continue
            for arq in d.glob("*.sql"):
                if not arq.is_file() or arq.is_symlink():
                    continue
                rel = arq.resolve().relative_to(raiz).as_posix()
                vistos.setdefault(rel, {"caminho": rel, "bytes": arq.stat().st_size})
    lista = sorted(vistos.values(), key=lambda a: _chave_ordem(a["caminho"]))
    return lista[:MAX_ARQUIVOS]


def situacao(sid: str) -> dict | None:
    c = _execucoes.get(sid)
    return c.como_dict() if c else None


def _banco(pasta_dados: Path, sid: str) -> _Banco:
    seg = sb.segredos(pasta_dados, sid, criar=False)
    if seg is None or not sb.gateway_no_ar(sid):
        raise ErroCopia("O Supabase deste sistema não está no ar. Inicie a solução e tente de novo.")
    return _Banco(sid, seg.senha_banco, Origem("", 5432, "", "", "postgres"))


def tabelas_public(pasta_dados: Path, sid: str) -> int:
    linhas = _banco(pasta_dados, sid).local_csv("select count(*) from pg_tables where schemaname = 'public'")
    return int(linhas[0][0])


def iniciar(sid: str, raiz: Path, escolhidos: list[str], pasta_dados: Path,
            seguranca: Callable[[Callable[[str], None]], str]) -> Copia:
    disponiveis = {a["caminho"]: a for a in listar(raiz)}
    if not escolhidos:
        raise ErroCopia("Escolha ao menos um arquivo .sql.")
    for rel in escolhidos:
        if rel not in disponiveis:
            raise ErroCopia(f"Arquivo fora da lista do repositório: {rel}")
        if disponiveis[rel]["bytes"] > MAX_BYTES:
            raise ErroCopia(f"{rel} tem mais de {MAX_BYTES // 1024 // 1024} MB.")
    with _trava:
        atual = _execucoes.get(sid)
        if atual and atual.estado == "executando":
            raise ErroCopia("Já existe uma criação de estrutura em andamento para este sistema.")
        c = Copia(sid)
        _execucoes[sid] = c
    threading.Thread(target=_rodar, args=(c, raiz, list(escolhidos), pasta_dados, seguranca),
                     daemon=True, name=f"estrutura-supabase-{sid}").start()
    return c


def _rodar(c: Copia, raiz: Path, escolhidos: list[str], pasta_dados: Path,
           seguranca: Callable[[Callable[[str], None]], str]) -> None:
    try:
        c.etapa = "Conferindo o banco"
        n = tabelas_public(pasta_dados, c.sid)
        if n:
            raise ErroCopia(f"O banco deste sistema já tem {n} tabela(s) no esquema public. "
                            "A criação pelos .sql só roda num banco vazio.")
        c.etapa = "Backup do Supabase local antes (segurança)"
        try:
            nome_bkp = seguranca(lambda l: c.registrar("  " + l))
        except Exception as exc:  # noqa: BLE001
            raise ErroCopia(f"O backup de segurança falhou, nada foi alterado: {exc}") from exc
        c.resumo["seguranca"] = nome_bkp
        c.registrar(f"Backup de segurança: {nome_bkp} (tela Backups).")
        banco = _banco(pasta_dados, c.sid)
        sb._gravar_json_privado(arquivo_importacao(pasta_dados, c.sid), {
            "backup": nome_bkp, "em": time.strftime("%Y-%m-%d %H:%M:%S"),
            "origem": "arquivos .sql do repositório", "partes": ["estrutura"]})
        aplicados = 0
        for i, rel in enumerate(escolhidos, 1):
            c.etapa = f"Aplicando {i} de {len(escolhidos)}: {rel}"
            sql = (raiz / rel).read_text(encoding="utf-8-sig", errors="replace")
            try:
                banco.local_sql(sql, usuario="postgres", transacao=True)
            except ErroCopia as exc:
                c.resumo["aplicados"] = aplicados
                c.resumo["falhou_em"] = rel
                raise ErroCopia(f"{rel}: {exc}. Os {aplicados} arquivo(s) anteriores ficaram aplicados; "
                                "este foi desfeito inteiro.") from exc
            aplicados += 1
            c.registrar(f"ok  {rel}")
        c.resumo["aplicados"] = aplicados
        c.etapa = "Atualizando as estatísticas do banco (ANALYZE)"
        try:
            banco.local_sql("analyze;", usuario="postgres", transacao=False)
            c.registrar("Estatísticas do banco atualizadas (ANALYZE).")
        except ErroCopia as exc:
            c.registrar(f"Aviso: ANALYZE não foi possível ({exc}). Rode \"analyze;\" no SQL Editor do Studio.")
        c.resumo["tabelas"] = tabelas_public(pasta_dados, c.sid)
        c.estado = "concluida"
        c.registrar(f"Concluído: {aplicados} arquivo(s), {c.resumo['tabelas']} tabela(s) no esquema public.")
    except Exception as exc:  # noqa: BLE001 - qualquer falha vira mensagem para a tela
        c.estado, c.erro = "falhou", str(exc)
        c.registrar(f"ERRO: {exc}")
    finally:
        c.etapa = ""
        c.fim = time.time()

