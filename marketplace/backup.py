"""Backup e restauracao dos dados do servidor.

O que entra num backup (cada um e opcional nos ajustes):

- **bancos**: todos os bancos do PostgreSQL do servidor (`pg_dump -Fc`), inclusive os criados a
  mao no pgAdmin, e as contas do banco (`pg_dumpall --globals-only`);
- **volumes**: as pastas de dados das solucoes (`[execucao.volumes]` dos manifestos), em .tar.gz;
- **bancos externos**: o banco de uma solucao fora do servidor (ex.: Supabase), lido da
  configuracao indicada em `[backup] banco_externo` do manifesto;
- **portal**: usuarios, acessos e historico (portal.db), configuracoes e segredos das solucoes e as
  solucoes criadas pelo assistente.

Cada backup e uma pasta `AAAA-MM-DD_HHMMSS_<motivo>` com um `manifesto.json` (arquivos, tamanhos,
sha256, erros). A pasta so ganha o nome final quando termina: uma pasta `.parcial` nunca e usada.

O backup so le: nada aqui altera os bancos ou volumes em uso. Restaurar um banco grava primeiro
num banco temporario e so troca pelo atual se a restauracao inteira der certo; antes, o portal
faz um backup de seguranca da solucao.

Tudo roda pelo Docker (`docker exec` no PostgreSQL do servidor, `docker run` com a imagem do
PostgreSQL para os volumes): o portal nao precisa de cliente de banco instalado.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from . import banco as banco_pg
from . import docker_cli
from . import supabase as sb
from .config import Config
from .manager import Gerenciador, Tarefa

try:  # trava entre processos (agendador no portal x comando no terminal); nao existe no Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

VERSAO_FORMATO = 1
MOTIVOS = {"agendado", "manual", "solucao", "antes-de-atualizar", "antes-de-restaurar", "antes-de-importar"}
NOME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}_[a-z-]+$")
HORARIO_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SUFIXOS_TEMPORARIOS = ("_restaurando", "_verificacao")
# Atrasado ate quanto tempo o agendador ainda faz o backup (ex.: VM desligada no horario).
JANELA_ATRASO = timedelta(hours=12)


class ErroBackup(RuntimeError):
    pass


# ======================================================================== ajustes

def _env_bool(nome: str, padrao: bool) -> bool:
    v = os.environ.get(nome)
    return padrao if v is None or not v.strip() else v.strip().lower() in {"1", "true", "sim", "yes", "on"}


def _env_int(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome, "").strip() or padrao)
    except ValueError:
        return padrao


@dataclass
class Ajustes:
    """O que o administrador muda na tela Backups. Os padroes vem do .env (PORTAL_BACKUP_*)."""

    ativo: bool = True
    horarios: list[str] = field(default_factory=lambda: ["02:30"])
    manter_diarios: int = 14
    manter_semanais: int = 8
    manter_mensais: int = 6
    manter_avulsos: int = 10
    incluir_portal: bool = True
    incluir_bancos: bool = True
    incluir_volumes: bool = True
    incluir_externos: bool = True
    incluir_supabase: bool = True
    excluir: list[str] = field(default_factory=list)
    espaco_minimo_gb: float = 2.0
    copiar: bool = True
    verificar: bool = True
    aviso_webhook: str = ""

    @classmethod
    def do_ambiente(cls) -> Ajustes:
        horarios = [h.strip() for h in os.environ.get("PORTAL_BACKUP_HORARIOS", "").split(",") if h.strip()]
        return cls(
            ativo=_env_bool("PORTAL_BACKUP_ATIVO", True),
            horarios=horarios or ["02:30"],
            manter_diarios=_env_int("PORTAL_BACKUP_MANTER_DIARIOS", 14),
            manter_semanais=_env_int("PORTAL_BACKUP_MANTER_SEMANAIS", 8),
            manter_mensais=_env_int("PORTAL_BACKUP_MANTER_MENSAIS", 6),
            manter_avulsos=_env_int("PORTAL_BACKUP_MANTER_AVULSOS", 10),
            espaco_minimo_gb=float(os.environ.get("PORTAL_BACKUP_ESPACO_MINIMO_GB", "").strip() or 2),
            aviso_webhook=os.environ.get("PORTAL_BACKUP_WEBHOOK", "").strip(),
        )

    def validar(self) -> None:
        if not self.horarios:
            raise ErroBackup("Informe pelo menos um horario (ex.: 02:30).")
        for h in self.horarios:
            if not HORARIO_RE.match(h):
                raise ErroBackup(f"Horario invalido: '{h}'. Use HH:MM, ex.: 02:30.")
        for nome in ("manter_diarios", "manter_semanais", "manter_mensais", "manter_avulsos"):
            if not 0 <= getattr(self, nome) <= 3650:
                raise ErroBackup(f"{nome}: use um numero entre 0 e 3650.")
        if self.manter_diarios < 1:
            raise ErroBackup("Guarde pelo menos 1 dia de backups diarios.")
        if not 0 <= self.espaco_minimo_gb <= 10000:
            raise ErroBackup("Espaco minimo invalido.")
        if self.aviso_webhook and not re.match(r"^https://[^\s]+$", self.aviso_webhook):
            raise ErroBackup("O endereco de aviso deve comecar com https://")

    @classmethod
    def de_dict(cls, dados: dict, base: Ajustes) -> Ajustes:
        """Mistura o que veio da tela com a base, convertendo tipos (campos desconhecidos sao ignorados)."""
        valores = asdict(base)
        for f in fields(cls):
            if f.name not in dados:
                continue
            v = dados[f.name]
            atual = valores[f.name]
            if isinstance(atual, bool):
                v = v if isinstance(v, bool) else str(v).lower() in {"1", "true", "sim", "on"}
            elif isinstance(atual, int):
                v = int(v)
            elif isinstance(atual, float):
                v = float(str(v).replace(",", "."))
            elif isinstance(atual, list):
                if isinstance(v, str):
                    v = [p.strip() for p in v.split(",") if p.strip()]
                v = sorted({str(p).strip() for p in v if str(p).strip()})
            else:
                v = str(v).strip()
            valores[f.name] = v
        aj = cls(**valores)
        aj.validar()
        return aj


# ======================================================================== utilidades

def fuso(cfg: Config) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(cfg.fuso)
    except Exception:  # noqa: BLE001 - imagem sem tzdata: Brasilia nao tem horario de verao desde 2019
        return timezone(timedelta(hours=-3), "BRT")


def _sha256(arq: Path) -> str:
    h = hashlib.sha256()
    with open(arq, "rb") as fh:
        for bloco in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _tamanho_pasta(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def formatar_tamanho(n: float) -> str:
    for unidade in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidade == "GB":
            return f"{n:.0f} {unidade}" if unidade == "B" else f"{n:.1f} {unidade}"
        n /= 1024
    return f"{n:.1f} TB"


def _nome_seguro(nome: str) -> str:
    if not NOME_RE.match(nome):
        raise ErroBackup(f"Backup desconhecido: {nome}")
    return nome


def _psql(container: str, sql: str, banco: str = "postgres") -> str:
    ok, saida = docker_cli.executar_em(
        container, ["psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", banco, "-tAc", sql], timeout=300)
    if not ok:
        raise ErroBackup(f"PostgreSQL recusou o comando: {saida[-400:]}")
    return saida.strip()


def _ident(nome: str) -> str:
    """Nome de banco/usuario vindo do proprio servidor: so aceita o formato simples."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", nome):
        raise ErroBackup(f"Nome fora do padrao, ignorado: {nome!r}")
    return nome


class Trava:
    """Um backup/restauracao por vez, mesmo entre o portal e o comando no terminal."""

    _local = threading.Lock()

    def __init__(self, pasta: Path):
        self.arquivo = pasta / ".trava"

    @contextlib.contextmanager
    def segurar(self) -> Iterator[None]:
        if not self._local.acquire(blocking=False):
            raise ErroBackup("Ja ha um backup ou restauracao em andamento.")
        fh = None
        try:
            if fcntl is not None:
                fh = open(self.arquivo, "w")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ErroBackup("Ja ha um backup ou restauracao em andamento (em outro processo).") from exc
            yield
        finally:
            if fh is not None:
                fh.close()
            self._local.release()


# ======================================================================== servico

class Backups:
    def __init__(self, cfg: Config, gerenciador: Callable[[], Gerenciador],
                 auditoria: Callable[[str, str], None] | None = None):
        self.cfg = cfg
        self._ger = gerenciador
        self.auditoria = auditoria or (lambda acao, detalhe: None)
        self.arquivo_estado = cfg.pasta_dados / "backup.json"
        self.tarefa: Tarefa | None = None
        self._lock_estado = threading.Lock()
        self._agendador: threading.Thread | None = None

    # ---------------- pastas ----------------
    @property
    def pasta(self) -> Path:
        return self.cfg.backup_pasta

    def _preparar_pasta(self, pasta: Path) -> None:
        pasta.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            pasta.chmod(0o700)

    # ---------------- estado e ajustes ----------------
    def _ler(self) -> dict:
        try:
            return json.loads(self.arquivo_estado.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _gravar(self, dados: dict) -> None:
        self.arquivo_estado.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.arquivo_estado.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.arquivo_estado)

    def _atualizar_estado(self, **valores) -> None:
        with self._lock_estado:
            dados = self._ler()
            dados.setdefault("estado", {}).update(valores)
            self._gravar(dados)

    def estado(self) -> dict:
        return self._ler().get("estado", {})

    def padroes(self) -> Ajustes:
        return Ajustes.do_ambiente()

    def ajustes(self) -> Ajustes:
        salvos = self._ler().get("ajustes")
        if not salvos:
            return self.padroes()
        try:
            return Ajustes.de_dict(salvos, self.padroes())
        except (ErroBackup, ValueError, TypeError):
            return self.padroes()

    def personalizado(self) -> bool:
        return bool(self._ler().get("ajustes"))

    def salvar_ajustes(self, dados: dict | None) -> Ajustes:
        """dados=None volta aos padroes do .env."""
        aj = Ajustes.de_dict(dados, self.ajustes()) if dados is not None else self.padroes()
        with self._lock_estado:
            atual = self._ler()
            if dados is None:
                atual.pop("ajustes", None)
            else:
                atual["ajustes"] = asdict(aj)
            self._gravar(atual)
        return aj

    # ---------------- leitura dos backups ----------------
    def _manifesto(self, pasta: Path) -> dict | None:
        try:
            return json.loads((pasta / "manifesto.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def listar(self, pasta: Path | None = None) -> list[dict]:
        pasta = pasta or self.pasta
        if not pasta.is_dir():
            return []
        lista = []
        for p in pasta.iterdir():
            if not p.is_dir() or not NOME_RE.match(p.name):
                continue
            man = self._manifesto(p)
            if man is None:
                continue
            man["nome"] = p.name
            lista.append(man)
        lista.sort(key=lambda m: m["nome"], reverse=True)
        return lista

    def obter(self, nome: str) -> tuple[Path, dict]:
        pasta = self.pasta / _nome_seguro(nome)
        man = self._manifesto(pasta)
        if man is None:
            raise ErroBackup(f"Backup desconhecido: {nome}")
        man["nome"] = nome
        return pasta, man

    def disco(self) -> dict:
        try:
            self._preparar_pasta(self.pasta)
            uso = shutil.disk_usage(self.pasta)
            return {"livre": uso.free, "total": uso.total, "ok": True}
        except OSError as exc:
            return {"livre": 0, "total": 0, "ok": False, "erro": str(exc)}

    def copia_disponivel(self) -> bool:
        return self.cfg.backup_copia is not None

    # ---------------- tarefas em segundo plano ----------------
    def em_segundo_plano(self, acao: str, funcao: Callable[[Tarefa], None]) -> Tarefa:
        if self.tarefa and self.tarefa.estado == "executando":
            raise ErroBackup(f"Ja ha uma operacao em andamento: {self.tarefa.acao}")
        tarefa = Tarefa(acao=acao)
        self.tarefa = tarefa

        def rodar() -> None:
            try:
                funcao(tarefa)
                tarefa.estado = "concluida" if tarefa.erro is None else "falhou"
            except Exception as exc:  # noqa: BLE001 - vira mensagem na tela
                tarefa.estado = "falhou"
                tarefa.erro = str(exc)
                tarefa.registrar(f"ERRO: {exc}")
            finally:
                tarefa.etapa = ""
                tarefa.fim = time.time()

        threading.Thread(target=rodar, daemon=True).start()
        return tarefa

    # ======================================================================== backup
    def fazer(self, motivo: str, registrar: Callable[[str], None] = print,
              somente: list[str] | None = None, incluir_portal: bool | None = None) -> dict:
        """Faz um backup e devolve o manifesto. `somente`: ids de solucoes (backup parcial)."""
        if motivo not in MOTIVOS:
            raise ErroBackup(f"Motivo invalido: {motivo}")
        self._preparar_pasta(self.pasta)
        with Trava(self.pasta).segurar():
            return self._fazer(motivo, registrar, somente, incluir_portal)

    def _fazer(self, motivo: str, registrar: Callable[[str], None], somente: list[str] | None,
               incluir_portal: bool | None) -> dict:
        aj = self.ajustes()
        ger = self._ger()
        tz = fuso(self.cfg)
        inicio = time.time()
        agora = datetime.now(tz)

        livre = shutil.disk_usage(self.pasta).free
        if livre < aj.espaco_minimo_gb * 1024 ** 3:
            msg = (f"Espaco livre insuficiente na pasta de backup: {formatar_tamanho(livre)} "
                   f"(minimo configurado: {aj.espaco_minimo_gb:g} GB).")
            self._atualizar_estado(ultimo_erro=msg, ultimo_erro_em=agora.isoformat())
            self._avisar(aj, f"Backup do Portal LOG20 nao foi feito: {msg}")
            raise ErroBackup(msg)

        nome = f"{agora:%Y-%m-%d_%H%M%S}_{motivo}"
        while (self.pasta / nome).exists():  # dois backups no mesmo segundo (ex.: solucoes pequenas)
            agora += timedelta(seconds=1)
            nome = f"{agora:%Y-%m-%d_%H%M%S}_{motivo}"
        destino = self.pasta / nome
        parcial = self.pasta / f".{nome}.parcial"
        if parcial.exists():
            shutil.rmtree(parcial)
        parcial.mkdir(mode=0o700)
        itens: list[dict] = []
        erros: list[str] = []

        solucoes = {sid: s for sid, s in ger.solucoes.items()
                    if (somente is None or sid in somente) and s.backup_incluir and sid not in aj.excluir}
        ignoradas = sorted(set(ger.solucoes) - set(solucoes)) if somente is None else []
        registrar(f"Backup {nome} em {self.pasta}")
        if ignoradas:
            registrar(f"Fora deste backup (ajustes ou manifesto): {', '.join(ignoradas)}")

        def item(tipo: str, arq: Path, **extra) -> None:
            arq.chmod(0o600)
            info = {"tipo": tipo, "arquivo": str(arq.relative_to(parcial)), "bytes": arq.stat().st_size,
                    "sha256": _sha256(arq), **extra}
            itens.append(info)
            registrar(f"  ok  {info['arquivo']} ({formatar_tamanho(info['bytes'])})")

        def tentar(descricao: str, funcao: Callable[[], None]) -> None:
            try:
                funcao()
            except Exception as exc:  # noqa: BLE001 - um item com erro nao derruba o resto
                msg = f"{descricao}: {exc}"
                erros.append(msg)
                registrar(f"  ERRO {msg}")

        # ---- portal ----
        if (aj.incluir_portal if incluir_portal is None else incluir_portal) and somente is None:
            registrar("Portal (usuarios, acessos, configuracoes e segredos das solucoes)")
            tentar("portal", lambda: self._backup_portal(parcial, item))

        # ---- bancos do servidor ----
        pg = self.cfg.postgres_container
        if aj.incluir_bancos and pg:
            registrar(f"Bancos do PostgreSQL do servidor ({pg})")
            mapa = self._bancos_das_solucoes(ger)
            try:
                banco_pg.aguardar(pg, 60)
                if somente is None:
                    tentar("contas do banco", lambda: self._backup_globais(parcial, item))
                for db in self._listar_bancos(pg):
                    sid = mapa.get(db)
                    if somente is not None and sid not in solucoes:
                        continue
                    if sid and sid not in solucoes:
                        continue
                    tentar(f"banco {db}", lambda db=db, sid=sid: self._backup_banco(parcial, item, pg, db, sid, aj))
            except Exception as exc:  # noqa: BLE001
                erros.append(f"PostgreSQL do servidor: {exc}")
                registrar(f"  ERRO PostgreSQL do servidor: {exc}")

        # ---- volumes ----
        if aj.incluir_volumes:
            vols = [(s, chave) for s in solucoes.values() for chave in s.volumes]
            if vols:
                registrar("Pastas de dados das solucoes (volumes)")
            for sol, chave in vols:
                nome_vol = sol.nome_volume(chave)
                if not docker_cli.volume_existe(nome_vol):
                    registrar(f"  -- {nome_vol}: ainda nao existe (solucao nunca foi iniciada)")
                    continue
                tentar(f"volume {nome_vol}",
                       lambda s=sol, c=chave, v=nome_vol: self._backup_volume(parcial, item, s.id, c, v))

        # ---- bancos externos ----
        if aj.incluir_externos:
            vistos: dict[str, str] = {}
            for sol in solucoes.values():
                if not sol.backup_externo:
                    continue
                url = ger.armazenamento.configuracoes(sol).get(sol.backup_externo, "")
                if not url:
                    registrar(f"  --  {sol.id}: {sol.backup_externo} vazio, sem banco externo")
                    continue
                if not re.match(r"^postgres(ql)?://", url):
                    registrar(f"  --  {sol.id}: {sol.backup_externo} nao e uma URL postgresql://")
                    continue
                chave = hashlib.sha256(url.encode()).hexdigest()
                if chave in vistos:
                    registrar(f"  -- {sol.id}: mesmo banco externo de {vistos[chave]} (ja importado)")
                    continue
                vistos[chave] = sol.id
                if not pg:
                    erros.append(f"banco externo de {sol.id}: precisa do PostgreSQL do servidor (pg_dump)")
                    continue
                registrar(f"Banco externo de {sol.id} ({sol.backup_externo})")
                tentar(f"banco externo de {sol.id}",
                       lambda s=sol, u=url: self._backup_externo(parcial, item, pg, s, u))

        # ---- Supabase das solucoes ([supabase] no manifesto) ----
        if aj.incluir_supabase:
            com_sb = [s for s in solucoes.values() if s.supabase]
            if com_sb:
                registrar("Supabase dos sistemas (banco, usuarios do login, arquivos e chaves)")
            for sol in com_sb:
                tentar(f"Supabase de {sol.id}", lambda s=sol: self._backup_supabase(parcial, item, s, registrar))

        # ---- manifesto ----
        man = {
            "formato": VERSAO_FORMATO,
            "nome": nome,
            "motivo": motivo,
            "criado_em": agora.isoformat(timespec="seconds"),
            "duracao_s": round(time.time() - inicio, 1),
            "parcial": somente is not None,
            "solucoes": sorted(solucoes) if somente is not None else sorted(set(ger.solucoes) - set(ignoradas)),
            "commit_portal": _commit_portal(),
            "itens": itens,
            "erros": erros,
            "ok": not erros,
            "bytes": sum(i["bytes"] for i in itens),
            "fixado": False,
        }
        (parcial / "manifesto.json").write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
        parcial.rename(destino)
        registrar(f"Backup concluido: {len(itens)} arquivo(s), {formatar_tamanho(man['bytes'])}, "
                  f"{man['duracao_s']:.0f}s" + (f", {len(erros)} erro(s)" if erros else ""))

        copia_erro = ""
        if aj.copiar and self.cfg.backup_copia is not None:
            try:
                self._copiar(destino, registrar)
            except Exception as exc:  # noqa: BLE001
                copia_erro = f"Copia para {self.cfg.backup_copia} falhou: {exc}"
                registrar(f"  ERRO {copia_erro}")

        try:
            removidos = self.aplicar_retencao(aj, registrar)
            if removidos:
                registrar(f"Retencao: {len(removidos)} backup(s) antigo(s) removido(s).")
        except Exception as exc:  # noqa: BLE001
            registrar(f"  ERRO na limpeza dos antigos: {exc}")

        if somente is None:  # backups parciais (seguranca antes de restaurar) nao contam como "o ultimo"
            self._atualizar_estado(
                ultimo=nome, ultimo_ok=man["ok"] and not copia_erro, ultimo_em=man["criado_em"],
                ultimo_erros=erros + ([copia_erro] if copia_erro else []), ultimo_bytes=man["bytes"],
                **({"ultimo_sucesso": nome, "ultimo_sucesso_em": man["criado_em"]} if man["ok"] else {}),
            )
        if erros or copia_erro:
            self._avisar(aj, f"Backup do Portal LOG20 ({nome}) terminou com erros: " + "; ".join(erros + [copia_erro])[:900])
        return man

    # ---------------- partes do backup ----------------
    def _backup_portal(self, parcial: Path, item) -> None:
        pasta = parcial / "portal"
        pasta.mkdir()
        db = self.cfg.pasta_dados / "portal.db"
        if db.exists():
            destino = pasta / "portal.db"
            # API de backup do SQLite: copia consistente mesmo com o portal em uso.
            with contextlib.closing(sqlite3.connect(db, timeout=30)) as origem, \
                 contextlib.closing(sqlite3.connect(destino)) as copia:
                origem.backup(copia)
            item("portal", destino)
        arq = pasta / "dados.tar.gz"
        with tarfile.open(arq, "w:gz") as tar:
            for p in sorted(self.cfg.pasta_dados.glob("*.json")):
                tar.add(p, arcname=p.name)
            chaves_sb = self.cfg.pasta_dados / "supabase"
            if chaves_sb.is_dir():  # chaves do Supabase de cada sistema
                tar.add(chaves_sb, arcname="supabase")
            locais = self.cfg.pasta_solucoes_locais
            if locais.is_dir():
                tar.add(locais, arcname=locais.name)
        item("portal", arq)

    def _bancos_das_solucoes(self, ger: Gerenciador) -> dict[str, str]:
        mapa = {}
        for sol in ger.solucoes.values():
            guardado = ger.armazenamento.banco(sol.id) or {}
            if guardado.get("nome"):
                mapa[guardado["nome"]] = sol.id
            elif sol.banco:
                with contextlib.suppress(banco_pg.ErroBanco):
                    mapa[banco_pg.nomes(sol.id)[0]] = sol.id
        return mapa

    def _listar_bancos(self, pg: str) -> list[str]:
        saida = _psql(pg, "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' "
                          "ORDER BY datname")
        return [d for d in saida.splitlines() if d and not d.endswith(SUFIXOS_TEMPORARIOS)]

    def _backup_globais(self, parcial: Path, item) -> None:
        (parcial / "postgres").mkdir(exist_ok=True)
        arq = parcial / "postgres" / "contas.sql"
        docker_cli.para_arquivo(["exec", self.cfg.postgres_container, "pg_dumpall", "-U", "postgres",
                                 "--globals-only"], arq, timeout=300)
        item("contas", arq)

    def _backup_banco(self, parcial: Path, item, pg: str, db: str, sid: str | None, aj: Ajustes) -> None:
        (parcial / "bancos").mkdir(exist_ok=True)
        arq = parcial / "bancos" / f"{_ident(db)}.dump"
        docker_cli.para_arquivo(["exec", pg, "pg_dump", "-U", "postgres", "-Fc", "-Z", "6", "-d", db], arq)
        dono = _psql(pg, f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '{db}'")
        extra = {"banco": db, "dono": dono, "solucao": sid}
        if aj.verificar:
            extra["tabelas"] = self._contar_tabelas_dump(pg, arq)
        item("banco", arq, **extra)

    def _contar_tabelas_dump(self, pg: str, arq: Path) -> int:
        """Le o indice do dump (pg_restore --list): confirma que o arquivo esta inteiro."""
        saida = docker_cli.de_arquivo(["exec", "-i", pg, "pg_restore", "--list"], arq, timeout=600)
        return _tabelas_no_indice(saida)

    def _backup_volume(self, parcial: Path, item, sid: str, chave: str, nome_vol: str) -> None:
        (parcial / "volumes").mkdir(exist_ok=True)
        arq = parcial / "volumes" / f"{nome_vol}.tar.gz"
        docker_cli.para_arquivo(["run", "--rm", "--network", "none", "-v", f"{nome_vol}:/dados:ro",
                                 "--entrypoint", "tar", self.cfg.backup_imagem,
                                 "-czf", "-", "--numeric-owner", "-C", "/dados", "."], arq)
        with tarfile.open(arq, "r:gz") as tar:  # confere que o arquivo abre
            arquivos = sum(1 for m in tar.getmembers() if m.isfile())
        item("volume", arq, volume=nome_vol, chave=chave, solucao=sid, arquivos=arquivos)

    def _backup_externo(self, parcial: Path, item, pg: str, sol, url: str) -> None:
        (parcial / "externos").mkdir(exist_ok=True)
        arq = parcial / "externos" / f"{sol.id}.dump"
        esquemas = " ".join(f"-n {e}" for e in sol.backup_esquemas)
        # A URL (com senha) vai pelo ambiente do docker exec: nao aparece em linha de comando nem log.
        docker_cli.para_arquivo(
            ["exec", "-e", "PORTAL_BACKUP_URL", pg, "sh", "-c",
             f'exec pg_dump -Fc -Z 6 --no-owner --no-privileges {esquemas} "$PORTAL_BACKUP_URL"'],
            arq, env={"PORTAL_BACKUP_URL": url}, timeout=3600)
        host = re.sub(r"^.*@", "", url.split("?")[0]).split("/")[0]
        item("externo", arq, solucao=sol.id, configuracao=sol.backup_externo, servidor=host,
             esquemas=sol.backup_esquemas, tabelas=self._contar_tabelas_dump(pg, arq))

    def _backup_supabase(self, parcial: Path, item, sol, registrar) -> None:
        """Supabase proprio de uma solucao, em tres partes:

        - chaves (dados/supabase/<id>.json): sem elas o banco restaurado nao abre (as senhas dos
          papeis internos e o segredo dos tokens ficam gravados dentro dele);
        - banco em pg_dump (com o sistema no ar), para conferir e para levar a outro lugar;
        - os volumes (arquivos do PostgreSQL, configuracao, arquivos enviados), copiados com a
          pilha parada por alguns segundos, para a copia ficar identica e consistente. E desta
          copia que a restauracao volta.
        """
        sid = sol.id
        seg_arq = sb.caminho_segredos(self.cfg.pasta_dados, sid)
        vols = [(chave, sb.volume(sid, chave)) for chave in ("dados", "config", "arquivos", "snippets")]
        vols = [(c, v) for c, v in vols if docker_cli.volume_existe(v)]
        if not seg_arq.is_file() and not vols:
            registrar(f"  -- {sid}: Supabase ainda nao criado (a solucao nunca foi iniciada)")
            return
        pasta = parcial / "supabase" / sid
        pasta.mkdir(parents=True, exist_ok=True)
        if seg_arq.is_file():
            destino = pasta / "chaves.json"
            shutil.copyfile(seg_arq, destino)
            item("supabase-segredos", destino, solucao=sid)
        seg = sb.segredos(self.cfg.pasta_dados, sid, criar=False)
        db = sb.container(sid, "db")
        if seg is not None and sb._estado(db)["rodando"]:
            arq = pasta / "banco.dump"
            env = {"PGPASSWORD": seg.senha_banco}
            docker_cli.para_arquivo(["exec", "-e", "PGPASSWORD", db, "pg_dump", "-h", "localhost", "-U", "supabase_admin",
                                     "-d", "postgres", "-Fc", "-Z", "6"], arq, env=env, timeout=3600)
            tabelas = _tabelas_no_indice(docker_cli.de_arquivo(["exec", "-i", db, "pg_restore", "--list"], arq, timeout=600))
            item("supabase-banco", arq, solucao=sid, tabelas=tabelas)
        elif seg is None:
            registrar(f"  -- {sid}: chaves do Supabase nao encontradas; vai so a copia dos arquivos do banco")
        else:
            registrar(f"  -- {sid}: Supabase parado; vai so a copia dos arquivos do banco")
        if not vols:
            return
        parados = sb.pausar(sid)
        inicio = time.time()
        if parados:
            registrar(f"  {sid}: Supabase parado para a copia ficar consistente")
        try:
            for chave, nome_vol in vols:
                arq = pasta / f"{chave}.tar.gz"
                docker_cli.para_arquivo(["run", "--rm", "--network", "none", "-v", f"{nome_vol}:/dados:ro",
                                         "--entrypoint", "tar", self.cfg.backup_imagem,
                                         "-czf", "-", "--numeric-owner", "-C", "/dados", "."], arq)
                with tarfile.open(arq, "r:gz") as tar:
                    arquivos = sum(1 for m in tar.getmembers() if m.isfile())
                item("supabase-volume", arq, solucao=sid, chave=chave, volume=nome_vol, arquivos=arquivos)
        finally:
            if parados:
                sb.retomar(sid, parados, lambda l: registrar("  " + l))
                registrar(f"  {sid}: Supabase no ar de novo ({time.time() - inicio:.0f}s parado)")

    # ---------------- copia e retencao ----------------
    def _copiar(self, origem: Path, registrar) -> None:
        destino_base = self.cfg.backup_copia
        assert destino_base is not None
        if not destino_base.is_dir():
            raise ErroBackup("a pasta de copia nao existe ou nao esta montada")
        tmp = destino_base / f".{origem.name}.parcial"
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(origem, tmp)
        tmp.rename(destino_base / origem.name)
        registrar(f"Copia gravada em {destino_base / origem.name}")
        self.aplicar_retencao(self.ajustes(), registrar, pasta=destino_base)

    def manter(self, lista: list[dict], aj: Ajustes) -> set[str]:
        """Quais backups ficam (pelo nome), da lista completa de uma pasta."""
        tz = fuso(self.cfg)
        ficar: set[str] = set()
        agendados = [m for m in lista if m.get("motivo") == "agendado"]
        avulsos = [m for m in lista if m.get("motivo") != "agendado"]
        hoje = datetime.now(tz).date()

        def data(m: dict):
            return datetime.strptime(m["nome"][:17], "%Y-%m-%d_%H%M%S").date()

        for m in agendados:
            if (hoje - data(m)).days < aj.manter_diarios:
                ficar.add(m["nome"])
        for chave, quantos in ((lambda d: d.isocalendar()[:2], aj.manter_semanais),
                               (lambda d: (d.year, d.month), aj.manter_mensais)):
            vistos: list = []
            for m in agendados:  # do mais novo para o mais antigo
                k = chave(data(m))
                if k in vistos:
                    continue
                if len(vistos) >= quantos:
                    break
                vistos.append(k)
                ficar.add(m["nome"])
        # Manuais/antes de atualizar (completos) e os parciais (so uma solucao: o "so desta solucao"
        # e os de seguranca) contam separado, e os parciais por solucao: varios backups de uma
        # solucao nao empurram para fora os completos nem os das outras solucoes.
        ficar.update(m["nome"] for m in [a for a in avulsos if not a.get("parcial")][:aj.manter_avulsos])
        grupos: dict[tuple, list[dict]] = {}
        for a in avulsos:
            if a.get("parcial"):
                grupos.setdefault(tuple(sorted(a.get("solucoes") or [])), []).append(a)
        for lista_grupo in grupos.values():
            ficar.update(m["nome"] for m in lista_grupo[:aj.manter_avulsos])
        ficar.update(m["nome"] for m in lista if m.get("fixado"))
        completos_ok = [m for m in lista if m.get("ok") and not m.get("parcial")]
        if completos_ok:  # nunca apaga o ultimo backup completo bom
            ficar.add(completos_ok[0]["nome"])
        return ficar

    def aplicar_retencao(self, aj: Ajustes, registrar=print, pasta: Path | None = None) -> list[str]:
        pasta = pasta or self.pasta
        lista = self.listar(pasta)
        ficar = self.manter(lista, aj)
        removidos = []
        for m in lista:
            if m["nome"] not in ficar:
                shutil.rmtree(pasta / m["nome"], ignore_errors=True)
                removidos.append(m["nome"])
        # restos de backups interrompidos (mais de um dia)
        for p in pasta.glob(".*.parcial"):
            if time.time() - p.stat().st_mtime > 86400:
                shutil.rmtree(p, ignore_errors=True)
        return removidos

    def fixar(self, nome: str, fixado: bool) -> None:
        pasta, man = self.obter(nome)
        man["fixado"] = bool(fixado)
        man.pop("nome", None)
        man["nome"] = nome
        (pasta / "manifesto.json").write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def so_da_solucao(man: dict, sid: str) -> bool:
        """Backup isolado desta solucao (parcial, so com ela)."""
        return bool(man.get("parcial")) and list(man.get("solucoes") or []) == [sid]

    def fazer_solucao(self, sid: str, registrar: Callable[[str], None] = print) -> dict:
        """Backup so desta solucao: banco, pastas de dados, banco externo e Supabase dela."""
        sol = self._ger().solucoes.get(sid)
        if sol is None:
            raise ErroBackup(f"Solucao desconhecida: {sid}")
        if not sol.backup_incluir or sid in self.ajustes().excluir:
            raise ErroBackup(f"{sol.nome} esta fora do backup (manifesto ou Ajustes).")
        man = self.fazer("solucao", registrar, somente=[sid])
        if not man["itens"] and not man["erros"]:
            # Nada para guardar (ex.: nunca foi iniciada): nao deixa um backup vazio na lista.
            with Trava(self.pasta).segurar():
                shutil.rmtree(self.pasta / man["nome"], ignore_errors=True)
            raise ErroBackup(f"{sol.nome} ainda nao tem dados para guardar (banco, pastas ou Supabase).")
        return man

    def apagar_todos(self, solucao: str | None = None) -> dict:
        """Apaga de uma vez os backups desta pasta, menos os fixados. Com `solucao`: so os
        backups isolados dela (os completos tem todas as solucoes e ficam). A segunda copia
        (pasta de rede) nao e tocada."""
        apagados: list[str] = []
        fixados = 0
        with Trava(self.pasta).segurar():
            for m in self.listar():
                if solucao and not self.so_da_solucao(m, solucao):
                    continue
                if m.get("fixado"):
                    fixados += 1
                    continue
                shutil.rmtree(self.pasta / m["nome"])
                apagados.append(m["nome"])
        return {"apagados": apagados, "fixados": fixados}

    def remover(self, nome: str) -> None:
        pasta, man = self.obter(nome)
        if man.get("fixado"):
            raise ErroBackup("Este backup esta fixado. Desfixe antes de apagar.")
        with Trava(self.pasta).segurar():
            shutil.rmtree(pasta)

    def _avisar(self, aj: Ajustes, texto: str) -> None:
        if not aj.aviso_webhook:
            return
        try:
            req = urllib.request.Request(aj.aviso_webhook, data=json.dumps({"text": texto}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as exc:  # noqa: BLE001 - aviso e best-effort
            print(f"[backup] aviso nao enviado: {exc}", flush=True)

    # ======================================================================== conferencia
    def verificar(self, nome: str, registrar: Callable[[str], None] = print) -> dict:
        """Confere o backup: sha256 de cada arquivo e uma restauracao de teste de cada banco
        num banco temporario (apagado no fim). Nada do que esta em uso e tocado."""
        pasta, man = self.obter(nome)
        pg = self.cfg.postgres_container
        problemas: list[str] = []
        with Trava(self.pasta).segurar():
            for it in man["itens"]:
                arq = pasta / it["arquivo"]
                if not arq.is_file():
                    problemas.append(f"{it['arquivo']}: arquivo sumiu")
                    continue
                if _sha256(arq) != it["sha256"]:
                    problemas.append(f"{it['arquivo']}: conteudo diferente do gravado (arquivo corrompido)")
                    continue
                try:
                    if it["tipo"] == "banco" and pg:
                        tmp = f"{it['banco'][:50]}_verificacao"
                        tabelas = self._restaurar_em(pg, arq, tmp, dono=None, manter=False)
                        registrar(f"  ok  {it['arquivo']}: restaurado num banco de teste, {tabelas} tabela(s)")
                        continue
                    if it["tipo"] == "externo" and pg:
                        n = self._contar_tabelas_dump(pg, arq)
                        registrar(f"  ok  {it['arquivo']}: indice legivel, {n} tabela(s)")
                        continue
                    if it["tipo"] == "supabase-banco":
                        saida = docker_cli.de_arquivo(["run", "--rm", "-i", "--network", "none", "--entrypoint",
                                                       "pg_restore", self.cfg.backup_imagem, "--list"], arq, timeout=600)
                        registrar(f"  ok  {it['arquivo']}: indice legivel, {_tabelas_no_indice(saida)} tabela(s)")
                        continue
                    if it["tipo"] == "supabase-segredos":
                        chaves = json.loads(arq.read_text(encoding="utf-8"))
                        if not {"senha_banco", "jwt_secret", "anon", "service"} <= set(chaves):
                            raise ErroBackup("arquivo de chaves incompleto")
                        registrar(f"  ok  {it['arquivo']}")
                        continue
                    if it["tipo"] in ("volume", "portal", "supabase-volume") and arq.suffix == ".gz":
                        with tarfile.open(arq, "r:gz") as tar:
                            n = sum(1 for m in tar.getmembers() if m.isfile())
                        registrar(f"  ok  {it['arquivo']}: {n} arquivo(s)")
                        continue
                    if arq.name == "portal.db":
                        with contextlib.closing(sqlite3.connect(f"file:{arq}?mode=ro", uri=True)) as c:
                            r = c.execute("PRAGMA integrity_check").fetchone()[0]
                        if r != "ok":
                            raise ErroBackup(r)
                    registrar(f"  ok  {it['arquivo']}")
                except Exception as exc:  # noqa: BLE001
                    problemas.append(f"{it['arquivo']}: {exc}")
                    registrar(f"  ERRO {it['arquivo']}: {exc}")
        resultado = {"ok": not problemas, "problemas": problemas,
                     "em": datetime.now(fuso(self.cfg)).isoformat(timespec="seconds")}
        man["verificacao"] = resultado
        (pasta / "manifesto.json").write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
        registrar("Verificacao: sem erros." if not problemas else f"Verificacao: {len(problemas)} problema(s).")
        return resultado

    # ======================================================================== restauracao
    def _restaurar_em(self, pg: str, arq: Path, banco: str, dono: str | None, manter: bool,
                      formato: str = "dump") -> int:
        """Carrega `arq` num banco novo `banco` (apagado antes, se existir). Tudo numa transacao: se
        qualquer comando falhar, nada fica pela metade. Devolve o numero de tabelas.

        formato "dump": pg_dump -Fc (pg_restore); "sql": SQL puro (psql), ja sem donos e permissoes.
        Com `dono`, os objetos criados ficam desse usuario (o da solucao)."""
        banco = _ident(banco)
        _psql(pg, f"DROP DATABASE IF EXISTS {banco} WITH (FORCE)")
        _psql(pg, f"CREATE DATABASE {banco} OWNER {_ident(dono or 'postgres')} ENCODING 'UTF8' TEMPLATE template0")
        _psql(pg, f"REVOKE ALL ON DATABASE {banco} FROM PUBLIC")
        if formato == "sql":
            # -o /dev/null: so os erros aparecem no registro (nao o resultado de cada SELECT do arquivo).
            args = ["exec", "-i", pg, "psql", "-q", "-X", "-o", "/dev/null", "-U", "postgres", "-d", banco,
                    "-v", "ON_ERROR_STOP=1", "--single-transaction"]
            if dono:
                args += ["-c", f"SET ROLE {_ident(dono)}"]
            args += ["-f", "-"]
        else:
            args = ["exec", "-i", pg, "pg_restore", "-U", "postgres", "-d", banco, "--no-owner", "--no-privileges",
                    "--single-transaction", "--exit-on-error"]
            if dono:
                args.append(f"--role={_ident(dono)}")
        try:
            docker_cli.de_arquivo(args, arq, timeout=3600)
            tabelas = int(_psql(pg, "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN "
                                    "('pg_catalog', 'information_schema')", banco=banco) or 0)
        except Exception:
            with contextlib.suppress(Exception):
                _psql(pg, f"DROP DATABASE IF EXISTS {banco} WITH (FORCE)")
            raise
        if not manter:
            _psql(pg, f"DROP DATABASE IF EXISTS {banco} WITH (FORCE)")
        return tabelas

    def _restaurar_banco(self, pg: str, arq: Path, banco: str, dono: str, registrar, formato: str = "dump") -> None:
        """Restaura num banco temporario e, so se tudo der certo, troca pelo atual."""
        banco, dono = _ident(banco), _ident(dono)
        tmp = f"{banco[:50]}_restaurando"
        registrar(f"Restaurando o banco {banco} (primeiro num banco temporario)")
        tabelas = self._restaurar_em(pg, arq, tmp, dono=dono, manter=True, formato=formato)
        registrar(f"  {tabelas} tabela(s) restaurada(s); trocando pelo banco atual")
        _psql(pg, f"DROP DATABASE IF EXISTS {banco} WITH (FORCE)")
        _psql(pg, f"ALTER DATABASE {tmp} RENAME TO {banco}")
        registrar(f"  ok  banco {banco} restaurado")

    def _restaurar_volume(self, arq: Path, nome_vol: str, registrar, juntar: bool = False) -> None:
        registrar(f"Restaurando a pasta de dados {nome_vol}" + (" (mantendo os arquivos que ja estao la)" if juntar else ""))
        limpar = "" if juntar else "find /dados -mindepth 1 -delete && "
        docker_cli.de_arquivo(
            ["run", "--rm", "-i", "--network", "none", "-v", f"{nome_vol}:/dados", "--entrypoint", "sh",
             self.cfg.backup_imagem, "-c",
             f"{limpar}tar -xzf - --numeric-owner -p -C /dados"], arq)
        registrar(f"  ok  {nome_vol} restaurado")

    def restaurar_solucao(self, nome: str, sid: str, banco: bool = True, arquivos: bool = True,
                          registrar: Callable[[str], None] = print, seguranca: bool = True,
                          supabase: bool = True) -> None:
        """Volta os dados de uma solucao (banco e/ou volumes) ao estado do backup `nome`.

        Antes: backup de seguranca so desta solucao e parada do container. Depois: inicia de novo
        se estava no ar. O banco so e trocado se a restauracao inteira der certo.
        """
        pasta, man = self.obter(nome)
        ger = self._ger()
        sol = ger.solucoes.get(sid)
        if sol is None:
            raise ErroBackup(f"Solucao desconhecida neste portal: {sid}")
        dumps = [i for i in man["itens"] if i["tipo"] == "banco" and i.get("solucao") == sid] if banco else []
        vols = [i for i in man["itens"] if i["tipo"] == "volume" and i.get("solucao") == sid] if arquivos else []
        # O Supabase volta inteiro (banco, usuarios, arquivos e chaves juntos): partes de momentos
        # diferentes nao combinam (ex.: um arquivo listado no banco que nao esta no disco).
        sbs = [i for i in man["itens"] if i["tipo"] in ("supabase-volume", "supabase-segredos")
               and i.get("solucao") == sid] if supabase else []
        if sbs and not any(i["tipo"] == "supabase-volume" and i.get("chave") == "dados" for i in sbs):
            raise ErroBackup(f"O backup {nome} nao tem a copia completa do Supabase de {sid}.")
        if not dumps and not vols and not sbs:
            raise ErroBackup(f"O backup {nome} nao tem dados de {sid} para restaurar.")
        pg = self.cfg.postgres_container
        if dumps and not pg:
            raise ErroBackup("Este portal nao tem o PostgreSQL do servidor configurado.")
        for it in dumps + vols + sbs:
            if _sha256(pasta / it["arquivo"]) != it["sha256"]:
                raise ErroBackup(f"{it['arquivo']} esta corrompido (sha256 diferente). Nada foi alterado.")

        if seguranca:
            registrar(f"Backup de seguranca de {sid} antes de restaurar")
            seg = self.fazer("antes-de-restaurar", lambda l: registrar("  " + l), somente=[sid])
            if not seg["ok"]:
                raise ErroBackup("O backup de seguranca teve erros. Nada foi restaurado. " + "; ".join(seg["erros"]))

        estava_no_ar = False
        falha: Exception | None = None
        with Trava(self.pasta).segurar():
            info = docker_cli.container_inspect(sol.container)
            estava_no_ar = bool(info and info.get("State", {}).get("Running"))
            try:
                if info:
                    registrar(f"Parando {sol.container}")
                    docker_cli.stop(sol.container)
                for it in dumps:
                    guardado = ger.armazenamento.banco(sid) or {}
                    if guardado.get("senha"):  # usuario do banco recriado se faltar (ex.: VM nova)
                        banco_pg.garantir(pg, sid, guardado["senha"], registrar)
                    dono = it.get("dono") or banco_pg.nomes(sid)[1]
                    if _psql(pg, f"SELECT 1 FROM pg_roles WHERE rolname = '{_ident(dono)}'") != "1":
                        dono = banco_pg.nomes(sid)[1]
                    self._restaurar_banco(pg, pasta / it["arquivo"], it["banco"], dono, registrar)
                for it in vols:
                    self._restaurar_volume(pasta / it["arquivo"], it["volume"], registrar)
                if sbs:
                    registrar(f"Parando o Supabase de {sid}")
                    sb.parar(sid, registrar)
                    for it in sbs:
                        if it["tipo"] == "supabase-segredos":
                            registrar("Restaurando as chaves do Supabase")
                            sb.gravar_segredos(self.cfg.pasta_dados, sid, (pasta / it["arquivo"]).read_bytes())
                    for it in sbs:
                        if it["tipo"] == "supabase-volume":
                            self._restaurar_volume(pasta / it["arquivo"], it["volume"], registrar)
            except Exception as exc:  # noqa: BLE001 - religa a solucao antes de avisar
                falha = exc
                registrar(f"ERRO: {exc}")

        if estava_no_ar:
            registrar(f"Iniciando {sid} de novo")
            tarefa = _TarefaRepasse("iniciar", registrar)
            ger._rodar(sol, tarefa)
            if tarefa.estado != "concluida" and falha is None:
                raise ErroBackup(f"Dados restaurados, mas a solucao nao subiu: {tarefa.erro}")
        else:
            registrar("A solucao estava parada e continua parada.")
        if falha is not None:
            raise ErroBackup(f"Restauracao interrompida: {falha}. O banco atual so e trocado quando a restauracao inteira da certo. O backup de seguranca esta na lista.")
        self.auditoria("Restaurou backup", f"{sid} a partir de {nome}"
                       + (" (banco)" if dumps else "") + (" (arquivos)" if vols else "")
                       + (" (Supabase)" if sbs else ""))

    def restaurar_portal(self, nome: str, registrar: Callable[[str], None] = print) -> None:
        """Volta usuarios, acessos, configuracoes e segredos do portal ao estado do backup."""
        pasta, man = self.obter(nome)
        itens = {Path(i["arquivo"]).name: i for i in man["itens"] if i["tipo"] == "portal"}
        if not itens:
            raise ErroBackup(f"O backup {nome} nao tem os dados do portal.")
        for it in itens.values():
            if _sha256(pasta / it["arquivo"]) != it["sha256"]:
                raise ErroBackup(f"{it['arquivo']} esta corrompido. Nada foi alterado.")
        dados = self.cfg.pasta_dados
        dados.mkdir(parents=True, exist_ok=True)
        if "dados.tar.gz" in itens:
            registrar("Restaurando configuracoes e segredos das solucoes")
            with tarfile.open(pasta / itens["dados.tar.gz"]["arquivo"], "r:gz") as tar:
                for m in tar.getmembers():
                    destino = (dados / m.name).resolve()
                    if dados.resolve() not in destino.parents or m.issym() or m.islnk():
                        raise ErroBackup(f"Arquivo suspeito no backup: {m.name}")
                tar.extractall(dados)  # noqa: S202 - membros conferidos acima
        if "portal.db" in itens:
            registrar("Restaurando usuarios, acessos e historico")
            with contextlib.closing(sqlite3.connect(pasta / itens["portal.db"]["arquivo"])) as origem, \
                 contextlib.closing(sqlite3.connect(dados / "portal.db", timeout=30)) as atual:
                origem.backup(atual)
        registrar("Portal restaurado. Reinicie o portal: docker compose restart portal")

    def restaurar_servidor(self, nome: str, registrar: Callable[[str], None] = print) -> None:
        """VM nova: recria as contas do banco (menos o administrador `postgres`, que fica com a senha
        do .env desta VM) e restaura os bancos que nao sao de nenhuma solucao (ex.: criados no pgAdmin).
        Os bancos das solucoes vem depois, com restaurar_solucao."""
        pasta, man = self.obter(nome)
        pg = self.cfg.postgres_container
        if not pg:
            raise ErroBackup("Este portal nao tem o PostgreSQL do servidor configurado.")
        banco_pg.aguardar(pg, 120)
        contas = [i for i in man["itens"] if i["tipo"] == "contas"]
        if contas:
            registrar("Recriando as contas do banco")
            linhas = (pasta / contas[0]["arquivo"]).read_text(encoding="utf-8").splitlines()
            sql = "\n".join(l for l in linhas if not re.search(r"\bROLE postgres\b", l))
            tmp = pasta / ".contas-sem-postgres.sql"
            tmp.write_text(sql, encoding="utf-8")
            try:
                # Sem ON_ERROR_STOP: contas que ja existem so geram aviso.
                docker_cli.de_arquivo(["exec", "-i", pg, "psql", "-q", "-U", "postgres", "-d", "postgres"], tmp)
            finally:
                tmp.unlink(missing_ok=True)
        for it in man["itens"]:
            if it["tipo"] != "banco" or it.get("solucao"):
                continue
            dono = it.get("dono") or "postgres"
            if _psql(pg, f"SELECT 1 FROM pg_roles WHERE rolname = '{_ident(dono)}'") != "1":
                dono = "postgres"
            self._restaurar_banco(pg, pasta / it["arquivo"], it["banco"], dono, registrar)

    # ======================================================================== importar arquivo
    # Um arquivo trazido de fora (ex.: pg_dump da maquina de quem desenvolveu, ou um .zip com
    # planilhas) vira os dados de uma solucao. Dois passos: receber (o arquivo e analisado e fica
    # em <pasta>/importacoes/<id>/) e aplicar (com backup de seguranca e a mesma troca segura
    # da restauracao). Arquivos recebidos e nao usados somem depois de 7 dias.

    @property
    def pasta_importacoes(self) -> Path:
        return self.pasta / "importacoes"

    def nova_importacao(self, nome_original: str) -> tuple[str, Path]:
        """Reserva uma pasta e devolve (id, caminho onde gravar o arquivo enviado)."""
        nome = Path(str(nome_original).replace("\\", "/")).name.strip() or "arquivo"
        if not re.fullmatch(r"[\w .()+-]{1,120}", nome, flags=re.UNICODE):
            nome = re.sub(r"[^\w.()+-]", "_", nome)[:120] or "arquivo"
        self._preparar_pasta(self.pasta)
        self._preparar_pasta(self.pasta_importacoes)
        livre = shutil.disk_usage(self.pasta).free
        if livre < self.ajustes().espaco_minimo_gb * 1024 ** 3:
            raise ErroBackup(f"Pouco espaco livre na pasta de backup ({formatar_tamanho(livre)}).")
        iid = f"{datetime.now(fuso(self.cfg)):%Y%m%d-%H%M%S}-{os.urandom(3).hex()}"
        pasta = self.pasta_importacoes / iid
        pasta.mkdir(mode=0o700)
        (pasta / "info.json").write_text(json.dumps({"id": iid, "arquivo": nome, "estado": "recebendo"}), encoding="utf-8")
        return iid, pasta / f"original-{nome}"

    def _pasta_importacao(self, iid: str) -> Path:
        if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", iid):
            raise ErroBackup("Importacao desconhecida.")
        pasta = self.pasta_importacoes / iid
        if not (pasta / "info.json").is_file():
            raise ErroBackup("Importacao desconhecida (ja usada ou apagada).")
        return pasta

    def importacao(self, iid: str) -> dict:
        return json.loads((self._pasta_importacao(iid) / "info.json").read_text(encoding="utf-8"))

    def _gravar_importacao(self, info: dict) -> None:
        pasta = self._pasta_importacao(info["id"])
        (pasta / "info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")

    def listar_importacoes(self) -> list[dict]:
        if not self.pasta_importacoes.is_dir():
            return []
        lista = []
        for p in sorted(self.pasta_importacoes.iterdir(), reverse=True):
            if not p.is_dir():
                continue
            if time.time() - p.stat().st_mtime > 7 * 86400:
                shutil.rmtree(p, ignore_errors=True)
                continue
            with contextlib.suppress(OSError, ValueError):
                info = json.loads((p / "info.json").read_text(encoding="utf-8"))
                if info.get("estado") == "pronto":
                    lista.append({k: v for k, v in info.items() if k != "preparado"})
        return lista

    def apagar_importacao(self, iid: str) -> None:
        shutil.rmtree(self._pasta_importacao(iid), ignore_errors=True)

    def versao_servidor(self) -> int:
        """Versao principal do PostgreSQL do servidor (ex.: 17); 0 se nao der para saber."""
        if getattr(self, "_versao_pg", 0):
            return self._versao_pg
        pg = self.cfg.postgres_container
        if not pg:
            return 0
        with contextlib.suppress(Exception):
            self._versao_pg = int(_psql(pg, "SHOW server_version_num")) // 10000
            return self._versao_pg
        return 0

    def analisar_importacao(self, iid: str, original: Path) -> dict:
        """Descobre o que e o arquivo e o prepara para importar. Erros viram ErroBackup com a
        explicacao (e a pasta e apagada)."""
        pasta = self._pasta_importacao(iid)
        info = self.importacao(iid)
        try:
            info.update(self._analisar(pasta, original))
        except Exception:
            shutil.rmtree(pasta, ignore_errors=True)
            raise
        info.update(estado="pronto", bytes_original=original.stat().st_size,
                    recebido_em=datetime.now(fuso(self.cfg)).isoformat(timespec="seconds"))
        if info["preparado"] != original.name:
            original.unlink(missing_ok=True)
        self._gravar_importacao(info)
        return {k: v for k, v in info.items() if k != "preparado"}

    def _analisar(self, pasta: Path, original: Path) -> dict:
        with open(original, "rb") as fh:
            cabeca = fh.read(4096)
        if not cabeca:
            raise ErroBackup("O arquivo esta vazio.")
        if cabeca[:5] == b"PGDMP":
            return self._analisar_dump(original)
        if cabeca[:4] == b"PK\x03\x04":
            return self._preparar_arquivos_zip(pasta, original)
        if cabeca[:2] == b"\x1f\x8b":
            with gzip.open(original, "rb") as gz:
                interno = gz.read(4096)
            if interno[257:262] == b"ustar":
                return self._preparar_arquivos_tar(pasta, original, "r:gz")
            if interno[:5] == b"PGDMP":
                destino = pasta / "banco.dump"
                with gzip.open(original, "rb") as gz, open(destino, "wb") as saida:
                    shutil.copyfileobj(gz, saida, 1 << 20)
                return self._analisar_dump(destino)
            return self._preparar_sql(pasta, original, compactado=True)
        if cabeca[257:262] == b"ustar":
            return self._preparar_arquivos_tar(pasta, original, "r:")
        return self._preparar_sql(pasta, original, compactado=False)

    def _analisar_dump(self, arq: Path) -> dict:
        pg = self.cfg.postgres_container
        if not pg:
            raise ErroBackup("Este portal nao tem o PostgreSQL do servidor para ler o arquivo.")
        servidor = self.versao_servidor()
        try:
            saida = docker_cli.de_arquivo(["exec", "-i", pg, "pg_restore", "--list"], arq, timeout=600)
        except docker_cli.DockerError as exc:
            msg = str(exc)
            if "unsupported version" in msg or "versao nao suportada" in msg.lower():
                raise ErroBackup(
                    f"Este .dump foi gerado por um PostgreSQL mais novo que o do servidor ({servidor}) e nao "
                    "pode ser lido. Gere de novo em SQL puro, que funciona entre versoes: "
                    "pg_dump -h localhost -U <usuario> -d <banco> --no-owner --no-privileges -f arquivo.sql") from exc
            raise ErroBackup(f"Nao foi possível ler o .dump (arquivo incompleto ou corrompido?): {msg[-300:]}") from exc
        origem = re.search(r"Dumped from database version:\s*(\d+)", saida)
        ferramenta = re.search(r"Dumped by pg_dump version:\s*(\d+)", saida)
        banco = re.search(r";\s+dbname:\s*(\S+)", saida)
        tabelas = _tabelas_no_indice(saida)
        return {"tipo": "banco", "formato": "dump", "preparado": arq.name, "tabelas": tabelas,
                "versao_origem": int(origem.group(1)) if origem else None,
                "versao_pg_dump": int(ferramenta.group(1)) if ferramenta else None,
                "banco_origem": banco.group(1) if banco else "", "versao_servidor": servidor,
                "avisos": [], "bytes": arq.stat().st_size}

    # Linhas de dono e permissao do pg_dump sem --no-owner/--no-privileges: os usuarios da
    # maquina de origem nao existem aqui, e quem manda no banco importado e o usuario da solucao.
    _LINHA_DONO = re.compile(r"^ALTER [A-Z ]+ .+ OWNER TO \S+;$")
    _LINHA_PERMISSAO = re.compile(r"^(GRANT|REVOKE) .+ (TO|FROM) \S+.*;$|^ALTER DEFAULT PRIVILEGES .+;$")

    def _preparar_sql(self, pasta: Path, original: Path, compactado: bool) -> dict:
        destino = pasta / "banco.sql"
        abrir = (lambda: gzip.open(original, "rt", encoding="utf-8", errors="strict", newline="")) if compactado \
            else (lambda: open(original, encoding="utf-8", errors="strict", newline=""))
        origem = ferramenta = None
        tabelas = linhas_dados = removidas = 0
        em_copy = False
        comeco: list[str] = []
        try:
            with abrir() as entrada, open(destino, "w", encoding="utf-8", newline="") as saida:
                for n, linha in enumerate(entrada):
                    if n < 40:
                        comeco.append(linha)
                        if "\x00" in linha:
                            raise ErroBackup("Isto nao parece um arquivo SQL (tem conteudo binario).")
                    sem_fim = linha.rstrip("\r\n")
                    if em_copy:
                        if sem_fim == "\\.":
                            em_copy = False
                        else:
                            linhas_dados += 1
                        saida.write(linha)
                        continue
                    if sem_fim.startswith("COPY ") and sem_fim.endswith("FROM stdin;"):
                        em_copy = True
                    elif sem_fim.startswith("CREATE TABLE "):
                        tabelas += 1
                    elif re.match(r"^\\(connect|c) ", sem_fim) or re.match(r"^CREATE DATABASE ", sem_fim):
                        raise ErroBackup("O arquivo cria ou troca de banco (foi gerado com -C/--create ou pelo "
                                         "pg_dumpall). Gere de novo so com o banco: pg_dump -d <banco> --no-owner "
                                         "--no-privileges -f arquivo.sql")
                    elif re.match(r"^(CREATE|ALTER) ROLE ", sem_fim):
                        raise ErroBackup("O arquivo cria usuarios do banco (pg_dumpall). Use o pg_dump de um banco so.")
                    elif self._LINHA_DONO.match(sem_fim) or self._LINHA_PERMISSAO.match(sem_fim):
                        removidas += 1
                        continue
                    if origem is None and (m := re.match(r"^-- Dumped from database version (\d+)", sem_fim)):
                        origem = int(m.group(1))
                    if ferramenta is None and (m := re.match(r"^-- Dumped by pg_dump version (\d+)", sem_fim)):
                        ferramenta = int(m.group(1))
                    saida.write(linha)
        except UnicodeDecodeError as exc:
            raise ErroBackup("O arquivo nao esta em UTF-8 ou nao e texto. Envie o SQL do pg_dump, um .dump "
                             "(pg_dump -Fc) ou um .zip/.tar.gz com arquivos.") from exc
        except (OSError, EOFError) as exc:
            raise ErroBackup(f"Nao foi possível ler o arquivo: {exc}") from exc
        texto = "".join(comeco)
        if "PostgreSQL database cluster dump" in texto:
            raise ErroBackup("Isto e um pg_dumpall (servidor inteiro). Use o pg_dump de um banco so.")
        if tabelas == 0:
            raise ErroBackup("Nao foi possível encontrar nenhuma tabela (CREATE TABLE) no arquivo. Envie o resultado do pg_dump do banco.")
        servidor = self.versao_servidor()
        avisos = []
        if ferramenta and servidor and ferramenta > servidor:
            avisos.append(f"Gerado pelo pg_dump {ferramenta}; o servidor usa o PostgreSQL {servidor}. Se algum comando nao existir, a importacao para e nada muda.")
        if removidas:
            avisos.append(f"{removidas} linha(s) de dono e permissoes da maquina de origem foram ignoradas: "
                          "no servidor, tudo fica do usuario da solucao.")
        if em_copy:
            raise ErroBackup("O arquivo termina no meio dos dados (incompleto). Gere e envie de novo.")
        return {"tipo": "banco", "formato": "sql", "preparado": destino.name, "tabelas": tabelas,
                "linhas_dados": linhas_dados, "versao_origem": origem, "versao_pg_dump": ferramenta,
                "versao_servidor": servidor, "avisos": avisos, "bytes": destino.stat().st_size}

    @staticmethod
    def _nome_seguro_arquivo(nome: str) -> bool:
        p = Path(nome.replace("\\", "/"))
        return bool(nome) and not p.is_absolute() and ".." not in p.parts and not nome.startswith("/")

    def _preparar_arquivos_tar(self, pasta: Path, original: Path, modo: str) -> dict:
        destino = pasta / "arquivos.tar.gz"
        n = total = 0
        try:
            with tarfile.open(original, modo) as tar:
                membros = tar.getmembers()
                nomes = {m.name.lstrip("./") for m in membros}
                if "toc.dat" in nomes:
                    raise ErroBackup("Isto e um pg_dump no formato tar (-Ft). Gere em -Fc (.dump) ou SQL puro.")
                for m in membros:
                    if not self._nome_seguro_arquivo(m.name) or not (m.isfile() or m.isdir()):
                        if m.issym() or m.islnk():
                            raise ErroBackup(f"O pacote tem atalhos (links), que nao sao aceitos: {m.name}")
                        if not (m.isfile() or m.isdir()):
                            raise ErroBackup(f"Item nao aceito no pacote: {m.name}")
                        raise ErroBackup(f"Caminho nao aceito no pacote: {m.name}")
                    if m.isfile():
                        n += 1
                        total += m.size
                if modo == "r:gz":
                    shutil.copyfile(original, destino)
                else:
                    with tarfile.open(destino, "w:gz") as novo:
                        for m in membros:
                            novo.addfile(m, tar.extractfile(m) if m.isfile() else None)
        except tarfile.TarError as exc:
            raise ErroBackup(f"Pacote .tar invalido: {exc}") from exc
        return self._info_arquivos(destino, n, total)

    def _preparar_arquivos_zip(self, pasta: Path, original: Path) -> dict:
        import zipfile

        destino = pasta / "arquivos.tar.gz"
        n = total = 0
        try:
            with zipfile.ZipFile(original) as z, tarfile.open(destino, "w:gz") as tar:
                for zi in z.infolist():
                    nome = zi.filename.replace("\\", "/")
                    if not self._nome_seguro_arquivo(nome):
                        raise ErroBackup(f"Caminho nao aceito no .zip: {zi.filename}")
                    ti = tarfile.TarInfo(nome.rstrip("/"))
                    ti.mtime = time.mktime(zi.date_time + (0, 0, -1))
                    if zi.is_dir():
                        ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                        tar.addfile(ti)
                        continue
                    ti.size, ti.mode = zi.file_size, 0o644
                    with z.open(zi) as dados:
                        tar.addfile(ti, dados)
                    n += 1
                    total += zi.file_size
        except zipfile.BadZipFile as exc:
            raise ErroBackup(f".zip invalido: {exc}") from exc
        return self._info_arquivos(destino, n, total)

    def _info_arquivos(self, destino: Path, n: int, total: int) -> dict:
        if n == 0:
            raise ErroBackup("O pacote nao tem nenhum arquivo.")
        return {"tipo": "arquivos", "formato": "tar.gz", "preparado": destino.name, "arquivos": n,
                "bytes_conteudo": total, "avisos": [], "bytes": destino.stat().st_size}

    def aplicar_importacao(self, iid: str, sid: str, chave: str = "", juntar: bool = False,
                           registrar: Callable[[str], None] = print) -> None:
        """Coloca o arquivo recebido no banco ou numa pasta de dados da solucao `sid`."""
        pasta = self._pasta_importacao(iid)
        info = self.importacao(iid)
        if info.get("estado") != "pronto":
            raise ErroBackup("O arquivo ainda nao foi analisado.")
        ger = self._ger()
        sol = ger.solucoes.get(sid)
        if sol is None:
            raise ErroBackup(f"Solucao desconhecida: {sid}")
        pg = self.cfg.postgres_container
        arq = pasta / info["preparado"]
        if info["tipo"] == "banco":
            if not sol.banco or not pg:
                raise ErroBackup(f"{sol.nome} nao usa o banco do servidor. Não ha onde importar.")
            guardado = ger.armazenamento.banco(sid) or {}
            if not guardado.get("senha"):
                raise ErroBackup(f"O banco de {sol.nome} ainda nao foi criado. Inicie a solucao uma vez (o portal cria o banco automaticamente) e importe depois.")
            nome_banco, dono = guardado.get("nome") or banco_pg.nomes(sid)[0], banco_pg.nomes(sid)[1]
        else:
            if chave not in sol.volumes:
                raise ErroBackup(f"Escolha uma pasta de dados de {sol.nome}: {', '.join(sol.volumes) or 'nenhuma'}.")
            nome_vol = sol.nome_volume(chave)

        registrar(f"Importando {info['arquivo']} em {sol.nome}")
        registrar("Backup de seguranca antes de importar")
        seg = self.fazer("antes-de-importar", lambda l: registrar("  " + l), somente=[sid])
        if not seg["ok"]:
            raise ErroBackup("O backup de seguranca teve erros. Nada foi importado. " + "; ".join(seg["erros"]))

        falha: Exception | None = None
        with Trava(self.pasta).segurar():
            info_c = docker_cli.container_inspect(sol.container)
            estava_no_ar = bool(info_c and info_c.get("State", {}).get("Running"))
            try:
                if info_c:
                    registrar(f"Parando {sol.container}")
                    docker_cli.stop(sol.container)
                if info["tipo"] == "banco":
                    banco_pg.garantir(pg, sid, guardado["senha"], registrar)
                    self._restaurar_banco(pg, arq, nome_banco, dono, registrar, formato=info["formato"])
                else:
                    self._restaurar_volume(arq, nome_vol, registrar, juntar=juntar)
            except Exception as exc:  # noqa: BLE001
                falha = exc
                registrar(f"ERRO: {exc}")
        if estava_no_ar:
            registrar(f"Iniciando {sid} de novo")
            tarefa = _TarefaRepasse("iniciar", registrar)
            ger._rodar(sol, tarefa)
            if tarefa.estado != "concluida" and falha is None:
                raise ErroBackup(f"Arquivo importado, mas a solucao nao subiu: {tarefa.erro}")
        else:
            registrar("A solucao estava parada e continua parada. Inicie quando quiser.")
        if falha is not None:
            raise ErroBackup(f"Importacao interrompida: {falha}. Os dados atuais nao foram trocados"
                             + (" (o banco so e trocado se a importacao inteira der certo)." if info["tipo"] == "banco"
                                else "; o backup de seguranca esta na lista."))
        shutil.rmtree(pasta, ignore_errors=True)
        registrar("Importacao concluida. O arquivo enviado foi apagado do servidor.")
        self.auditoria("Importou arquivo", f"{info['arquivo']} em {sid}" + (f" ({chave})" if chave else " (banco)"))

    # ======================================================================== agendador
    def proximo(self, agora: datetime | None = None) -> datetime | None:
        aj = self.ajustes()
        if not aj.ativo:
            return None
        tz = fuso(self.cfg)
        agora = agora or datetime.now(tz)
        candidatos = []
        for dias in (0, 1):
            d = (agora + timedelta(days=dias)).date()
            for h in aj.horarios:
                hh, mm = map(int, h.split(":"))
                slot = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
                if slot > agora:
                    candidatos.append(slot)
        return min(candidatos) if candidatos else None

    def _slot_devido(self, agora: datetime) -> datetime | None:
        """Ultimo horario agendado que ja passou (ate 12 h atras) e ainda nao foi feito."""
        aj = self.ajustes()
        if not aj.ativo:
            return None
        tz = agora.tzinfo
        slots = []
        for dias in (0, -1):
            d = (agora + timedelta(days=dias)).date()
            for h in aj.horarios:
                hh, mm = map(int, h.split(":"))
                slot = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
                if slot <= agora:
                    slots.append(slot)
        if not slots:
            return None
        slot = max(slots)
        if agora - slot > JANELA_ATRASO:
            return None
        if self.estado().get("ultimo_agendado") == slot.isoformat():
            return None
        return slot

    def iniciar_agendador(self) -> None:
        if self._agendador is not None:
            return

        def laco() -> None:
            time.sleep(20)  # deixa o portal subir
            while True:
                try:
                    agora = datetime.now(fuso(self.cfg))
                    slot = self._slot_devido(agora)
                    if slot and not (self.tarefa and self.tarefa.estado == "executando"):
                        self._atualizar_estado(ultimo_agendado=slot.isoformat())
                        print(f"[backup] backup agendado de {slot:%d/%m %H:%M}", flush=True)
                        t = self.em_segundo_plano("backup agendado",
                                                  lambda tarefa: self._rodar_backup(tarefa, "agendado"))
                        while t.estado == "executando":
                            time.sleep(5)
                except Exception as exc:  # noqa: BLE001 - o agendador nunca morre
                    print(f"[backup] erro no agendador: {exc}", flush=True)
                time.sleep(30)

        self._agendador = threading.Thread(target=laco, daemon=True, name="agendador-backup")
        self._agendador.start()

    def _rodar_backup(self, tarefa: Tarefa, motivo: str) -> None:
        tarefa.etapa = "Fazendo backup"
        man = self.fazer(motivo, tarefa.registrar)
        if not man["ok"]:
            tarefa.erro = f"{len(man['erros'])} item(ns) com erro: " + "; ".join(man["erros"])[:500]


class _TarefaRepasse(Tarefa):
    def __init__(self, acao: str, registrar: Callable[[str], None]):
        super().__init__(acao=acao)
        self._repassar = registrar

    def registrar(self, linha: str) -> None:
        super().registrar(linha)
        self._repassar("  " + str(linha))


def _tabelas_no_indice(saida: str) -> int:
    """Conta as tabelas no indice do pg_restore --list (linhas "123; 1259 16390 TABLE esquema nome dono")."""
    return sum(1 for linha in saida.splitlines() if re.match(r"^\d+; \d+ \d+ TABLE (?!DATA )", linha))


def _commit_portal() -> str:
    raiz = Path(__file__).resolve().parent.parent
    with contextlib.suppress(OSError):
        head = (raiz / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = raiz / ".git" / head[5:]
            if ref.exists():
                return ref.read_text().strip()[:7]
            packed = raiz / ".git" / "packed-refs"
            for linha in packed.read_text().splitlines() if packed.exists() else []:
                if linha.endswith(head[5:]):
                    return linha[:7]
        return head[:7]
    return ""


def resumo(man: dict) -> dict:
    """Versao enxuta do manifesto para a tela (sem sha256)."""
    return {
        "nome": man["nome"], "motivo": man.get("motivo"), "criado_em": man.get("criado_em"),
        "ok": man.get("ok"), "erros": man.get("erros", []), "bytes": man.get("bytes", 0),
        "duracao_s": man.get("duracao_s"), "parcial": man.get("parcial", False), "fixado": man.get("fixado", False),
        "solucoes": man.get("solucoes", []),
        "verificacao": man.get("verificacao"),
        "itens": [{k: v for k, v in i.items() if k != "sha256"} for i in man.get("itens", [])],
    }

