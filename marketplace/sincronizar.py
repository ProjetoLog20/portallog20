"""Deixa as solucoes do catalogo no ar sem passar pela tela: `python -m marketplace sincronizar`.

Usado por `deploy/atualizar.sh` depois de atualizar o portal. Para cada solucao do catalogo:

- nunca instalada ou sem container: constroi (se preciso) e inicia;
- com problema: inicia de novo;
- no ar: so reinicia com --reiniciar (ex.: o IP da VM mudou) e reconstroi com --atualizar
  (busca o ultimo commit do branch);
- parada: fica parada (alguem parou de proposito), a nao ser com --incluir-paradas.

Tambem regera a configuracao do Caddy. As operacoes rodam uma por vez, com o log no terminal.
"""

from __future__ import annotations

import sys
import time

from . import config as config_mod
from . import docker_cli, proxy
from .catalog import carregar_catalogo
from .manager import Armazenamento, Gerenciador, Tarefa


class TarefaNoTerminal(Tarefa):
    """Tarefa que tambem escreve cada linha do log no terminal."""

    def __init__(self, acao: str, prefixo: str):
        super().__init__(acao=acao)
        self.prefixo = prefixo

    def registrar(self, linha: str) -> None:
        super().registrar(linha)
        for parte in str(linha).splitlines() or [""]:
            print(f"  {self.prefixo} | {parte}", flush=True)


def _acao(estado: str, atualizar: bool, reiniciar: bool, incluir_paradas: bool) -> str | None:
    if estado in ("nao_instalado", "instalado"):
        return "iniciar"
    if estado == "com_problema":
        return "reconstruir" if atualizar else "iniciar"
    if estado in ("rodando", "iniciando"):
        if atualizar:
            return "reconstruir"
        return "iniciar" if reiniciar else None
    if estado == "parado" and incluir_paradas:
        return "reconstruir" if atualizar else "iniciar"
    return None


def _rotulo(acao: str, estado: str) -> str:
    if acao == "reconstruir":
        return "atualizar e iniciar"
    if estado == "nao_instalado":
        return "instalar e iniciar"
    return "reiniciar" if estado in ("rodando", "iniciando", "com_problema") else "iniciar"


def executar(atualizar: bool = False, reiniciar: bool = False, incluir_paradas: bool = False,
             somente: list[str] | None = None) -> int:
    cfg = config_mod.carregar()
    st = docker_cli.status()
    if not st.ok:
        print(f"Docker indisponivel: {st.erro}", file=sys.stderr)
        return 1

    solucoes, erros = carregar_catalogo(cfg.pasta_solucoes, cfg.pasta_solucoes_locais)
    if cfg.faixa_portas:
        inicio, fim = cfg.faixa_portas
        for sid, sol in list(solucoes.items()):
            if not inicio <= sol.porta_host <= fim:
                erros.append(f"{sol.manifesto}: porta {sol.porta_host} fora da faixa {inicio}-{fim}; ignorada")
                del solucoes[sid]
    for msg in erros:
        print(f"AVISO  {msg}")
    if somente:
        desconhecidas = [s for s in somente if s not in solucoes]
        if desconhecidas:
            print(f"Solucoes desconhecidas: {', '.join(desconhecidas)}", file=sys.stderr)
            return 1
        solucoes = {k: v for k, v in solucoes.items() if k in somente}

    if cfg.caddy_container:
        r = proxy.aplicar(proxy.gerar(list(solucoes.values()), cfg.portal_upstream, extras=proxy.extras(cfg, solucoes)),
                          cfg.caddy_pasta, cfg.caddy_container)
        print(f"[caddy] {r.mensagem}")

    ger = Gerenciador(solucoes, Armazenamento(cfg.pasta_dados), cfg)
    resultados: list[tuple[str, str, str]] = []
    imagens_feitas: set[str] = set()
    for sol in sorted(solucoes.values(), key=lambda s: s.porta_host):
        if sol.externo:
            continue  # sobe por fora do portal (ex.: deploy/supabase)
        estado = ger.status(sol)["estado"]
        acao = _acao(estado, atualizar, reiniciar, incluir_paradas)
        # Solucoes que dividem a imagem (ex.: principal + homologacao): constroi uma vez so.
        if acao == "reconstruir" and sol.imagem in imagens_feitas:
            acao = "iniciar"
        if acao is None:
            resultados.append((sol.id, ROTULOS_ESTADO.get(estado, estado), "sem mudanca"))
            continue
        valores = ger.armazenamento.configuracoes(sol)
        faltando = [c.rotulo for c in sol.configuracoes if c.obrigatoria and not valores.get(c.nome)]
        if faltando:
            resultados.append((sol.id, "falta configurar", f"preencha no portal: {', '.join(faltando)}"))
            continue
        rotulo = _rotulo(acao, estado)
        print(f"\n==> {sol.nome} ({sol.id}): {rotulo}", flush=True)
        tarefa = TarefaNoTerminal(acao, sol.id)
        inicio = time.time()
        ger._rodar(sol, tarefa)
        if acao == "reconstruir" and tarefa.estado == "concluida":
            imagens_feitas.add(sol.imagem)
        duracao = f"{time.time() - inicio:.0f}s"
        if tarefa.estado == "concluida":
            resultados.append((sol.id, "no ar", f"{rotulo} ({duracao})"))
        else:
            resultados.append((sol.id, "FALHOU", tarefa.erro or "erro"))

    print("\nResumo:")
    for sid, estado, detalhe in resultados:
        print(f"  {sid:<36} {estado:<14} {detalhe}")
    falhas = sum(1 for _, estado, _ in resultados if estado == "FALHOU")
    if falhas:
        print(f"\n{falhas} solucao(oes) com falha. Veja o log acima ou a aba Logs no portal.")
    return 1 if falhas else 0


ROTULOS_ESTADO = {
    "rodando": "no ar", "iniciando": "iniciando", "parado": "parada", "com_problema": "com problema",
    "instalado": "instalada", "nao_instalado": "nao instalada",
}
