"""Usuarios, senhas, sessoes e permissoes do portal (SQLite da biblioteca padrao)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

PERFIS = {"admin", "usuario"}
USUARIO_RE = re.compile(r"^[a-zA-Z0-9._@-]{3,60}$")
SENHA_MIN = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  usuario TEXT NOT NULL UNIQUE COLLATE NOCASE,
  nome TEXT NOT NULL,
  senha_hash TEXT NOT NULL,
  perfil TEXT NOT NULL CHECK (perfil IN ('admin', 'usuario')),
  ativo INTEGER NOT NULL DEFAULT 1,
  trocar_senha INTEGER NOT NULL DEFAULT 1,
  criado_em REAL NOT NULL,
  ultimo_login REAL
);
CREATE TABLE IF NOT EXISTS sessoes (
  token_hash TEXT PRIMARY KEY,
  usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
  criada_em REAL NOT NULL,
  expira_em REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessoes_usuario ON sessoes(usuario_id);
CREATE TABLE IF NOT EXISTS acessos (
  usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
  solucao_id TEXT NOT NULL,
  PRIMARY KEY (usuario_id, solucao_id)
);
CREATE TABLE IF NOT EXISTS auditoria (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quando REAL NOT NULL,
  autor TEXT NOT NULL,
  acao TEXT NOT NULL,
  detalhe TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auditoria_quando ON auditoria(quando);
"""

LOTE_MAX = 500
# Como o perfil pode vir escrito numa planilha.
_PERFIL_TEXTO = {"": "usuario", "usuário": "usuario", "administrador": "admin", "administradora": "admin"}
AUDITORIA_MAX = 5000


class ErroAuth(ValueError):
    """Erro de validacao mostrado ao usuario."""


class ErroLote(ErroAuth):
    """Lote recusado: `problemas` traz {linha, usuario, erro} de cada linha com erro."""

    def __init__(self, problemas: list[dict]):
        super().__init__(f"{len(problemas)} linha(s) com problema; nenhum usuario foi criado.")
        self.problemas = problemas


# ---------------- senhas ----------------

def gerar_hash(senha: str) -> str:
    sal = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    dk = hashlib.scrypt(senha.encode("utf-8"), salt=sal, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${sal.hex()}${dk.hex()}"


def conferir_senha(senha: str, armazenado: str) -> bool:
    try:
        algoritmo, n, r, p, sal, esperado = armazenado.split("$")
        if algoritmo != "scrypt":
            return False
        dk = hashlib.scrypt(
            senha.encode("utf-8"), salt=bytes.fromhex(sal), n=int(n), r=int(r), p=int(p), dklen=32
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), esperado)


# Hash calculado uma vez para gastar o mesmo tempo quando o usuario nao existe.
_HASH_FALSO = gerar_hash(secrets.token_urlsafe(16))


def senha_temporaria() -> str:
    alfabeto = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(12))


def validar_senha(senha: str) -> None:
    if len(senha) < SENHA_MIN:
        raise ErroAuth(f"A senha precisa ter pelo menos {SENHA_MIN} caracteres.")
    if len(senha) > 200:
        raise ErroAuth("Senha longa demais.")


# ---------------- limite de tentativas ----------------

class LimiteTentativas:
    """Bloqueia por alguns minutos depois de muitas falhas de login (por IP e por usuario)."""

    def __init__(self, maximo: int = 8, janela_s: int = 15 * 60):
        self.maximo = maximo
        self.janela = janela_s
        self._falhas: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recentes(self, chave: str, agora: float) -> list[float]:
        lista = [t for t in self._falhas.get(chave, []) if agora - t < self.janela]
        self._falhas[chave] = lista
        return lista

    def bloqueado(self, *chaves: str) -> bool:
        agora = time.time()
        with self._lock:
            return any(len(self._recentes(c, agora)) >= self.maximo for c in chaves)

    def falhou(self, *chaves: str) -> None:
        agora = time.time()
        with self._lock:
            for c in chaves:
                self._recentes(c, agora).append(agora)

    def limpar(self, *chaves: str) -> None:
        with self._lock:
            for c in chaves:
                self._falhas.pop(c, None)


# ---------------- banco ----------------

@dataclass
class Usuario:
    id: int
    usuario: str
    nome: str
    perfil: str
    ativo: bool
    trocar_senha: bool
    ultimo_login: float | None = None
    criado_em: float | None = None

    @property
    def admin(self) -> bool:
        return self.perfil == "admin"


def _usuario(row: sqlite3.Row) -> Usuario:
    return Usuario(
        id=row["id"],
        usuario=row["usuario"],
        nome=row["nome"],
        perfil=row["perfil"],
        ativo=bool(row["ativo"]),
        trocar_senha=bool(row["trocar_senha"]),
        ultimo_login=row["ultimo_login"],
        criado_em=row["criado_em"],
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class Autenticacao:
    def __init__(self, arquivo: Path, sessao_horas: int = 12):
        self.arquivo = arquivo
        self.sessao_s = sessao_horas * 3600
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)
        self.limite = LimiteTentativas()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.arquivo, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # ----- usuarios -----
    def total_admins_ativos(self, db=None) -> int:
        if db is None:
            with self._db() as d:
                return self.total_admins_ativos(d)
        return db.execute("SELECT COUNT(*) FROM usuarios WHERE perfil='admin' AND ativo=1").fetchone()[0]

    @staticmethod
    def _problema_cadastro(usuario: str, nome: str, perfil: str) -> str | None:
        if not USUARIO_RE.match(usuario):
            return "Usuario deve ter 3 a 60 caracteres: letras, numeros, ponto, hifen, _ ou @."
        if not nome:
            return "Informe o nome."
        if len(nome) > 120:
            return "Nome longo demais (maximo 120 caracteres)."
        if perfil not in PERFIS:
            return "Perfil invalido (use usuario ou admin)."
        return None

    def criar_usuario(
        self, usuario: str, nome: str, perfil: str, senha: str | None = None, trocar_senha: bool = True
    ) -> tuple[Usuario, str]:
        usuario = usuario.strip()
        nome = nome.strip()
        problema = self._problema_cadastro(usuario, nome, perfil)
        if problema:
            raise ErroAuth(problema)
        senha = senha or senha_temporaria()
        validar_senha(senha)
        with self._db() as db:
            try:
                cur = db.execute(
                    "INSERT INTO usuarios (usuario, nome, senha_hash, perfil, trocar_senha, criado_em)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (usuario, nome, gerar_hash(senha), perfil, int(trocar_senha), time.time()),
                )
            except sqlite3.IntegrityError as exc:
                raise ErroAuth(f"O usuario '{usuario}' ja existe.") from exc
            row = db.execute("SELECT * FROM usuarios WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _usuario(row), senha

    def obter(self, uid: int) -> Usuario | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()
        return _usuario(row) if row else None

    def listar(self) -> list[dict]:
        with self._db() as db:
            usuarios = [_usuario(r) for r in db.execute("SELECT * FROM usuarios ORDER BY nome COLLATE NOCASE")]
            acessos: dict[int, list[str]] = {}
            for r in db.execute("SELECT usuario_id, solucao_id FROM acessos ORDER BY solucao_id"):
                acessos.setdefault(r["usuario_id"], []).append(r["solucao_id"])
        return [{**vars(u), "solucoes": acessos.get(u.id, [])} for u in usuarios]

    def atualizar(self, uid: int, *, nome: str | None = None, perfil: str | None = None,
                  ativo: bool | None = None, autor: Usuario | None = None) -> Usuario:
        with self._db() as db:
            row = db.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()
            if row is None:
                raise ErroAuth("Usuario nao encontrado.")
            atual = _usuario(row)
            novo_perfil = atual.perfil if perfil is None else perfil
            novo_ativo = atual.ativo if ativo is None else ativo
            if novo_perfil not in PERFIS:
                raise ErroAuth("Perfil invalido.")
            perde_admin = atual.admin and atual.ativo and (novo_perfil != "admin" or not novo_ativo)
            if perde_admin:
                if autor is not None and autor.id == uid:
                    raise ErroAuth("Você não pode remover o seu próprio acesso de administrador.")
                if self.total_admins_ativos(db) <= 1:
                    raise ErroAuth("É preciso manter pelo menos um administrador ativo.")
            novo_nome = atual.nome if nome is None else nome.strip()
            if not novo_nome:
                raise ErroAuth("Informe o nome.")
            db.execute(
                "UPDATE usuarios SET nome = ?, perfil = ?, ativo = ? WHERE id = ?",
                (novo_nome, novo_perfil, int(novo_ativo), uid),
            )
            if not novo_ativo:
                db.execute("DELETE FROM sessoes WHERE usuario_id = ?", (uid,))
            row = db.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()
        return _usuario(row)

    def definir_acessos(self, uid: int, solucoes: list[str]) -> None:
        with self._db() as db:
            db.execute("DELETE FROM acessos WHERE usuario_id = ?", (uid,))
            db.executemany(
                "INSERT INTO acessos (usuario_id, solucao_id) VALUES (?, ?)",
                [(uid, s) for s in sorted(set(solucoes))],
            )

    def criar_lote(self, itens: list[dict], solucoes: list[str]) -> list[tuple[Usuario, str]]:
        """Cria varios usuarios de uma vez, todos ou nenhum.

        `itens`: [{usuario, nome, perfil?}]. Se alguma linha tiver problema (formato, repetida
        na lista ou ja cadastrada), levanta ErroLote e nada e gravado.
        """
        if not itens:
            raise ErroAuth("A lista esta vazia.")
        if len(itens) > LOTE_MAX:
            raise ErroAuth(f"Envie no maximo {LOTE_MAX} usuarios por vez.")
        limpos = [{
            "usuario": str(i.get("usuario") or "").strip(),
            "nome": " ".join(str(i.get("nome") or "").split()),
            "perfil": _PERFIL_TEXTO.get(str(i.get("perfil") or "").strip().lower(),
                                        str(i.get("perfil") or "").strip().lower()),
        } for i in itens]
        problemas = self.validar_lote(limpos)
        if problemas:
            raise ErroLote(problemas)

        # O hash (scrypt) e o passo lento: feito antes de abrir a transacao.
        preparados = []
        for item in limpos:
            senha = senha_temporaria()
            preparados.append((item, senha, gerar_hash(senha)))
        criados: list[tuple[Usuario, str]] = []
        with self._db() as db:
            agora = time.time()
            for item, senha, hash_ in preparados:
                try:
                    cur = db.execute(
                        "INSERT INTO usuarios (usuario, nome, senha_hash, perfil, trocar_senha, criado_em)"
                        " VALUES (?, ?, ?, ?, 1, ?)",
                        (item["usuario"], item["nome"], hash_, item["perfil"], agora),
                    )
                except sqlite3.IntegrityError as exc:  # criado por outra pessoa no meio do caminho
                    db.rollback()
                    raise ErroAuth(f"O usuario '{item['usuario']}' ja existe. Nenhum usuario foi criado.") from exc
                if item["perfil"] != "admin":
                    db.executemany("INSERT INTO acessos (usuario_id, solucao_id) VALUES (?, ?)",
                                   [(cur.lastrowid, s) for s in sorted(set(solucoes))])
                row = db.execute("SELECT * FROM usuarios WHERE id = ?", (cur.lastrowid,)).fetchone()
                criados.append((_usuario(row), senha))
        return criados

    def validar_lote(self, itens: list[dict]) -> list[dict]:
        """Problemas de cada linha (numeradas a partir de 1), sem gravar nada."""
        with self._db() as db:
            existentes = {r[0].lower() for r in db.execute("SELECT usuario FROM usuarios")}
        vistos: dict[str, int] = {}
        problemas = []
        for n, item in enumerate(itens, start=1):
            usuario = item["usuario"]
            erro = self._problema_cadastro(usuario, item["nome"], item["perfil"])
            chave = usuario.lower()
            if not erro and chave in existentes:
                erro = "Ja existe um usuario com este login."
            elif not erro and chave in vistos:
                erro = f"Repetido na lista (linha {vistos[chave]})."
            vistos.setdefault(chave, n)
            if erro:
                problemas.append({"linha": n, "usuario": usuario, "erro": erro})
        return problemas

    def alterar_acessos(self, uids: list[int], solucoes: list[str], conceder: bool) -> int:
        """Concede ou retira sistemas de varios usuarios. Devolve quantas permissoes mudaram.

        Administradores ja veem tudo: nao recebem permissoes individuais.
        """
        if not uids or not solucoes:
            return 0
        with self._db() as db:
            marcas = ",".join("?" * len(uids))
            alvos = [r[0] for r in db.execute(
                f"SELECT id FROM usuarios WHERE id IN ({marcas}) AND perfil != 'admin'", list(uids))]
            pares = [(u, s) for u in alvos for s in sorted(set(solucoes))]
            antes = db.total_changes
            if conceder:
                db.executemany("INSERT OR IGNORE INTO acessos (usuario_id, solucao_id) VALUES (?, ?)", pares)
            else:
                db.executemany("DELETE FROM acessos WHERE usuario_id = ? AND solucao_id = ?", pares)
            return db.total_changes - antes

    def nomes(self, uids: list[int]) -> dict[int, str]:
        if not uids:
            return {}
        with self._db() as db:
            marcas = ",".join("?" * len(uids))
            return {r["id"]: r["usuario"] for r in db.execute(
                f"SELECT id, usuario FROM usuarios WHERE id IN ({marcas})", list(uids))}

    # ----- historico -----
    def registrar(self, autor: Usuario | None, acao: str, detalhe: str = "") -> None:
        with self._db() as db:
            db.execute("INSERT INTO auditoria (quando, autor, acao, detalhe) VALUES (?, ?, ?, ?)",
                       (time.time(), autor.usuario if autor else "sistema", acao, detalhe[:2000]))
            db.execute("DELETE FROM auditoria WHERE id <= (SELECT MAX(id) FROM auditoria) - ?", (AUDITORIA_MAX,))

    def historico(self, limite: int = 200) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT quando, autor, acao, detalhe FROM auditoria ORDER BY id DESC LIMIT ?",
                (max(1, min(limite, 1000)),))]

    def acessos(self, uid: int) -> set[str]:
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT solucao_id FROM acessos WHERE usuario_id = ?", (uid,))}

    def pode_acessar(self, usuario: Usuario, solucao_id: str) -> bool:
        return usuario.admin or solucao_id in self.acessos(usuario.id)

    def redefinir_senha(self, uid: int) -> str:
        senha = senha_temporaria()
        with self._db() as db:
            cur = db.execute(
                "UPDATE usuarios SET senha_hash = ?, trocar_senha = 1 WHERE id = ?", (gerar_hash(senha), uid)
            )
            if cur.rowcount == 0:
                raise ErroAuth("Usuario nao encontrado.")
            db.execute("DELETE FROM sessoes WHERE usuario_id = ?", (uid,))
        return senha

    def trocar_senha(self, uid: int, atual: str, nova: str, manter_token: str | None = None) -> None:
        validar_senha(nova)
        with self._db() as db:
            row = db.execute("SELECT senha_hash FROM usuarios WHERE id = ?", (uid,)).fetchone()
            if row is None or not conferir_senha(atual, row["senha_hash"]):
                raise ErroAuth("Senha atual incorreta.")
            if conferir_senha(nova, row["senha_hash"]):
                raise ErroAuth("A nova senha precisa ser diferente da atual.")
            db.execute(
                "UPDATE usuarios SET senha_hash = ?, trocar_senha = 0 WHERE id = ?", (gerar_hash(nova), uid)
            )
            # Encerra as outras sessoes; a atual continua.
            manter = _hash_token(manter_token) if manter_token else ""
            db.execute("DELETE FROM sessoes WHERE usuario_id = ? AND token_hash != ?", (uid, manter))

    # ----- sessoes -----
    def login(self, usuario: str, senha: str, ip: str) -> tuple[Usuario, str]:
        chave_ip, chave_user = f"ip:{ip}", f"u:{usuario.strip().lower()}"
        if self.limite.bloqueado(chave_ip, chave_user):
            raise ErroAuth("Muitas tentativas. Aguarde alguns minutos e tente de novo.")
        with self._db() as db:
            row = db.execute("SELECT * FROM usuarios WHERE usuario = ?", (usuario.strip(),)).fetchone()
            ok = conferir_senha(senha, row["senha_hash"] if row else _HASH_FALSO)
            if not row or not ok or not row["ativo"]:
                self.limite.falhou(chave_ip, chave_user)
                raise ErroAuth("Usuario ou senha incorretos.")
            token = secrets.token_urlsafe(32)
            agora = time.time()
            db.execute("DELETE FROM sessoes WHERE expira_em < ?", (agora,))
            db.execute(
                "INSERT INTO sessoes (token_hash, usuario_id, criada_em, expira_em) VALUES (?, ?, ?, ?)",
                (_hash_token(token), row["id"], agora, agora + self.sessao_s),
            )
            db.execute("UPDATE usuarios SET ultimo_login = ? WHERE id = ?", (agora, row["id"]))
        self.limite.limpar(chave_user)
        return _usuario(row), token

    # A sessao vence depois de `sessao_horas` SEM USO (cada uso empurra o prazo), e nunca passa
    # de SESSAO_MAX_S desde o login. A renovacao grava no maximo a cada RENOVAR_A_CADA_S.
    SESSAO_MAX_S = 30 * 24 * 3600
    RENOVAR_A_CADA_S = 600

    def sessao(self, token: str | None) -> Usuario | None:
        if not token or len(token) > 200:
            return None
        agora = time.time()
        th = _hash_token(token)
        with self._db() as db:
            row = db.execute(
                "SELECT u.*, s.criada_em AS s_criada, s.expira_em AS s_expira FROM sessoes s"
                " JOIN usuarios u ON u.id = s.usuario_id"
                " WHERE s.token_hash = ? AND s.expira_em > ? AND u.ativo = 1",
                (th, agora),
            ).fetchone()
            if row:
                novo = min(agora + self.sessao_s, row["s_criada"] + self.SESSAO_MAX_S)
                if novo - row["s_expira"] >= self.RENOVAR_A_CADA_S:
                    db.execute("UPDATE sessoes SET expira_em = ? WHERE token_hash = ?", (novo, th))
        return _usuario(row) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self._db() as db:
                db.execute("DELETE FROM sessoes WHERE token_hash = ?", (_hash_token(token),))
