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
"""


class ErroAuth(ValueError):
    """Erro de validacao mostrado ao usuario."""


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

    def criar_usuario(
        self, usuario: str, nome: str, perfil: str, senha: str | None = None, trocar_senha: bool = True
    ) -> tuple[Usuario, str]:
        usuario = usuario.strip()
        nome = nome.strip()
        if not USUARIO_RE.match(usuario):
            raise ErroAuth("Usuario deve ter 3 a 60 caracteres: letras, numeros, ponto, hifen, _ ou @.")
        if not nome:
            raise ErroAuth("Informe o nome.")
        if perfil not in PERFIS:
            raise ErroAuth("Perfil invalido.")
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
                    raise ErroAuth("Voce nao pode remover o seu proprio acesso de administrador.")
                if self.total_admins_ativos(db) <= 1:
                    raise ErroAuth("E preciso manter pelo menos um administrador ativo.")
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

    def sessao(self, token: str | None) -> Usuario | None:
        if not token or len(token) > 200:
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT u.* FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id"
                " WHERE s.token_hash = ? AND s.expira_em > ? AND u.ativo = 1",
                (_hash_token(token), time.time()),
            ).fetchone()
        return _usuario(row) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self._db() as db:
                db.execute("DELETE FROM sessoes WHERE token_hash = ?", (_hash_token(token),))
