"""Supabase do servidor (deploy/supabase): segredos e endereco no .env.

`python -m marketplace supabase preparar` e chamado por deploy/atualizar.sh e deploy/preparar-vm.sh
quando PORTAL_SUPABASE=sim. Na primeira vez gera os segredos (senha do banco, segredo dos tokens,
chaves anon e service_role assinadas com ele, senha do painel); depois disso nunca os troca, porque
trocar o segredo dos tokens derruba todos os logins e invalida as chaves gravadas nos sites.
O endereco publico da API (SUPABASE_URL_PUBLICA) e recalculado a cada vez a partir de
PORTAL_URL_BASE, para acompanhar a troca de IP da VM.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

ANOS_CHAVES = 10
USUARIO_PAINEL_PADRAO = "supabaselog"


def _b64url(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()


def jwt_hs256(payload: dict, segredo: str) -> str:
    cabecalho = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    corpo = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    assinatura = hmac.new(segredo.encode(), f"{cabecalho}.{corpo}".encode(), hashlib.sha256).digest()
    return f"{cabecalho}.{corpo}.{_b64url(assinatura)}"


def chave(papel: str, segredo: str, agora: int | None = None) -> str:
    """Chave de API do Supabase: um JWT com o papel (anon ou service_role), como o generate-keys.sh oficial."""
    iat = int(agora or time.time())
    return jwt_hs256({"role": papel, "iss": "supabase", "iat": iat, "exp": iat + ANOS_CHAVES * 365 * 86400}, segredo)


def ler_env(caminho: Path) -> dict[str, str]:
    valores = {}
    if caminho.exists():
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$", linha)
            if m:
                valores[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return valores


def gravar_env(caminho: Path, novos: dict[str, str]) -> None:
    """Atualiza ou acrescenta chaves no .env, sem mexer nas outras linhas nem nos comentarios."""
    linhas = caminho.read_text(encoding="utf-8").splitlines() if caminho.exists() else []
    pendentes = dict(novos)
    for i, linha in enumerate(linhas):
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)=", linha)
        if m and m.group(1) in pendentes:
            linhas[i] = f"{m.group(1)}={pendentes.pop(m.group(1))}"
    if pendentes:
        if linhas and linhas[-1].strip():
            linhas.append("")
        linhas.append("# Supabase do servidor (deploy/supabase) - gerado por 'python -m marketplace supabase preparar'.")
        linhas.append("# Nao troque SUPABASE_JWT_SECRET: derruba os logins e invalida as chaves gravadas nos sites.")
        linhas += [f"{k}={v}" for k, v in pendentes.items()]
    # Reescreve no mesmo arquivo (mesmo dono e permissoes).
    with open(caminho, "w", encoding="utf-8") as fh:
        fh.write("\n".join(linhas) + "\n")


def preparar(caminho_env: Path, porta_api: int = 8111) -> list[str]:
    """Garante os segredos e o endereco no .env. Devolve o que mudou (so os nomes)."""
    atual = ler_env(caminho_env)
    novos: dict[str, str] = {}
    if not atual.get("SUPABASE_POSTGRES_SENHA"):
        novos["SUPABASE_POSTGRES_SENHA"] = secrets.token_hex(24)
    segredo = atual.get("SUPABASE_JWT_SECRET", "")
    if not segredo:
        segredo = secrets.token_hex(32)
        novos["SUPABASE_JWT_SECRET"] = segredo
    if not atual.get("SUPABASE_ANON_KEY") or "SUPABASE_JWT_SECRET" in novos:
        novos["SUPABASE_ANON_KEY"] = chave("anon", segredo)
    if not atual.get("SUPABASE_SERVICE_ROLE_KEY") or "SUPABASE_JWT_SECRET" in novos:
        novos["SUPABASE_SERVICE_ROLE_KEY"] = chave("service_role", segredo)
    if not atual.get("SUPABASE_PAINEL_USUARIO"):
        novos["SUPABASE_PAINEL_USUARIO"] = USUARIO_PAINEL_PADRAO
    if not atual.get("SUPABASE_PAINEL_SENHA"):
        novos["SUPABASE_PAINEL_SENHA"] = secrets.token_urlsafe(18)
    if not atual.get("SUPABASE_META_CHAVE"):
        novos["SUPABASE_META_CHAVE"] = secrets.token_hex(32)
    if not atual.get("SUPABASE_S3_ID"):
        novos["SUPABASE_S3_ID"] = secrets.token_hex(16)
    if not atual.get("SUPABASE_S3_SEGREDO"):
        novos["SUPABASE_S3_SEGREDO"] = secrets.token_hex(32)
    base = (atual.get("PORTAL_URL_BASE") or "http://localhost").rstrip("/")
    url = f"{base}:{porta_api}"
    if atual.get("SUPABASE_URL_PUBLICA") != url:
        novos["SUPABASE_URL_PUBLICA"] = url
    if novos:
        gravar_env(caminho_env, novos)
    return sorted(novos)


def conferir_chaves(caminho_env: Path) -> list[str]:
    """Problemas nas chaves do .env (assinatura que nao bate com o segredo, papel errado)."""
    v = ler_env(caminho_env)
    problemas = []
    for nome, papel in (("SUPABASE_ANON_KEY", "anon"), ("SUPABASE_SERVICE_ROLE_KEY", "service_role")):
        token = v.get(nome, "")
        partes = token.split(".")
        if len(partes) != 3:
            problemas.append(f"{nome} ausente ou invalida")
            continue
        esperado = hmac.new(v.get("SUPABASE_JWT_SECRET", "").encode(), f"{partes[0]}.{partes[1]}".encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(esperado), partes[2]):
            problemas.append(f"{nome} nao foi assinada com o SUPABASE_JWT_SECRET atual")
            continue
        corpo = json.loads(base64.urlsafe_b64decode(partes[1] + "=" * (-len(partes[1]) % 4)))
        if corpo.get("role") != papel:
            problemas.append(f"{nome} tem o papel '{corpo.get('role')}' (esperado: {papel})")
    return problemas
