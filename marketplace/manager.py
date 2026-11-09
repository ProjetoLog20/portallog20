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

from . import banco as banco_pg
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

    def banco(self, sid: str) -> dict | None:
        """Banco no PostgreSQL do servidor criado para a solucao: {nome, usuario, senha, configurado}."""
        return self.ler(sid).get("banco")

    def banco_personalizado(self, sol: Solucao, campos) -> bool:
        """True se algum campo do banco tem valor proprio (nem vazio, nem o padrao do manifesto)."""
        padroes = {c.nome: c.padrao for c in sol.configuracoes}
        salvos = self.ler(sol.id)["configuracoes"]
        return any(salvos.get(n) not in (None, "", padroes.get(n, "")) for n in campos)

    def banco_em_uso(self, sol: Solucao, esperado: dict[str, str]) -> bool:
        """True se as configuracoes ainda sao as que o portal preencheu com o banco do servidor."""
        salvos = self.ler(sol.id)["configuracoes"]
        return all(salvos.get(n) == v for n, v in esperado.items())

    def registrar_banco(self, sol: Solucao, cred, valores_config: dict[str, str]) -> list[str]:
        """Guarda as credenciais e, na primeira vez, preenche as configuracoes do banco.

        So preenche se o admin ainda nao apontou a solucao para outro banco (todas as
        configuracoes do banco vazias ou com o valor padrao). Depois disso o portal nao mexe
        mais nelas: se o admin trocar o servidor ou apagar os campos, a escolha dele fica.
        Devolve os nomes preenchidos agora.
        """
        with self._lock:
            dados = self.ler(sol.id)
            anterior = dados.get("banco") or {}
            preenchidas: list[str] = []
            salvos = dados["configuracoes"]
            if not anterior.get("configurado") and not self.banco_personalizado(sol, valores_config):
                for nome, valor in valores_config.items():
                    salvos[nome] = valor
                    preenchidas.append(nome)
            dados["banco"] = {"nome": cred.nome, "usuario": cred.usuario, "senha": cred.senha,
                              "servidor": cred.host,
                              "configurado": bool(anterior.get("configurado") or preenchidas)}
            self.gravar(sol.id, dados)
            return preenchidas

    def recursos(self, sid: str) -> dict:
        """Limites escolhidos na tela para a solucao: {'memoria_mb': int|None, 'cpus': float|None}.
        None = segue o manifesto/padrao do servidor; 0 = sem limite."""
        r = self.ler(sid).get("recursos") or {}
        return {"memoria_mb": r.get("memoria_mb"), "cpus": r.get("cpus")}

    def salvar_recursos(self, sid: str, memoria_mb: int | None, cpus: float | None) -> None:
        with self._lock:
            dados = self.ler(sid)
            if memoria_mb is None and cpus is None:
                dados.pop("recursos", None)
            else:
                dados["recursos"] = {"memoria_mb": memoria_mb, "cpus": cpus}
            self.gravar(sid, dados)

    def restaurar_banco(self, sol: Solucao) -> bool:
        """Volta as configuracoes do banco para o PostgreSQL do servidor.

        Com o banco ja criado, regrava os campos com as credenciais guardadas; sem ele, apaga os
        campos para o proximo Iniciar criar o banco e preencher. Devolve True se ja havia banco.
        """
        with self._lock:
            dados = self.ler(sol.id)
            guardado = dados.get("banco") or {}
            salvos = dados["configuracoes"]
            if guardado.get("nome"):
                cred = banco_pg.Credenciais(host=guardado.get("servidor", ""), porta=banco_pg.PORTA,
                                            nome=guardado["nome"], usuario=guardado["usuario"],
                                            senha=guardado["senha"])
                salvos.update(banco_pg.preencher(sol.banco, cred))
                guardado["configurado"] = True
            else:
                for nome in sol.banco:
                    salvos.pop(nome, None)
            self.gravar(sol.id, dados)
            return bool(guardado.get("nome"))

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
            if estado.get("OOMKilled"):
                return {"estado": "com_problema", "detalhe": "Reiniciando por falta de memoria - aumente o limite em Uso"}
            return {"estado": "com_problema", "detalhe": "Reiniciando em loop - veja os logs"}
        if estado.get("OOMKilled"):
            return {"estado": "com_problema", "detalhe": "Parou por falta de memoria - aumente o limite em Uso"}
        codigo = estado.get("ExitCode")
        detalhe = "Parado" if codigo in (0, 137, 143) else f"Parou com erro (codigo {codigo})"
        return {"estado": "parado" if codigo in (0, 137, 143) else "com_problema", "detalhe": detalhe}

    def _responde(self, sol: Solucao) -> bool:
        try:
            with urllib.request.urlopen(self.url_interna(sol) + sol.caminho_saude, timeout=3) as resp:
                return resp.status < 500
        except urllib.error.HTTPError as erro:
            # Respondeu (ex.: 401/403/404 de uma tela de login): a aplicacao esta no ar.
            return erro.code < 500
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
        dockerfile = sol.dockerfile_portal or (contexto / sol.dockerfile_nome).resolve()
        if sol.dockerfile_portal:
            tarefa.registrar("Usando o Dockerfile guardado pelo portal (o repositorio nao precisa ter um).")
        if not dockerfile.exists():
            raise docker_cli.DockerError(f"Dockerfile nao encontrado em {dockerfile}")
        tarefa.etapa = "Construindo imagem (a primeira vez leva alguns minutos)"
        tarefa.registrar(f"$ docker build -t {sol.imagem} {contexto}")
        # Arquivos auxiliares do Dockerfile do portal (ex.: portal-saude) vem da pasta do manifesto.
        contextos = {"portal": sol.dockerfile_portal.parent} if sol.dockerfile_portal else None
        docker_cli.build(contexto, dockerfile, sol.imagem, tarefa.registrar, contextos)
        tarefa.registrar("Imagem construida com sucesso.")
        if versao:
            info = {"commit": versao.commit, "resumo": versao.resumo, "data": versao.data, "ref": sol.ref}
            # A imagem pode ser compartilhada: registra a versao em todas as solucoes que a usam.
            for outra in self.solucoes.values():
                if outra.imagem == sol.imagem:
                    self.armazenamento.registrar_versao(outra.id, info)

    def _preparar_banco(self, sol: Solucao, tarefa: Tarefa) -> None:
        """Garante o banco da solucao no PostgreSQL do servidor ([banco] no manifesto)."""
        if not self.config.postgres_container:
            tarefa.registrar("Banco: este portal nao tem PostgreSQL do servidor configurado; "
                             "a solucao usa o banco das configuracoes (ou o local).")
            return
        guardado = self.armazenamento.banco(sol.id) or {}
        # Com outro banco nas configuracoes (ex.: Supabase), o do servidor nao e usado nem exigido.
        if guardado.get("configurado"):
            anterior = banco_pg.Credenciais(host=guardado.get("servidor", self.config.postgres_container),
                                            porta=banco_pg.PORTA, nome=guardado["nome"],
                                            usuario=guardado["usuario"], senha=guardado["senha"])
            if not self.armazenamento.banco_em_uso(sol, banco_pg.preencher(sol.banco, anterior)):
                atuais = self.armazenamento.configuracoes(sol)
                if any(not atuais.get(n) for n in sol.banco):
                    tarefa.registrar("Banco: ha campos do banco vazios. Para voltar ao banco do servidor, "
                                     "use 'Usar o banco do servidor' em Configuracoes e inicie de novo.")
                else:
                    tarefa.registrar("Banco: as configuracoes apontam para outro banco; o banco do servidor nao e usado.")
                return
        elif self.armazenamento.banco_personalizado(sol, sol.banco):
            tarefa.registrar("Banco: as configuracoes ja apontam para outro banco e foram mantidas.")
            return
        tarefa.etapa = "Preparando o banco de dados"
        cred, _ = banco_pg.garantir(self.config.postgres_container, sol.id, guardado.get("senha"),
                                    tarefa.registrar)
        preenchidas = self.armazenamento.registrar_banco(sol, cred, banco_pg.preencher(sol.banco, cred))
        if preenchidas:
            tarefa.registrar(f"Banco: configuracoes preenchidas com o banco do servidor: {', '.join(preenchidas)}.")

    def _iniciar(self, sol: Solucao, tarefa: Tarefa) -> None:
        valores = self.armazenamento.configuracoes(sol)
        faltando = [c.rotulo for c in sol.configuracoes if c.obrigatoria and not valores.get(c.nome)]
        if faltando:
            raise docker_cli.DockerError(
                f"Preencha em Configuracoes antes de iniciar: {', '.join(faltando)}. Depois clique em Iniciar."
            )
        if sol.banco:
            self._preparar_banco(sol, tarefa)
        tarefa.etapa = "Iniciando container"
        # Recria o container para aplicar configuracoes novas; os volumes preservam os dados.
        docker_cli.remove(sol.container)
        lim = self.limites(sol)
        tarefa.registrar(f"$ docker run {sol.container} -> {self.url(sol)}"
                         f" (memoria: {_texto_mem(lim['memoria_mb'])}, CPU: {_texto_cpus(lim['cpus'])})")
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
            memoria_mb=lim["memoria_mb"],
            cpus=lim["cpus"],
            processos=lim["processos"],
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

    # ---------- limites de memoria e CPU ----------
    def limites(self, sol: Solucao) -> dict:
        """Limites efetivos e de onde vieram: tela > manifesto > padrao do servidor."""
        escolha = self.armazenamento.recursos(sol.id)
        resultado = {}
        for chave, manifesto, padrao in (("memoria_mb", sol.recursos_memoria_mb, self.config.limite_memoria_mb),
                                         ("cpus", sol.recursos_cpus, self.config.limite_cpus)):
            if escolha.get(chave) is not None:
                resultado[chave], origem = escolha[chave], "portal"
            elif manifesto is not None:
                resultado[chave], origem = manifesto, "manifesto"
            else:
                resultado[chave], origem = padrao, "padrao"
            resultado[f"origem_{chave}"] = origem
        resultado["processos"] = self.config.limite_processos
        return resultado

    def aplicar_limites(self, sol: Solucao) -> str:
        """Aplica os limites atuais no container em execucao (sem reiniciar). Devolve um aviso."""
        info = docker_cli.container_inspect(sol.container)
        if not (info and info.get("State", {}).get("Running")):
            return "Vale quando a solucao for iniciada."
        lim = self.limites(sol)
        atual_mem = (info.get("HostConfig") or {}).get("Memory") or 0
        if not lim["memoria_mb"] and atual_mem:
            return "Para tirar o limite de memoria, clique em Reiniciar na solucao."
        try:
            docker_cli.atualizar_limites(sol.container, lim["memoria_mb"], lim["cpus"])
        except docker_cli.DockerError as exc:
            return f"Nao deu para aplicar agora ({exc}); vale no proximo Reiniciar."
        return "Aplicado agora, sem reiniciar."

    def _diagnostico(self, sol: Solucao, tarefa: Tarefa, estado: dict) -> None:
        """Copia para o log da operacao o motivo da falha e o fim do log da aplicacao."""
        tarefa.registrar("")
        tarefa.registrar("----- diagnostico -----")
        if not estado.get("Running"):
            tarefa.registrar(f"Codigo de saida: {estado.get('ExitCode')} {estado.get('Error') or ''}".strip())
        if estado.get("OOMKilled"):
            tarefa.registrar(f"A aplicacao passou do limite de memoria ({_texto_mem(self.limites(sol)['memoria_mb'])}) "
                             "e foi encerrada. Aumente o limite na tela Uso.")
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


def _texto_mem(mb: int) -> str:
    if not mb:
        return "sem limite"
    return f"{mb / 1024:g} GB" if mb % 1024 == 0 else f"{mb} MB"


def _texto_cpus(cpus: float) -> str:
    return f"{cpus:g}" if cpus else "sem limite"
