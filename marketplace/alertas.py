"""Alertas de saude do servidor (tela Alertas).

Um vigia dentro do portal confere a cada minuto:

- **sistema**: uma solucao que deveria estar no ar (nao parada de proposito) esta com problema
  ou nao responde ha alguns minutos (inclui o Supabase do sistema);
- **reinicios**: o container reiniciou varias vezes em pouco tempo ou parou por falta de memoria;
- **memoria**: uma solucao passou de um percentual do limite de memoria por alguns minutos;
- **disco**: pouco espaco livre no disco do portal ou da pasta de backup;
- **backup**: o ultimo backup falhou, nao foi feito, ou faz tempo demais desde o ultimo completo;
- **copia**: a segunda copia falhou ou a pasta dela esta no mesmo disco da VM (pasta de rede
  desmontada: a "copia" estaria caindo no disco da propria VM);
- **certificado**: com HTTPS, o certificado vence em poucos dias, venceu ou tem problema (arquivo
  faltando, chave errada, nome que nao bate) e o portal ficou em HTTP.

Cada problema vira um alerta com inicio e fim (historico em `dados/alertas.db`). O aviso sai uma
vez quando comeca e outra quando resolve, com lembrete opcional a cada X horas, por e-mail
(SMTP). Os ajustes ficam em `dados/alertas.json` (com a senha do SMTP: so o portal le).

Nao vigia o proprio portal: se a VM ou o portal cair, ninguem avisa. Para isso, `/api/saude`
responde sem login e pode ser consultado por um monitor de fora (ex.: o da TI).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import certificado, correio, docker_cli
from . import supabase as sb
from .config import RAIZ, Config

TIPOS = {
    "sistema": "Sistema fora do ar",
    "reinicios": "Sistema reiniciando",
    "memoria": "Memória perto do limite",
    "disco": "Pouco espaço em disco",
    "backup": "Backup",
    "copia": "Segunda cópia do backup",
    "certificado": "Certificado HTTPS",
}
INTERVALO_S = 60
INICIO_TOLERANCIA_S = 10 * 60      # depois que o portal sobe, os sistemas ainda estao subindo
INICIANDO_TOLERANCIA_S = 10 * 60   # "iniciando" so vira alerta depois disso
SINCRONIZANDO_MAX_S = 3 * 3600     # marca do sincronizar esquecida (processo morto) deixa de valer
JANELA_REINICIOS_S = 30 * 60
ROTULOS_ESTADO = {"rodando": "no ar", "iniciando": "iniciando", "ocupado": "em andamento", "instalado": "instalado, parado",
                  "parado": "parado", "nao_instalado": "não instalado", "com_problema": "COM PROBLEMA"}
MARCA_SINCRONIZANDO = "sincronizando"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SMTP_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class ErroAlertas(Exception):
    """Mensagem pronta para a tela."""


# ======================================================================== ajustes

@dataclass
class AjustesAlertas:
    ativo: bool = True
    tipos: dict[str, bool] = field(default_factory=lambda: {t: True for t in TIPOS})
    minutos_fora: int = 3
    memoria_pct: int = 90
    memoria_minutos: int = 5
    disco_min_gb: float = 10.0
    disco_min_pct: int = 10
    backup_horas: int = 26
    certificado_dias: int = 30
    lembrar_horas: int = 12
    avisar_resolvido: bool = True
    # Canais
    email_ativo: bool = False
    email_para: list[str] = field(default_factory=list)
    smtp_origem: str = "proprio"   # "proprio", "servidor" (.env) ou o id de um sistema com Supabase
    smtp_servidor: str = ""
    smtp_porta: str = "587"
    smtp_usuario: str = ""
    smtp_senha: str = ""
    smtp_remetente: str = ""
    smtp_nome: str = "Portal LOG20"

    def publico(self) -> dict:
        d = asdict(self)
        d["tem_senha"] = bool(d.pop("smtp_senha"))
        return d

    @classmethod
    def de_dict(cls, dados: dict, atual: AjustesAlertas, origens: set[str]) -> AjustesAlertas:
        """Valida o que veio da tela. Senha em branco mantem a guardada (mesmo servidor e usuario)."""
        aj = cls()

        def inteiro(nome: str, minimo: int, maximo: int) -> int:
            try:
                v = int(dados.get(nome, getattr(atual, nome)))
            except (TypeError, ValueError):
                raise ErroAlertas(f"{nome}: informe um número.") from None
            if not minimo <= v <= maximo:
                raise ErroAlertas(f"{nome}: use um número entre {minimo} e {maximo}.")
            return v

        aj.ativo = bool(dados.get("ativo", atual.ativo))
        tipos = dados.get("tipos") or {}
        aj.tipos = {t: bool(tipos.get(t, atual.tipos.get(t, True))) for t in TIPOS}
        aj.minutos_fora = inteiro("minutos_fora", 1, 120)
        aj.memoria_pct = inteiro("memoria_pct", 50, 100)
        aj.memoria_minutos = inteiro("memoria_minutos", 1, 120)
        aj.disco_min_pct = inteiro("disco_min_pct", 0, 90)
        aj.backup_horas = inteiro("backup_horas", 2, 24 * 14)
        aj.certificado_dias = inteiro("certificado_dias", 1, 180)
        aj.lembrar_horas = inteiro("lembrar_horas", 0, 24 * 7)
        try:
            aj.disco_min_gb = float(dados.get("disco_min_gb", atual.disco_min_gb))
        except (TypeError, ValueError):
            raise ErroAlertas("Espaço mínimo: informe um número.") from None
        if not 0 <= aj.disco_min_gb <= 100000:
            raise ErroAlertas("Espaço mínimo inválido.")
        aj.avisar_resolvido = bool(dados.get("avisar_resolvido", atual.avisar_resolvido))

        aj.email_ativo = bool(dados.get("email_ativo", atual.email_ativo))
        para = dados.get("email_para", atual.email_para)
        if isinstance(para, str):
            para = re.split(r"[,;\s]+", para)
        aj.email_para = [p.strip() for p in para or [] if p and p.strip()][:20]
        for p in aj.email_para:
            if not EMAIL_RE.match(p):
                raise ErroAlertas(f"E-mail inválido: {p}")
        aj.smtp_origem = str(dados.get("smtp_origem", atual.smtp_origem) or "proprio")
        if aj.smtp_origem not in origens:
            raise ErroAlertas("Origem do e-mail desconhecida.")
        aj.smtp_servidor = str(dados.get("smtp_servidor", atual.smtp_servidor) or "").strip()
        aj.smtp_porta = str(dados.get("smtp_porta", atual.smtp_porta) or "587").strip()
        aj.smtp_usuario = str(dados.get("smtp_usuario", atual.smtp_usuario) or "").strip()[:200]
        aj.smtp_remetente = str(dados.get("smtp_remetente", atual.smtp_remetente) or "").strip()
        aj.smtp_nome = " ".join(str(dados.get("smtp_nome", atual.smtp_nome) or "").split())[:80]
        senha = str(dados.get("smtp_senha") or "")
        if not senha and aj.smtp_servidor == atual.smtp_servidor and aj.smtp_usuario == atual.smtp_usuario:
            senha = atual.smtp_senha
        aj.smtp_senha = senha[:500]
        if aj.smtp_origem == "proprio" and (aj.smtp_servidor or aj.email_ativo):
            if not SMTP_HOST_RE.match(aj.smtp_servidor):
                raise ErroAlertas("Servidor de e-mail inválido (ex.: smtp.office365.com).")
            if not aj.smtp_porta.isdigit() or not 1 <= int(aj.smtp_porta) <= 65535:
                raise ErroAlertas("Porta do e-mail inválida (normalmente 587 ou 465).")
            if not EMAIL_RE.match(aj.smtp_remetente):
                raise ErroAlertas("Informe o e-mail do remetente (ex.: alertas@log20.com.br).")
        if aj.email_ativo and not aj.email_para:
            raise ErroAlertas("Informe pelo menos um destinatário para os alertas por e-mail.")
        return aj


def _gravar_json_privado(arq: Path, dados: dict) -> None:
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(dados, fh, indent=2, ensure_ascii=False)
    tmp.replace(arq)


# ======================================================================== vigia

@dataclass
class Problema:
    chave: str
    tipo: str
    alvo: str
    titulo: str
    detalhe: str
    espera_s: int = 0   # quanto tempo o problema precisa durar antes de virar alerta


class Alertas:
    def __init__(self, cfg: Config, gerenciador: Callable, backups, uso):
        self.cfg = cfg
        self._ger = gerenciador
        self._backups = backups
        self._uso = uso
        self.arquivo = cfg.pasta_dados / "alertas.db"
        self.arquivo_ajustes = cfg.pasta_dados / "alertas.json"
        self._lock = threading.Lock()
        self._desde: dict[str, float] = {}   # chave -> quando o problema foi visto pela primeira vez
        self._inicio = time.time()
        self._thread: threading.Thread | None = None
        self.ultima_verificacao = 0.0
        self.erro = ""          # erro do proprio vigia
        self.erro_envio = ""    # ultimo erro ao enviar um aviso
        self.erro_envio_em = 0.0

    # ---------------- ajustes ----------------
    def ajustes(self) -> AjustesAlertas:
        try:
            dados = json.loads(self.arquivo_ajustes.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return AjustesAlertas()
        padrao = AjustesAlertas()
        campos = {k: dados[k] for k in AjustesAlertas.__dataclass_fields__ if k in dados}
        aj = AjustesAlertas(**campos)
        aj.tipos = {t: bool(aj.tipos.get(t, True)) for t in TIPOS} if isinstance(aj.tipos, dict) else padrao.tipos
        return aj

    def origens_smtp(self) -> list[dict]:
        """Onde buscar o SMTP: proprio, o do .env ou o do login de um sistema com Supabase."""
        lista = [{"id": "proprio", "nome": "Preencher aqui", "servidor": ""}]
        env = sb.smtp_global()
        if env["servidor"]:
            lista.append({"id": "servidor", "nome": "O do servidor (.env)", "servidor": env["servidor"]})
        for sid, sol in sorted(self._ger().solucoes.items(), key=lambda x: x[1].nome.lower()):
            if sol.supabase:
                lg = sb.ler_login(self.cfg.pasta_dados, sid)
                if lg.smtp_servidor:
                    lista.append({"id": sid, "nome": f"O mesmo do login de {sol.nome}", "servidor": lg.smtp_servidor})
        return lista

    def validar(self, dados: dict) -> AjustesAlertas:
        return AjustesAlertas.de_dict(dados, self.ajustes(), {o["id"] for o in self.origens_smtp()})

    def salvar(self, dados: dict) -> AjustesAlertas:
        aj = self.validar(dados)
        _gravar_json_privado(self.arquivo_ajustes, asdict(aj))
        return aj

    def smtp(self, aj: AjustesAlertas) -> dict | None:
        if aj.smtp_origem == "servidor":
            s = sb.smtp_global()
            return s if s["servidor"] else None
        if aj.smtp_origem != "proprio":
            lg = sb.ler_login(self.cfg.pasta_dados, aj.smtp_origem)
            s, origem = sb.smtp_efetivo(lg)
            return s if origem != "nenhum" else None
        if not aj.smtp_servidor:
            return None
        return {"servidor": aj.smtp_servidor, "porta": aj.smtp_porta, "usuario": aj.smtp_usuario,
                "senha": aj.smtp_senha, "remetente": aj.smtp_remetente, "nome": aj.smtp_nome}

    # ---------------- banco ----------------
    def _con(self) -> sqlite3.Connection:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.arquivo, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS alerta (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chave TEXT NOT NULL, tipo TEXT NOT NULL,
                alvo TEXT NOT NULL, titulo TEXT NOT NULL, detalhe TEXT NOT NULL,
                inicio INTEGER NOT NULL, fim INTEGER, avisado INTEGER, lembrado INTEGER);
            CREATE INDEX IF NOT EXISTS alerta_ativo ON alerta (fim, chave);
            CREATE INDEX IF NOT EXISTS alerta_inicio ON alerta (inicio);
        """)
        return con

    def ativos(self) -> list[dict]:
        with self._lock, contextlib.closing(self._con()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM alerta WHERE fim IS NULL ORDER BY inicio DESC")]

    def historico(self, limite: int = 100) -> list[dict]:
        with self._lock, contextlib.closing(self._con()) as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM alerta WHERE fim IS NOT NULL ORDER BY fim DESC LIMIT ?", (limite,))]

    def total_ativos(self) -> int:
        with self._lock, contextlib.closing(self._con()) as con:
            return con.execute("SELECT count(*) FROM alerta WHERE fim IS NULL").fetchone()[0]

    def limpar_antigos(self, dias: int = 180) -> None:
        with self._lock, contextlib.closing(self._con()) as con, con:
            con.execute("DELETE FROM alerta WHERE fim IS NOT NULL AND fim < ?", (int(time.time()) - dias * 86400,))

    # ---------------- verificacoes ----------------
    def _suspensos(self) -> set[str]:
        """Tipos que nao abrem nem fecham alerta agora (situacoes esperadas de instabilidade)."""
        suspensos: set[str] = set()
        if time.time() - self._inicio < INICIO_TOLERANCIA_S:
            suspensos |= {"sistema", "reinicios", "memoria"}
        marca = self.cfg.pasta_dados / MARCA_SINCRONIZANDO
        with contextlib.suppress(OSError):
            if time.time() - marca.stat().st_mtime < SINCRONIZANDO_MAX_S:
                suspensos |= {"sistema", "reinicios"}   # deploy/atualizar.sh esta subindo os sistemas
        tarefa = getattr(self._backups, "tarefa", None)
        if tarefa is not None and tarefa.estado == "executando":
            suspensos.add("sistema")   # o backup e a restauracao param o Supabase por alguns segundos
        return suspensos

    def _verificar_sistemas(self, aj: AjustesAlertas) -> list[Problema]:
        ger = self._ger()
        problemas = []
        for sid, sol in ger.solucoes.items():
            if sol.externo and sol.externo.startswith(sb.STUDIO + ":"):
                continue  # o Studio so liga quando alguem abre
            try:
                st = ger.status(sol)
            except docker_cli.DockerError as exc:
                return [Problema("sistema:docker", "sistema", "docker", "O Docker não responde",
                                 f"O portal não consegue falar com o Docker: {exc}", aj.minutos_fora * 60)]
            if st["estado"] == "com_problema":
                problemas.append(Problema(f"sistema:{sid}", "sistema", sid, f"{sol.nome} fora do ar",
                                          st["detalhe"], aj.minutos_fora * 60))
            elif st["estado"] == "iniciando":
                problemas.append(Problema(f"sistema:{sid}", "sistema", sid, f"{sol.nome} fora do ar",
                                          f"Não terminou de iniciar ({st['detalhe']}).",
                                          max(INICIANDO_TOLERANCIA_S, aj.minutos_fora * 60)))
        return problemas

    def _verificar_reinicios(self) -> list[Problema]:
        ger = self._ger()
        problemas = []
        desde = int(time.time()) - JANELA_REINICIOS_S
        por_alvo: dict[str, list[dict]] = {}
        for ev in self._uso.eventos_desde(desde):
            por_alvo.setdefault(ev["alvo"], []).append(ev)
        for alvo, eventos in por_alvo.items():
            sol = ger.solucoes.get(alvo)
            if sol is None:
                continue
            n = 0
            for ev in eventos:
                m = re.match(r"(\d+)", ev["detalhe"] or "")
                n += int(m.group(1)) if m else 1
            oom = any(ev["tipo"] == "falta_memoria" for ev in eventos)
            if n >= 3 or oom:
                detalhe = (f"{n} reinício(s) nos últimos {JANELA_REINICIOS_S // 60} minutos"
                           + (", por falta de memória: aumente o limite em Uso." if oom else ". Veja os logs do sistema."))
                problemas.append(Problema(f"reinicios:{alvo}", "reinicios", alvo, f"{sol.nome} reiniciando", detalhe))
        return problemas

    def _verificar_memoria(self, aj: AjustesAlertas) -> list[Problema]:
        ger = self._ger()
        problemas = []
        atual = self._uso.atual()
        for alvo, v in (atual.get("conteineres") or {}).items():
            sol = ger.solucoes.get(alvo)
            if sol is None or not v.get("mem_limite"):
                continue
            pct = v["mem"] / v["mem_limite"] * 100
            if pct >= aj.memoria_pct:
                problemas.append(Problema(
                    f"memoria:{alvo}", "memoria", alvo, f"{sol.nome} perto do limite de memória",
                    f"Usando {pct:.0f}% do limite ({_tamanho(v['mem'])} de {_tamanho(v['mem_limite'])}). "
                    "Se chegar a 100%, o sistema reinicia: aumente o limite em Uso.", aj.memoria_minutos * 60))
        return problemas

    def _verificar_disco(self, aj: AjustesAlertas) -> list[Problema]:
        problemas = []
        vistos: set[int] = set()
        for rotulo, pasta in (("do portal", RAIZ), ("dos backups", self._backups.pasta)):
            try:
                dev = os.stat(pasta).st_dev
                uso = shutil.disk_usage(pasta)
            except OSError:
                continue
            if dev in vistos:
                continue
            vistos.add(dev)
            pct = uso.free / uso.total * 100 if uso.total else 100
            if uso.free < aj.disco_min_gb * 1024 ** 3 or pct < aj.disco_min_pct:
                problemas.append(Problema(
                    f"disco:{dev}", "disco", str(pasta), "Pouco espaço em disco",
                    f"Disco {rotulo} ({pasta}): {_tamanho(uso.free)} livres de {_tamanho(uso.total)} ({pct:.0f}%). "
                    "Veja o que ocupa espaço na tela Uso."))
        return problemas

    def _verificar_backup(self, aj: AjustesAlertas) -> list[Problema]:
        e = self._backups.estado()
        ajb = self._backups.ajustes()
        problemas = []
        copia_erro = e.get("ultimo_copia_erro") or ""
        erros = [x for x in (e.get("ultimo_erros") or []) if x and x != copia_erro]
        if e.get("ultimo") and not e.get("ultimo_ok") and erros:
            problemas.append(Problema("backup:falhou", "backup", "backup", "O último backup teve erros",
                                      f"{e['ultimo']}: " + "; ".join(erros)[:900]))
        ultimo_em = _data(e.get("ultimo_em"))
        erro_em = _data(e.get("ultimo_erro_em"))
        if e.get("ultimo_erro") and erro_em and (not ultimo_em or erro_em > ultimo_em):
            problemas.append(Problema("backup:nao_feito", "backup", "backup", "O backup não foi feito", e["ultimo_erro"]))
        sucesso = _data(e.get("ultimo_sucesso_em"))
        if ajb.ativo and sucesso and time.time() - sucesso > aj.backup_horas * 3600:
            horas = (time.time() - sucesso) / 3600
            problemas.append(Problema("backup:atrasado", "backup", "backup", "Backup atrasado",
                                      f"O último backup completo foi há {horas:.0f} horas "
                                      f"({datetime.fromtimestamp(sucesso).strftime('%d/%m/%Y %H:%M')})."))
        return problemas

    def _verificar_copia(self) -> list[Problema]:
        copia = self.cfg.backup_copia
        if copia is None or not self._backups.ajustes().copiar:
            return []
        problemas = []
        e = self._backups.estado()
        if e.get("ultimo_copia_erro"):
            problemas.append(Problema("copia:falhou", "copia", "copia", "A segunda cópia do backup falhou",
                                      e["ultimo_copia_erro"]))
        with contextlib.suppress(OSError):
            if os.stat(copia).st_dev == os.stat(RAIZ).st_dev:
                problemas.append(Problema(
                    "copia:local", "copia", "copia", "Segunda cópia gravando no disco da VM",
                    f"A pasta da segunda cópia ({copia}) está no mesmo disco da VM: a pasta de rede "
                    "provavelmente não está montada. Na VM, rode 'sudo mount -a' e depois "
                    "'bash deploy/atualizar.sh'.", INTERVALO_S * 2))
        return problemas

    def _verificar_certificado(self, aj: AjustesAlertas) -> list[Problema]:
        info = certificado.ler(self.cfg)
        if not info["configurado"]:
            return []
        if not info["ok"]:
            return [Problema("certificado:erro", "certificado", "certificado", "HTTPS desligado: problema no certificado",
                             f"{info['erro']} Enquanto isso, os sistemas respondem só em http://, pelo IP.")]
        if info["dias"] is not None and info["dias"] < aj.certificado_dias:
            ate = datetime.fromtimestamp(info["valido_ate"]).strftime("%d/%m/%Y")
            return [Problema("certificado:vencendo", "certificado", "certificado", "Certificado HTTPS perto de vencer",
                             f"O certificado de {info['nome']} vence em {ate} ({info['dias']} dia(s)). Peça o novo à TI, "
                             "troque certificado.pem e chave.pem em dados/certificados e clique em Recarregar catálogo.")]
        return []

    def verificar(self) -> None:
        """Uma rodada: abre, mantem e fecha alertas, e envia os avisos."""
        aj = self.ajustes()
        agora = int(time.time())
        if not aj.ativo:
            self._desde.clear()
            return
        suspensos = self._suspensos()
        verificacoes = {
            "sistema": lambda: self._verificar_sistemas(aj),
            "reinicios": self._verificar_reinicios,
            "memoria": lambda: self._verificar_memoria(aj),
            "disco": lambda: self._verificar_disco(aj),
            "backup": lambda: self._verificar_backup(aj),
            "copia": self._verificar_copia,
            "certificado": lambda: self._verificar_certificado(aj),
        }
        vistos: dict[str, Problema] = {}
        falharam: set[str] = set()
        for tipo, funcao in verificacoes.items():
            if not aj.tipos.get(tipo, True):
                continue
            try:
                for p in funcao():
                    vistos[p.chave] = p
            except Exception as exc:  # noqa: BLE001 - uma verificacao com erro nao derruba as outras
                falharam.add(tipo)
                print(f"[alertas] erro ao verificar {tipo}: {exc}", flush=True)
        for chave in list(self._desde):
            if chave not in vistos:
                del self._desde[chave]
        for chave in vistos:
            self._desde.setdefault(chave, agora)

        avisos: list[tuple[str, dict]] = []
        with self._lock, contextlib.closing(self._con()) as con, con:
            ativos = {r["chave"]: dict(r) for r in con.execute("SELECT * FROM alerta WHERE fim IS NULL")}
            for chave, a in ativos.items():
                tipo = a["tipo"]
                if chave in vistos:
                    p = vistos[chave]
                    if p.detalhe != a["detalhe"]:
                        con.execute("UPDATE alerta SET detalhe = ? WHERE id = ?", (p.detalhe, a["id"]))
                        a["detalhe"] = p.detalhe
                    ultimo = a["lembrado"] or a["avisado"] or a["inicio"]
                    if aj.lembrar_horas and agora - ultimo >= aj.lembrar_horas * 3600:
                        con.execute("UPDATE alerta SET lembrado = ? WHERE id = ?", (agora, a["id"]))
                        avisos.append(("lembrete", a))
                    continue
                # Desligado nos ajustes: fecha sem avisar. Suspenso ou com erro na verificacao: mantem.
                if not aj.tipos.get(tipo, True):
                    con.execute("UPDATE alerta SET fim = ? WHERE id = ?", (agora, a["id"]))
                    continue
                if tipo in suspensos or tipo in falharam:
                    continue
                con.execute("UPDATE alerta SET fim = ? WHERE id = ?", (agora, a["id"]))
                a["fim"] = agora
                if aj.avisar_resolvido and a["avisado"]:
                    avisos.append(("resolvido", a))
            for chave, p in vistos.items():
                if chave in ativos or p.tipo in suspensos:
                    continue
                if agora - self._desde[chave] < p.espera_s:
                    continue
                inicio = int(self._desde[chave])
                cur = con.execute(
                    "INSERT INTO alerta (chave, tipo, alvo, titulo, detalhe, inicio, avisado) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chave, p.tipo, p.alvo, p.titulo, p.detalhe, inicio, agora))
                avisos.append(("novo", {"id": cur.lastrowid, "chave": chave, "tipo": p.tipo, "alvo": p.alvo,
                                        "titulo": p.titulo, "detalhe": p.detalhe, "inicio": inicio, "fim": None}))
        self.ultima_verificacao = time.time()
        for tipo_aviso, a in avisos:
            self._avisar(aj, tipo_aviso, a)

    # ---------------- avisos ----------------
    def _texto(self, tipo_aviso: str, a: dict) -> tuple[str, str]:
        inicio = datetime.fromtimestamp(a["inicio"]).strftime("%d/%m/%Y %H:%M") if a.get("inicio") else ""
        if tipo_aviso == "resolvido":
            duracao = _duracao(a["fim"] - a["inicio"])
            assunto = f"✔ Resolvido: {a['titulo']}"
            corpo = f"{a['titulo']}: resolvido. Durou {duracao} (desde {inicio})."
        elif tipo_aviso == "lembrete":
            duracao = _duracao(int(time.time()) - a["inicio"])
            assunto = f"⚠ Continua: {a['titulo']}"
            corpo = f"{a['titulo']} há {duracao} (desde {inicio}).\n{a['detalhe']}"
        elif tipo_aviso == "teste":
            assunto = "Teste dos alertas do Portal LOG20"
            corpo = "Este é um aviso de teste. Se chegou, os alertas vão chegar por aqui também."
        else:
            assunto = f"⚠ {a['titulo']}"
            corpo = f"{a['titulo']} desde {inicio}.\n{a['detalhe']}"
        portal = self.cfg.url_publica or self.cfg.url_base
        return f"[Portal LOG20] {assunto}", f"{corpo}\n\nPortal: {portal}/#/admin/alertas\n"

    def _avisar(self, aj: AjustesAlertas, tipo_aviso: str, a: dict) -> list[str]:
        """Envia o e-mail, se ligado. Devolve os erros (tambem guardados para a tela)."""
        assunto, corpo = self._texto(tipo_aviso, a)
        erros = []
        if aj.email_ativo and aj.email_para:
            smtp = self.smtp(aj)
            try:
                if smtp is None:
                    raise correio.ErroCorreio("nenhum servidor de e-mail configurado")
                correio.enviar(smtp, aj.email_para, assunto, corpo)
            except correio.ErroCorreio as exc:
                erros.append(f"E-mail: {exc}")
        if erros:
            self.erro_envio, self.erro_envio_em = "; ".join(erros), time.time()
            print(f"[alertas] aviso nao enviado: {self.erro_envio}", flush=True)
        elif tipo_aviso != "teste":
            self.erro_envio = ""
        return erros

    def situacao(self) -> tuple[str, str]:
        """Assunto e texto do e-mail "situacao atual": alertas ativos, sistemas, disco e backup."""
        agora = int(time.time())
        aj = self.ajustes()
        ativos = self.ativos()
        def data(ts: float) -> str:
            return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")
        partes = [f"Situação do servidor em {data(agora)}.", ""]

        partes.append(f"ALERTAS ATIVOS ({len(ativos)})")
        if ativos:
            for a in ativos:
                partes.append(f"- {a['titulo']} (há {_duracao(agora - a['inicio'])}, desde {data(a['inicio'])})")
                partes.append(f"  {a['detalhe']}")
        else:
            partes.append("- Nenhum.")
        if not aj.ativo:
            partes.append("  Atenção: os alertas estão desligados (Alertas → Ajustes).")
        partes.append("")

        partes.append("SISTEMAS")
        ger = self._ger()
        for sol in sorted(ger.solucoes.values(), key=lambda s: s.nome.lower()):
            try:
                st = ger.status(sol)
                partes.append(f"- {sol.nome}: {ROTULOS_ESTADO.get(st['estado'], st['estado'])}"
                              + (f" ({st['detalhe']})" if st["estado"] == "com_problema" else ""))
            except Exception as exc:  # noqa: BLE001 - um sistema com erro nao impede o resto
                partes.append(f"- {sol.nome}: não foi possível conferir ({exc})")
        partes.append("")

        partes.append("DISCO")
        vistos: set[int] = set()
        for rotulo, pasta in (("Portal", RAIZ), ("Backups", self._backups.pasta)):
            try:
                dev = os.stat(pasta).st_dev
                uso = shutil.disk_usage(pasta)
            except OSError as exc:
                partes.append(f"- {rotulo} ({pasta}): não foi possível ler ({exc})")
                continue
            if dev in vistos:
                continue
            vistos.add(dev)
            pct = uso.free / uso.total * 100 if uso.total else 0
            partes.append(f"- {rotulo} ({pasta}): {_tamanho(uso.free)} livres de {_tamanho(uso.total)} ({pct:.0f}%)")
        partes.append("")

        partes.append("BACKUP")
        e = self._backups.estado()
        ajb = self._backups.ajustes()
        sucesso = _data(e.get("ultimo_sucesso_em"))
        partes.append(f"- Último completo: {data(sucesso) if sucesso else 'nenhum ainda'}")
        if e.get("ultimo"):
            partes.append(f"- Último feito: {e['ultimo']} " + ("(sem erros)" if e.get("ultimo_ok") else
                          "(com erros: " + "; ".join(e.get("ultimo_erros") or [])[:400] + ")"))
        partes.append(f"- Automático: {'ligado, às ' + ', '.join(ajb.horarios) if ajb.ativo else 'DESLIGADO'}")
        if self.cfg.backup_copia is None:
            partes.append("- Segunda cópia: não configurada")
        else:
            partes.append(f"- Segunda cópia: {'ligada' if ajb.copiar else 'desligada'}, em {self.cfg.backup_copia}")

        cert = certificado.ler(self.cfg)
        if cert["configurado"]:
            partes.append("")
            partes.append("HTTPS")
            if cert["ok"]:
                partes.append(f"- {cert['nome']}: ligado; certificado válido até "
                              f"{datetime.fromtimestamp(cert['valido_ate']).strftime('%d/%m/%Y')} ({cert['dias']} dia(s))")
            else:
                partes.append(f"- {cert['nome']}: DESLIGADO ({cert['erro']})")

        assunto = (f"Situação atual: {len(ativos)} alerta(s) ativo(s)" if ativos else "Situação atual: nenhum alerta ativo")
        portal = self.cfg.url_publica or self.cfg.url_base
        return f"[Portal LOG20] {assunto}", "\n".join(partes) + f"\n\nPortal: {portal}/#/admin/alertas\n"

    def enviar_situacao(self) -> str:
        """Envia agora o e-mail com a situacao atual para os destinatarios salvos nos ajustes."""
        aj = self.ajustes()
        if not aj.email_para:
            raise ErroAlertas("Informe os destinatários em Alertas → Ajustes → E-mail e salve.")
        smtp = self.smtp(aj)
        if smtp is None:
            raise ErroAlertas("Nenhum servidor de e-mail configurado (Alertas → Ajustes → E-mail).")
        assunto, corpo = self.situacao()
        try:
            correio.enviar(smtp, aj.email_para, assunto, corpo)
        except correio.ErroCorreio as exc:
            raise ErroAlertas(str(exc)) from None
        return "Situação atual enviada para " + ", ".join(aj.email_para) + "."

    def testar(self, dados: dict) -> str:
        """Envia um e-mail de teste com os valores da tela (ainda sem salvar)."""
        aj = self.validar(dados)
        if not (aj.email_ativo and aj.email_para):
            raise ErroAlertas("Ligue o envio por e-mail e informe os destinatários para testar.")
        erros = self._avisar(aj, "teste", {})
        if erros:
            raise ErroAlertas("; ".join(erros))
        return "E-mail de teste enviado para " + ", ".join(aj.email_para) + "."

    # ---------------- vigia em segundo plano ----------------
    def iniciar(self) -> None:
        if self._thread is not None:
            return

        def laco() -> None:
            time.sleep(30)
            ultima_limpeza = 0.0
            while True:
                inicio = time.time()
                try:
                    self.verificar()
                    self.erro = ""
                    if time.time() - ultima_limpeza > 86400:
                        self.limpar_antigos()
                        ultima_limpeza = time.time()
                except Exception as exc:  # noqa: BLE001 - o vigia nunca morre
                    self.erro = str(exc)
                    print(f"[alertas] erro: {exc}", flush=True)
                time.sleep(max(10.0, INTERVALO_S - (time.time() - inicio)))

        self._thread = threading.Thread(target=laco, daemon=True, name="vigia-alertas")
        self._thread.start()


def _data(texto: str | None) -> float | None:
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto).timestamp()
    except ValueError:
        return None


def _tamanho(n: float) -> str:
    gb = n / 1024 ** 3
    if gb >= 1:
        return f"{gb:.1f} GB".replace(".", ",")
    return f"{n / 1024 ** 2:.0f} MB"


def _duracao(s: int) -> str:
    s = max(0, int(s))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} dia(s) e {s % 86400 // 3600} h"
