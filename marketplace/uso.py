"""Uso de recursos do servidor e de cada solucao (tela Uso).

Um coletor dentro do portal roda a cada `uso_intervalo_s` (padrao 60 s):

- da maquina (VM): memoria (/proc/meminfo), carga de CPU (/proc/loadavg), disco;
- de cada container do portal (solucoes + portal, Caddy e PostgreSQL): CPU e memoria
  (`docker stats`), mais reinicios e encerramentos por falta de memoria (viram eventos).

Guarda tudo em `dados/uso.db` (SQLite) por `uso_dias` dias (padrao 7). Os tamanhos em disco
(volumes, bancos, imagens, cache do Docker, backups) sao mais caros e sao medidos a cada 15 min.
Tudo so le: nada aqui muda containers, a nao ser `limpar_docker`, que so apaga cache e imagens
sem nome (nunca volumes nem imagens em uso).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import docker_cli
from .config import RAIZ, Config
from .manager import Gerenciador

INFRA = {"log20-portal": "Portal", "log20-caddy": "Proxy (Caddy)", "log20-postgres": "PostgreSQL do servidor",
         "log20-sb-studio": "Supabase Studio"}
# A pilha fixa da primeira versao (deploy/supabase, containers log20-supabase-*), se ainda estiver
# ligada numa VM: aparece somada numa linha so, para ficar claro quanto ela ocupa.
SUPABASE_ANTIGO = "log20-supabase"
# Pilha do Supabase de cada solucao (containers log20-sb-<id>-*): alvo "supabase:<id>".
ALVO_SUPABASE = "supabase:"
VOLUME_POSTGRES = "log20-postgres-dados"
TAMANHOS_A_CADA = 15 * 60

_UNIDADES = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2, "gb": 1000 ** 3,
             "gib": 1024 ** 3, "tb": 1000 ** 4, "tib": 1024 ** 4}


def bytes_de(texto: str) -> int:
    """'123.4MiB', '1.2GB', '0B' -> bytes."""
    m = re.match(r"^\s*([\d.]+)\s*([a-zA-Z]*)", str(texto))
    if not m:
        return 0
    return int(float(m.group(1)) * _UNIDADES.get(m.group(2).lower() or "b", 1))


def percentual(texto: str) -> float:
    with contextlib.suppress(ValueError):
        return float(str(texto).strip().rstrip("%") or 0)
    return 0.0


def maquina(pasta_disco: Path) -> dict:
    """Memoria, CPU e disco da VM (o /proc de um container mostra os numeros da maquina)."""
    mem = {}
    with contextlib.suppress(OSError):
        for linha in Path("/proc/meminfo").read_text().splitlines():
            nome, _, resto = linha.partition(":")
            mem[nome] = int(resto.split()[0]) * 1024
    carga = [0.0, 0.0, 0.0]
    with contextlib.suppress(OSError, ValueError):
        carga = [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    disco = shutil.disk_usage(pasta_disco)
    total = mem.get("MemTotal", 0)
    disponivel = mem.get("MemAvailable", mem.get("MemFree", 0))
    return {
        "mem_total": total, "mem_usada": max(0, total - disponivel),
        "swap_total": mem.get("SwapTotal", 0), "swap_usada": max(0, mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
        "cpus": os.cpu_count() or 1, "carga": carga,
        "disco_total": disco.total, "disco_usado": disco.total - disco.free, "disco_livre": disco.free,
    }


class Uso:
    def __init__(self, cfg: Config, gerenciador: Callable[[], Gerenciador],
                 pasta_backups: Callable[[], Path] | None = None):
        self.cfg = cfg
        self._ger = gerenciador
        self._pasta_backups = pasta_backups
        self.arquivo = cfg.pasta_dados / "uso.db"
        self._lock = threading.Lock()
        self._ultima: dict | None = None
        self._tamanhos: dict = {}
        self._tamanhos_em = 0.0
        self._reinicios: dict[str, int] = {}
        self._parado_oom: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self.erro = ""

    # ---------------- banco de amostras ----------------
    def _con(self) -> sqlite3.Connection:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.arquivo, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS amostra (ts INTEGER, alvo TEXT, cpu REAL, mem INTEGER, mem_limite INTEGER);
            CREATE INDEX IF NOT EXISTS amostra_ts ON amostra (ts);
            CREATE TABLE IF NOT EXISTS maquina (ts INTEGER PRIMARY KEY, mem_usada INTEGER, mem_total INTEGER,
                disco_usado INTEGER, disco_total INTEGER, carga REAL, cpus INTEGER);
            CREATE TABLE IF NOT EXISTS evento (ts INTEGER, alvo TEXT, tipo TEXT, detalhe TEXT);
            CREATE INDEX IF NOT EXISTS evento_ts ON evento (ts);
        """)
        return con

    # ---------------- coleta ----------------
    def _nomes(self, ger: Gerenciador) -> dict[str, str]:
        """nome do container -> id do alvo (id da solucao, ou o nome do container para a infra)."""
        from . import supabase as sb

        nomes = {sol.container: sid for sid, sol in ger.solucoes.items()}
        nomes.update({c: c for c in INFRA})
        for sid, sol in ger.solucoes.items():
            if sol.supabase:
                nomes.update({sb.container(sid, s): ALVO_SUPABASE + sid for s in sb.SERVICOS})
        return nomes

    def coletar(self) -> dict:
        """Uma amostra agora (grava no historico) e devolve o retrato atual."""
        ger = self._ger()
        nomes = self._nomes(ger)
        agora = int(time.time())
        maq = maquina(RAIZ)
        conteineres: dict[str, dict] = {}
        try:
            for s in docker_cli.estatisticas():
                nome = s.get("Name", "")
                alvo = nomes.get(nome) or (SUPABASE_ANTIGO if nome.startswith(SUPABASE_ANTIGO + "-") else None)
                if not alvo:
                    continue
                usado, _, limite = str(s.get("MemUsage", "")).partition("/")
                atual = {"cpu": percentual(s.get("CPUPerc")) / 100, "mem": bytes_de(usado),
                         "mem_limite": bytes_de(limite), "processos": int(s.get("PIDs") or 0)}
                if alvo in conteineres:  # varios containers no mesmo alvo (pilha do Supabase): soma
                    antes = conteineres[alvo]
                    atual = {"cpu": antes["cpu"] + atual["cpu"], "mem": antes["mem"] + atual["mem"],
                             "mem_limite": 0, "processos": antes["processos"] + atual["processos"]}
                conteineres[alvo] = atual
            self.erro = ""
        except docker_cli.DockerError as exc:
            self.erro = f"docker stats: {exc}"
        eventos = self._eventos(ger, agora)
        with self._lock, contextlib.closing(self._con()) as con, con:
            con.executemany("INSERT INTO amostra VALUES (?, ?, ?, ?, ?)",
                            [(agora, a, v["cpu"], v["mem"], v["mem_limite"]) for a, v in conteineres.items()])
            con.execute("INSERT OR REPLACE INTO maquina VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (agora, maq["mem_usada"], maq["mem_total"], maq["disco_usado"], maq["disco_total"],
                         maq["carga"][0], maq["cpus"]))
            con.executemany("INSERT INTO evento VALUES (?, ?, ?, ?)", eventos)
        self._ultima = {"ts": agora, "maquina": maq, "conteineres": conteineres}
        return self._ultima

    def _eventos(self, ger: Gerenciador, agora: int) -> list[tuple]:
        """Reinicios (RestartCount subiu) e encerramentos por falta de memoria desde a ultima amostra."""
        eventos = []
        for sid, sol in ger.solucoes.items():
            info = docker_cli.container_inspect(sol.container)
            if not info:
                continue
            estado = info.get("State", {})
            reinicios = int(info.get("RestartCount") or 0)
            antes = self._reinicios.get(sid)
            self._reinicios[sid] = reinicios
            # Na primeira olhada (portal acabou de subir) so guarda a contagem: sem evento repetido.
            if antes is not None and reinicios > antes:
                oom = bool(estado.get("OOMKilled"))
                eventos.append((agora, sid, "falta_memoria" if oom else "reinicio",
                                f"{reinicios - antes} reinício(s)" + (" por falta de memória" if oom else "")))
            elif antes is not None and not estado.get("Running") and estado.get("OOMKilled") \
                    and self._parado_oom.get(sid) != estado.get("FinishedAt"):
                eventos.append((agora, sid, "falta_memoria", "parou por falta de memória"))
            if estado.get("OOMKilled") and not estado.get("Running"):
                self._parado_oom[sid] = estado.get("FinishedAt")
        return eventos

    def atual(self) -> dict:
        """Retrato mais recente (coleta na hora se o ultimo tiver mais de um intervalo)."""
        if not self._ultima or time.time() - self._ultima["ts"] > self.cfg.uso_intervalo_s:
            return self.coletar()
        return self._ultima

    # ---------------- tamanhos em disco ----------------
    def tamanhos(self, forcar: bool = False) -> dict:
        if forcar or not self._tamanhos or time.time() - self._tamanhos_em > TAMANHOS_A_CADA:
            self._tamanhos = self._medir_tamanhos()
            self._tamanhos_em = time.time()
        return {**self._tamanhos, "medido_em": int(self._tamanhos_em)}

    def _medir_tamanhos(self) -> dict:
        ger = self._ger()
        r: dict = {"volumes": {}, "bancos": {}, "imagens": {}, "docker": {}, "backups": 0, "erros": []}
        # Volumes: um container so, com todos os volumes montados so para leitura, mede com du.
        try:
            existentes = {v.get("Name") for v in docker_cli.listar_json(["volume", "ls"])}
            from . import supabase as sb

            vols = sorted(({sol.nome_volume(c) for sol in ger.solucoes.values() for c in sol.volumes}
                           | {v for sol in ger.solucoes.values() if sol.supabase for v in sb.volumes(sol.id)}) & existentes)
            if VOLUME_POSTGRES in existentes:
                vols.append(VOLUME_POSTGRES)
            if vols:
                args = ["run", "--rm", "--network", "none"]
                for i, v in enumerate(vols):
                    args += ["-v", f"{v}:/v/{i}:ro"]
                saida = docker_cli.rodar([*args, "--entrypoint", "du", self.cfg.backup_imagem, "-sk",
                                          *[f"/v/{i}" for i in range(len(vols))]], timeout=300)
                for linha in saida.splitlines():
                    kb, _, caminho = linha.partition("\t")
                    with contextlib.suppress(ValueError, IndexError):
                        r["volumes"][vols[int(caminho.rsplit("/", 1)[1])]] = int(kb) * 1024
        except docker_cli.DockerError as exc:
            r["erros"].append(f"volumes: {exc}")
        # Bancos do PostgreSQL do servidor.
        if self.cfg.postgres_container:
            ok, saida = docker_cli.executar_em(self.cfg.postgres_container, [
                "psql", "-U", "postgres", "-tAF", "\t", "-c",
                "SELECT datname, pg_database_size(datname) FROM pg_database WHERE NOT datistemplate"], timeout=60)
            if ok:
                for linha in saida.splitlines():
                    nome, _, tam = linha.partition("\t")
                    with contextlib.suppress(ValueError):
                        r["bancos"][nome] = int(tam)
            else:
                r["erros"].append(f"bancos: {saida[-200:]}")
        # Imagens e resumo do Docker (imagens, containers, volumes, cache de construcao).
        try:
            for img in docker_cli.listar_json(["image", "ls"]):
                nome = f"{img.get('Repository')}:{img.get('Tag')}"
                r["imagens"][nome] = bytes_de(img.get("Size", "0"))
            for linha in docker_cli.listar_json(["system", "df"]):
                tipo = str(linha.get("Type", ""))
                r["docker"][tipo] = {"total": bytes_de(linha.get("Size", "0")),
                                     "recuperavel": bytes_de(str(linha.get("Reclaimable", "0")).split(" ")[0])}
        except docker_cli.DockerError as exc:
            r["erros"].append(f"imagens: {exc}")
        if self._pasta_backups:
            with contextlib.suppress(OSError):
                pasta = self._pasta_backups()
                r["backups"] = sum(f.stat().st_size for f in pasta.rglob("*") if f.is_file()) if pasta.is_dir() else 0
        return r

    def limpar_docker(self) -> dict:
        """Apaga o cache de construcao e as imagens sem nome (restos de versoes antigas).
        Nunca apaga volumes (dados), containers nem imagens com nome."""
        antes = shutil.disk_usage(RAIZ).free
        saidas = []
        for args in (["builder", "prune", "-f"], ["image", "prune", "-f"]):
            try:
                saidas.append(docker_cli.rodar(args, timeout=900).strip().splitlines()[-1:])
            except docker_cli.DockerError as exc:
                saidas.append([f"erro: {exc}"])
        liberado = max(0, shutil.disk_usage(RAIZ).free - antes)
        self.tamanhos(forcar=True)
        return {"liberado": liberado, "detalhes": [s[0] for s in saidas if s]}

    # ---------------- historico ----------------
    def historico(self, horas: float) -> dict:
        """Series agregadas em ~180 pontos: memoria (pico do intervalo) e CPU (media) por alvo."""
        fim = int(time.time())
        inicio = fim - int(horas * 3600)
        passo = max(self.cfg.uso_intervalo_s, int(horas * 3600 / 180))
        series: dict[str, dict] = {}
        with self._lock, contextlib.closing(self._con()) as con:
            for alvo, balde, cpu, mem in con.execute(
                    "SELECT alvo, (ts / ?) * ?, AVG(cpu), MAX(mem) FROM amostra WHERE ts >= ? "
                    "GROUP BY alvo, (ts / ?) ORDER BY 2", (passo, passo, inicio, passo)):
                s = series.setdefault(alvo, {"t": [], "cpu": [], "mem": []})
                s["t"].append(balde)
                s["cpu"].append(round(cpu, 3))
                s["mem"].append(mem)
            maq = {"t": [], "mem_usada": [], "mem_total": [], "disco_usado": [], "disco_total": [], "carga": []}
            for linha in con.execute(
                    "SELECT (ts / ?) * ?, MAX(mem_usada), MAX(mem_total), MAX(disco_usado), MAX(disco_total), "
                    "AVG(carga) FROM maquina WHERE ts >= ? GROUP BY ts / ? ORDER BY 1", (passo, passo, inicio, passo)):
                for chave, valor in zip(maq, linha, strict=True):
                    maq[chave].append(round(valor, 2) if chave == "carga" else valor)
            picos = {alvo: {"mem": mem, "cpu": cpu} for alvo, mem, cpu in con.execute(
                "SELECT alvo, MAX(mem), MAX(cpu) FROM amostra WHERE ts >= ? GROUP BY alvo", (fim - 86400,))}
            eventos = [{"ts": ts, "alvo": a, "tipo": tp, "detalhe": d} for ts, a, tp, d in con.execute(
                "SELECT ts, alvo, tipo, detalhe FROM evento WHERE ts >= ? ORDER BY ts DESC LIMIT 50", (fim - 7 * 86400,))]
        return {"inicio": inicio, "fim": fim, "passo": passo, "series": series, "maquina": maq,
                "picos_24h": picos, "eventos": eventos}

    def eventos_desde(self, ts: int) -> list[dict]:
        """Reinicios e encerramentos por falta de memoria a partir de `ts` (usado pelos alertas)."""
        with self._lock, contextlib.closing(self._con()) as con:
            return [{"ts": t, "alvo": a, "tipo": tp, "detalhe": d} for t, a, tp, d in con.execute(
                "SELECT ts, alvo, tipo, detalhe FROM evento WHERE ts >= ? ORDER BY ts", (ts,))]

    def limpar_antigos(self) -> None:
        limite = int(time.time()) - self.cfg.uso_dias * 86400
        with self._lock, contextlib.closing(self._con()) as con, con:
            for tabela in ("amostra", "maquina", "evento"):
                con.execute(f"DELETE FROM {tabela} WHERE ts < ?", (limite,))  # noqa: S608 - nome fixo

    # ---------------- coletor ----------------
    def iniciar(self) -> None:
        if self._thread is not None:
            return

        def laco() -> None:
            time.sleep(10)
            ultima_limpeza = 0.0
            while True:
                inicio = time.time()
                try:
                    self.coletar()
                    if time.time() - self._tamanhos_em > TAMANHOS_A_CADA:
                        self.tamanhos(forcar=True)
                    if time.time() - ultima_limpeza > 3600:
                        self.limpar_antigos()
                        ultima_limpeza = time.time()
                except Exception as exc:  # noqa: BLE001 - o coletor nunca morre
                    self.erro = str(exc)
                    print(f"[uso] erro na coleta: {exc}", flush=True)
                time.sleep(max(5.0, self.cfg.uso_intervalo_s - (time.time() - inicio)))

        self._thread = threading.Thread(target=laco, daemon=True, name="coletor-uso")
        self._thread.start()
