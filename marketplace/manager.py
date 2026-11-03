"""Orquestra o ciclo de vida das solucoes: instalar (build), iniciar, parar, logs."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import docker_cli, fontes
from .catalog import Solucao
from .config import Config

ACOES = {"instalar", "iniciar", "parar", "reconstruir", "remover"}


@dataclass
class Tarefa:
    acao: str
    estado: str = "executando"  # executando | concluida | falhou
    etapa: str = ""
    log: deque = field(default_factory=lambda: deque(maxlen=3000))
    erro: str | None = None
    inicio: float = field(default_factory=time.time)
    fim: float | None = None

    def registrar(self, linha: str) -> None:
        self.log.append(linha)


class Armazenamento:
    """Configuracoes e segredos por solucao, em `dados/<id>.json` (fora do git)."""

    def __init__(self, pasta: Path):
        self.pasta = pasta
        self.pasta.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _arquivo(self, sid: str) -> Path:
        return self.pasta / f"{sid}.json"

    def ler(self, sid: str) -> dict:
        arq = self._arquivo(sid)
        if not arq.exists():
            return {"segredos": {}, "configuracoes": {}}
        dados = json.loads(arq.read_text(encoding="utf-8"))
        dados.setdefault("segredos", {})
        dados.setdefault("configuracoes", {})
        return dados

    def gravar(self, sid: str, dados: dict) -> None:
        arq = self._arquivo(sid)
        tmp = arq.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(arq)

    def segredos(self, sol: Solucao) -> dict[str, str]:
        """Gera na primeira vez e reaproveita depois (trocar o JWT derrubaria as sessoes)."""
        with self._lock:
            dados = self.ler(sol.id)
            mudou = False
            for nome in sol.segredos:
                if not dados["segredos"].get(nome):
                    dados["segredos"][nome] = secrets.token_urlsafe(48)
                    mudou = True
            if mudou:
                self.gravar(sol.id, dados)
            return {n: dados["segredos"][n] for n in sol.segredos}

    def configuracoes(self, sol: Solucao) -> dict[str, str]:
        salvos = self.ler(sol.id)["configuracoes"]
        return {c.nome: str(salvos.get(c.nome, c.padrao)) for c in sol.configuracoes}

    def atualizar_configuracoes(self, sol: Solucao, valores: dict[str, object]) -> None:
        validos = {c.nome: c for c in sol.configuracoes}
        with self._lock:
            dados = self.ler(sol.id)
            for nome, valor in valores.items():
                conf = validos.get(nome)
                if conf is None:
                    raise ValueError(f"Configuracao desconhecida: {nome}")
                if conf.tipo == "booleano":
                    texto = "true" if valor in (True, "true", "1", "on", 1) else "false"
                else:
                    texto = "" if valor is None else str(valor)
                    # Segredo em branco significa "manter o valor atual".
                    if conf.tipo == "segredo" and texto == "":
                        continue
                dados["configuracoes"][nome] = texto
            self.gravar(sol.id, dados)

    def versao_instalada(self, sid: str) -> dict | None:
        return self.ler(sid).get("versao_codigo")

    def registrar_versao(self, sid: str, versao: dict) -> None:
        with self._lock:
            dados = self.ler(sid)
            dados["versao_codigo"] = versao
            self.gravar(sid, dados)

    def url_publica(self, sid: str) -> str:
        return str(self.ler(sid).get("url_publica") or "")

    def definir_url_publica(self, sid: str, url: str) -> None:
        url = (url or "").strip().rstrip("/")
        if url and not re.match(r"^https?://[^\s/]+(:\d+)?(/[^\s]*)?$", url):
            raise ValueError("Endereco publico deve comecar com http:// ou https://")
        with self._lock:
            dados = self.ler(sid)
            dados["url_publica"] = url
            self.gravar(sid, dados)

    def limpar_segredo(self, sol: Solucao, nome: str) -> None:
        with self._lock:
            dados = self.ler(sol.id)
            dados["configuracoes"].pop(nome, None)
            self.gravar(sol.id, dados)


class Gerenciador:
    def __init__(self, solucoes: dict[str, Solucao], armazenamento: Armazenamento, config: Config):
        self.solucoes = solucoes
        self.armazenamento = armazenamento
        self.config = config
        self.tarefas: dict[str, Tarefa] = {}
        self._lock = threading.Lock()

    # ---------- enderecos ----------
    def url(self, sol: Solucao) -> str:
        """Endereco que os usuarios usam (pode ser um dominio atras de proxy reverso)."""
        return self.armazenamento.url_publica(sol.id) or f"{self.config.url_base}:{sol.porta_host}"

    def url_interna(self, sol: Solucao) -> str:
        if self.config.rede_docker and not self.config.publicar_portas:
            # Portal e solucao na mesma rede do Docker: fala direto com o container.
            return f"http://{sol.container}:{sol.porta_container}"
        return f"http://{self.config.host_interno}:{sol.porta_host}"

    # ---------- ambiente ----------
    def ambiente(self, sol: Solucao) -> dict[str, str]:
        url = self.url(sol)
        host = urlparse(url).hostname or ""
        env = {k: v.replace("{url}", url).replace("{host}", host) for k, v in sol.ambiente.items()}
        for nome, valor in self.armazenamento.configuracoes(sol).items():
            if valor != "":
                env[nome] = valor
        env.update(self.armazenamento.segredos(sol))
        return env

    # ---------- status ----------
    def status(self, sol: Solucao) -> dict:
        tarefa = self.tarefas.get(sol.id)
        if tarefa and tarefa.estado == "executando":
            return {"estado": "ocupado", "detalhe": tarefa.etapa or tarefa.acao}

        info = docker_cli.container_inspect(sol.container)
        if info is None:
            if docker_cli.image_exists(sol.imagem):
                return {"estado": "instalado", "detalhe": "Pronto para iniciar"}
            return {"estado": "nao_instalado", "detalhe": "Ainda nao instalado"}

        estado = info.get("State", {})
        saude = (estado.get("Health") or {}).get("Status")
        if estado.get("Running"):
            # Com HEALTHCHECK na imagem, o proprio Docker diz se a aplicacao responde.
            if saude == "healthy" or (saude is None and self._responde(sol)):
                return {"estado": "rodando", "detalhe": "No ar"}
            if saude is None:
                return {"estado": "iniciando", "detalhe": "Aguardando resposta"}
            if saude == "starting":
                return {"estado": "iniciando", "detalhe": "Iniciando"}
            return {"estado": "com_problema", "detalhe": "Health check falhando - veja os logs"}
        if estado.get("Restarting"):
            return {"estado": "com_problema", "detalhe": "Reiniciando em loop - veja os logs"}
        codigo = estado.get("ExitCode")
        detalhe = "Parado" if codigo in (0, 137, 143) else f"Parou com erro (codigo {codigo})"
        return {"estado": "parado" if codigo in (0, 137, 143) else "com_problema", "detalhe": detalhe}

    def _responde(self, sol: Solucao) -> bool:
        try:
            with urllib.request.urlopen(self.url_interna(sol) + sol.caminho_saude, timeout=3) as resp:
                return resp.status < 500
        except (urllib.error.URLError, OSError):
            return False

    # ---------- acoes ----------
    def executar(self, sol: Solucao, acao: str) -> Tarefa:
        if acao not in ACOES:
            raise ValueError(f"Acao invalida: {acao}")
        with self._lock:
            atual = self.tarefas.get(sol.id)
            if atual and atual.estado == "executando":
                raise RuntimeError(f"Ja existe uma operacao em andamento: {atual.acao}")
            tarefa = Tarefa(acao=acao)
            self.tarefas[sol.id] = tarefa
        threading.Thread(target=self._rodar, args=(sol, tarefa), daemon=True).start()
        return tarefa

    def _rodar(self, sol: Solucao, tarefa: Tarefa) -> None:
        try:
            if tarefa.acao == "instalar":
                self._build(sol, tarefa)
            elif tarefa.acao == "iniciar":
                if not docker_cli.image_exists(sol.imagem):
                    self._build(sol, tarefa)
                self._iniciar(sol, tarefa)
            elif tarefa.acao == "reconstruir":
                self._build(sol, tarefa)
                self._iniciar(sol, tarefa)
            elif tarefa.acao == "parar":
                tarefa.etapa = "Parando"
                tarefa.registrar(f"$ docker stop {sol.container}")
                docker_cli.stop(sol.container)
                tarefa.registrar("Container parado. Os dados foram mantidos.")
            elif tarefa.acao == "remover":
                tarefa.etapa = "Removendo"
                docker_cli.remove(sol.container)
                tarefa.registrar("Container removido. Imagem e dados (volumes) foram mantidos.")
            tarefa.estado = "concluida"
        except Exception as exc:  # noqa: BLE001 - qualquer falha vira mensagem para o usuario
            tarefa.estado = "falhou"
            tarefa.erro = str(exc)
            tarefa.registrar(f"ERRO: {exc}")
        finally:
            tarefa.etapa = ""
            tarefa.fim = time.time()

    def _build(self, sol: Solucao, tarefa: Tarefa) -> None:
        versao = None
        if sol.repositorio:
            tarefa.etapa = "Baixando o codigo do repositorio"
            pasta, versao = fontes.atualizar(
                sol.repositorio, sol.ref, self.config.pasta_dados / "fontes", tarefa.registrar,
                token=self.config.git_token, token_host=self.config.git_host,
            )
            tarefa.registrar(f"Codigo no commit {versao.commit}: {versao.resumo}")
            contexto = (pasta / sol.subpasta).resolve()
            if contexto != pasta.resolve() and pasta.resolve() not in contexto.parents:
                raise docker_cli.DockerError("A subpasta do manifesto aponta para fora do repositorio.")
        else:
            contexto = sol.contexto_build
        dockerfile = (contexto / sol.dockerfile_nome).resolve()
        if not dockerfile.exists():
            raise docker_cli.DockerError(f"Dockerfile nao encontrado em {dockerfile}")
        tarefa.etapa = "Construindo imagem (a primeira vez leva alguns minutos)"
        tarefa.registrar(f"$ docker build -t {sol.imagem} {contexto}")
        docker_cli.build(contexto, dockerfile, sol.imagem, tarefa.registrar)
        tarefa.registrar("Imagem construida com sucesso.")
        if versao:
            info = {"commit": versao.commit, "resumo": versao.resumo, "data": versao.data, "ref": sol.ref}
            # A imagem pode ser compartilhada: registra a versao em todas as solucoes que a usam.
            for outra in self.solucoes.values():
                if outra.imagem == sol.imagem:
                    self.armazenamento.registrar_versao(outra.id, info)

    def _iniciar(self, sol: Solucao, tarefa: Tarefa) -> None:
        tarefa.etapa = "Iniciando container"
        # Recria o container para aplicar configuracoes novas; os volumes preservam os dados.
        docker_cli.remove(sol.container)
        tarefa.registrar(f"$ docker run {sol.container} -> {self.url(sol)}")
        cid = docker_cli.run_container(
            name=sol.container,
            image=sol.imagem,
            host_port=sol.porta_host,
            container_port=sol.porta_container,
            bind=self.config.bind_solucoes,
            publicar=self.config.publicar_portas,
            rede=self.config.rede_docker,
            env=self.ambiente(sol),
            volumes=sol.volumes_docker(),
            labels={"log20.portal": "1", "log20.solucao": sol.id},
        )
        tarefa.registrar(f"Container {cid[:12]} criado. Aguardando a aplicacao responder...")
        tarefa.etapa = "Aguardando a aplicacao responder"
        prazo = time.time() + 120
        while time.time() < prazo:
            info = docker_cli.container_inspect(sol.container) or {}
            estado = info.get("State", {})
            if not estado.get("Running"):
                self._diagnostico(sol, tarefa, estado)
                raise docker_cli.DockerError("O container parou logo apos iniciar. Veja o diagnostico acima.")
            saude = (estado.get("Health") or {}).get("Status")
            if saude == "healthy" or (saude is None and self._responde(sol)):
                tarefa.registrar(f"Pronto! Acesse {self.url(sol)}{sol.caminho_abrir}")
                return
            if saude == "unhealthy":
                self._diagnostico(sol, tarefa, estado)
                raise docker_cli.DockerError("A aplicacao subiu mas o health check falhou. Veja o diagnostico acima.")
            time.sleep(2)
        tarefa.registrar("A aplicacao ainda nao respondeu; ela pode estar terminando de iniciar. Veja os logs.")

    def _diagnostico(self, sol: Solucao, tarefa: Tarefa, estado: dict) -> None:
        """Copia para o log da operacao o motivo da falha e o fim do log da aplicacao."""
        tarefa.registrar("")
        tarefa.registrar("----- diagnostico -----")
        if not estado.get("Running"):
            tarefa.registrar(f"Codigo de saida: {estado.get('ExitCode')} {estado.get('Error') or ''}".strip())
        for item in ((estado.get("Health") or {}).get("Log") or [])[-2:]:
            saida = (item.get("Output") or "").strip() or "(sem saida)"
            tarefa.registrar(f"Health check (codigo {item.get('ExitCode')}): {saida}")
        tarefa.registrar("Ultimas linhas da aplicacao:")
        for linha in (docker_cli.logs(sol.container, tail=40) or "(nenhuma)").splitlines():
            tarefa.registrar("  " + linha)
        tarefa.registrar("-----------------------")

    def logs(self, sol: Solucao) -> dict:
        tarefa = self.tarefas.get(sol.id)
        app_logs = docker_cli.logs(sol.container) if docker_cli.container_inspect(sol.container) else ""
        return {
            "operacao": None if tarefa is None else {
                "acao": tarefa.acao,
                "estado": tarefa.estado,
                "erro": tarefa.erro,
                "log": "\n".join(tarefa.log),
            },
            "aplicacao": app_logs,
        }
