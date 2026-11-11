"""Configuracao do portal por variaveis de ambiente (os padroes servem para uso local)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _bool(nome: str, padrao: bool = False) -> bool:
    valor = os.environ.get(nome)
    if valor is None:
        return padrao
    return valor.strip().lower() in {"1", "true", "sim", "yes", "on"}


@dataclass(frozen=True)
class Config:
    pasta_solucoes: Path
    pasta_dados: Path
    # Endereco que os usuarios usam para chegar ao servidor, sem porta (ex.: http://192.168.0.10).
    url_base: str
    # Interface em que as portas das solucoes sao publicadas: 127.0.0.1 (so a maquina) ou 0.0.0.0 (rede).
    bind_solucoes: str
    # Como o portal alcanca as solucoes para checar se estao no ar.
    host_interno: str
    # Rede do Docker em que as solucoes sao criadas (ex.: "log20"); vazio usa a rede padrao.
    rede_docker: str
    # False: as solucoes nao abrem porta na maquina; so o proxy (Caddy) na mesma rede chega nelas.
    publicar_portas: bool
    cookie_seguro: bool
    # Ex.: ".log20.com.br" para a sessao valer tambem nos subdominios das solucoes (forward auth).
    cookie_dominio: str
    # Endereco publico do proprio portal (usado para redirecionar ao login).
    url_publica: str
    sessao_horas: int
    docs_api: bool
    # Token de leitura para clonar repositorios privados das solucoes (enviado so para git_host).
    git_token: str
    git_host: str
    # Caddy gerenciado pelo portal: nome do container (vazio = o portal nao mexe no Caddy),
    # pasta onde o portal escreve o Caddyfile e endereco do portal visto pelo Caddy.
    caddy_container: str
    caddy_pasta: Path
    portal_upstream: str
    # Faixa de portas publicada pelo Caddy (ex.: (8101, 8150)); None = sem restricao.
    faixa_portas: tuple[int, int] | None
    # PostgreSQL do servidor (container na mesma rede das solucoes); vazio = sem banco gerenciado.
    postgres_container: str = ""
    # Backup: pasta principal, pasta de copia (ex.: compartilhamento de rede montado na VM; None =
    # sem copia), imagem usada para ler/gravar os volumes (ja existe no servidor) e fuso dos horarios.
    backup_pasta: Path = RAIZ / "backups"
    backup_copia: Path | None = None
    backup_imagem: str = "postgres:17"
    fuso: str = "America/Sao_Paulo"
    # Limites padrao de cada solucao (0 = sem limite); o manifesto e a tela podem mudar por solucao.
    limite_memoria_mb: int = 1024
    limite_cpus: float = 1.0
    limite_processos: int = 1024
    # Painel de uso: intervalo da coleta e quantos dias de historico guardar.
    uso_intervalo_s: int = 60
    uso_dias: int = 7
    # Supabase do servidor (deploy/supabase): ligado por PORTAL_SUPABASE=sim; porta da API.
    supabase_ativo: bool = False
    supabase_porta_api: int = 8111
    supabase_gateway: str = "log20-supabase-gw:8000"

    @property
    def pasta_solucoes_locais(self) -> Path:
        """Solucoes criadas pelo assistente neste servidor (fora do Git)."""
        return self.pasta_dados / "solucoes-locais"


def _limite(texto: str, padrao, tipo: str):
    """Le PORTAL_LIMITE_*: vazio = padrao; 'sem' ou 0 = sem limite."""
    from .catalog import cpus_validas, memoria_mb

    texto = texto.strip().lower()
    if not texto:
        return padrao
    if texto in {"sem", "nenhum", "0"}:
        return 0
    try:
        return memoria_mb(texto) if tipo == "memoria" else cpus_validas(texto)
    except ValueError:
        return padrao


def carregar() -> Config:
    return Config(
        pasta_solucoes=Path(os.environ.get("PORTAL_SOLUCOES", RAIZ / "solucoes")),
        pasta_dados=Path(os.environ.get("PORTAL_DADOS", RAIZ / "dados")),
        url_base=os.environ.get("PORTAL_URL_BASE", "http://localhost").rstrip("/"),
        bind_solucoes=os.environ.get("PORTAL_BIND_SOLUCOES", "127.0.0.1"),
        host_interno=os.environ.get("PORTAL_HOST_INTERNO", "127.0.0.1"),
        rede_docker=os.environ.get("PORTAL_REDE_DOCKER", ""),
        publicar_portas=_bool("PORTAL_PUBLICAR_PORTAS", True),
        cookie_seguro=_bool("PORTAL_COOKIE_SEGURO"),
        cookie_dominio=os.environ.get("PORTAL_COOKIE_DOMINIO", ""),
        url_publica=os.environ.get("PORTAL_URL_PUBLICA", "").rstrip("/"),
        sessao_horas=int(os.environ.get("PORTAL_SESSAO_HORAS", "12")),
        docs_api=_bool("PORTAL_DOCS_API"),
        git_token=os.environ.get("PORTAL_GIT_TOKEN", "").strip(),
        git_host=os.environ.get("PORTAL_GIT_HOST", "github.com").strip(),
        caddy_container=os.environ.get("PORTAL_CADDY_CONTAINER", "").strip(),
        caddy_pasta=Path(os.environ.get("PORTAL_CADDY_PASTA", RAIZ / "dados" / "caddy")),
        portal_upstream=os.environ.get("PORTAL_UPSTREAM", "portal:8000").strip(),
        faixa_portas=_faixa(os.environ.get("PORTAL_FAIXA_PORTAS", "")),
        postgres_container=os.environ.get("PORTAL_POSTGRES_CONTAINER", "").strip(),
        backup_pasta=Path(os.environ.get("PORTAL_BACKUP_PASTA", "").strip() or RAIZ / "backups"),
        backup_copia=Path(c) if (c := os.environ.get("PORTAL_BACKUP_COPIA", "").strip()) else None,
        backup_imagem=os.environ.get("PORTAL_BACKUP_IMAGEM", "").strip() or "postgres:17",
        fuso=os.environ.get("PORTAL_FUSO", "").strip() or "America/Sao_Paulo",
        limite_memoria_mb=_limite(os.environ.get("PORTAL_LIMITE_MEMORIA", ""), 1024, "memoria"),
        limite_cpus=_limite(os.environ.get("PORTAL_LIMITE_CPUS", ""), 1.0, "cpus"),
        limite_processos=int(os.environ.get("PORTAL_LIMITE_PROCESSOS", "").strip() or 1024),
        uso_intervalo_s=max(15, int(os.environ.get("PORTAL_USO_INTERVALO", "").strip() or 60)),
        uso_dias=max(1, int(os.environ.get("PORTAL_USO_DIAS", "").strip() or 7)),
        supabase_ativo=os.environ.get("PORTAL_SUPABASE", "").strip().lower() in {"1", "sim", "true", "yes", "on"},
        supabase_porta_api=int(os.environ.get("PORTAL_SUPABASE_PORTA_API", "").strip() or 8111),
    )


def _faixa(valor: str) -> tuple[int, int] | None:
    """"8101-8150" -> (8101, 8150)."""
    valor = valor.strip()
    if not valor:
        return None
    inicio, _, fim = valor.partition("-")
    a, b = int(inicio), int(fim or inicio)
    if not (1 <= a <= b <= 65535):
        raise ValueError(f"PORTAL_FAIXA_PORTAS invalida: {valor}")
    return a, b
