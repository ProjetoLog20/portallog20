"""Supabase por solucao: cada sistema feito sobre o Supabase ganha um so para ele.

Uma solucao com `[supabase]` no manifesto (ex.: o Panorama) recebe uma pilha propria, com banco,
login, API de dados, arquivos e o gateway, em containers `log20-sb-<id>-*` numa rede so dela
(`log20-sb-<id>`). Os dados ficam nos volumes `log20-sb-<id>-dados|config|arquivos`; recriar ou
atualizar os containers nunca os apaga. Nada abre porta na maquina: o navegador fala com a API
pelo MESMO endereco do sistema (`http://<servidor>:<porta>/rest/v1`, `/auth/v1`, `/storage/v1`),
que o Caddy encaminha ao gateway desta pilha depois do login do portal.

Os containers sao criados com a CLI do Docker (create, cp, start), sem docker compose: assim o
portal, que roda num container, nao depende de caminhos da maquina. Cada container leva no rotulo
`log20.sb.hash` o resumo da sua configuracao; se a configuracao muda (endereco do sistema por troca
de IP, imagem nova), o portal recria so aquele container.

O painel (Supabase Studio) e um so para o servidor (`log20-sb-studio`), aberto para um projeto de
cada vez: o botao "Abrir o Studio" da solucao recria o painel apontando para a pilha dela.

As versoes das imagens e os arquivos em deploy/supabase/volumes vem do compose oficial
(github.com/supabase/supabase, pasta docker; commit em deploy/supabase/ORIGEM-COMMIT). Sem realtime,
edge functions, logs/analytics, pooler e imgproxy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import docker_cli

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVOS = RAIZ / "deploy" / "supabase" / "volumes"

IMAGENS = {
    "db": "supabase/postgres:17.6.1.136",
    "auth": "supabase/gotrue:v2.197.0",
    "rest": "postgrest/postgrest:v14.18",
    "storage": "supabase/storage-api:v1.89.12",
    "meta": "supabase/postgres-meta:v0.100.0",
    "gw": "envoyproxy/envoy:v1.39.6",
    "realtime": "supabase/realtime:v2.148.3",
    "studio": "supabase/studio:2026.11.02-sha-a05e7ec",
}
# Ordem de subida (o banco primeiro, o gateway por ultimo); para parar, a ordem inversa.
SERVICOS = ("db", "auth", "rest", "realtime", "meta", "storage", "gw")
# So existem quando a solucao pede ([supabase] realtime = true); sem isso nao contam como falta.
OPCIONAIS = ("realtime",)
ROTULOS = {"db": "banco", "auth": "login", "rest": "API de dados", "realtime": "tempo real (Realtime)",
           "meta": "meta (painel)", "storage": "arquivos", "gw": "gateway", "studio": "Studio"}
# Nome que o gateway (envoy) usa para o Realtime; dele o Realtime tira o tenant ("realtime-dev").
APELIDO_REALTIME = "realtime-dev.supabase-realtime"
STUDIO = "log20-sb-studio"
PORTA_GATEWAY = 8000
# Maximo de linhas por resposta da API (PostgREST). Os sistemas paginam com .range() em blocos
# maiores que 1000 (o Panorama usa 10000): com o padrao 1000, as paginas voltavam cortadas.
MAX_LINHAS_API = "1000000"
ANOS_CHAVES = 10
# Caminhos da API do Supabase que os sistemas usam pelo navegador (dados, login e arquivos).
CAMINHOS_API = ("/rest/v1/*", "/auth/v1/*", "/storage/v1/*", "/realtime/v1/*")
# Nomes padrao das variaveis que recebem o endereco e a chave publica (o que o supabase-js usa).
VARIAVEL_URL = "VITE_SUPABASE_URL"
VARIAVEL_CHAVE = "VITE_SUPABASE_ANON_KEY"

_travas: dict[str, threading.Lock] = {}
_trava_geral = threading.Lock()


class ErroSupabase(RuntimeError):
    pass


# ---------------------------------------------------------------- chaves (JWT HS256)

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


def chave_valida(token: str, segredo: str, papel: str) -> bool:
    partes = token.split(".")
    if len(partes) != 3:
        return False
    esperado = hmac.new(segredo.encode(), f"{partes[0]}.{partes[1]}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url(esperado), partes[2]):
        return False
    corpo = json.loads(base64.urlsafe_b64decode(partes[1] + "=" * (-len(partes[1]) % 4)))
    return corpo.get("role") == papel


# ---------------------------------------------------------------- nomes

def prefixo(sid: str) -> str:
    return f"log20-sb-{sid}"


def container(sid: str, servico: str) -> str:
    return f"{prefixo(sid)}-{servico}"


def rede(sid: str) -> str:
    return prefixo(sid)


def volume(sid: str, nome: str) -> str:
    return f"{prefixo(sid)}-{nome}"


def gateway(sid: str) -> str:
    """Endereco do gateway na rede do portal (para o Caddy e para servidores das solucoes)."""
    return f"{container(sid, 'gw')}:{PORTA_GATEWAY}"


def volumes(sid: str) -> list[str]:
    return [volume(sid, n) for n in ("dados", "config", "arquivos", "snippets")]


# ---------------------------------------------------------------- segredos por solucao

@dataclass
class Segredos:
    senha_banco: str
    jwt_secret: str
    anon: str
    service: str
    meta_chave: str
    painel_senha: str
    s3_id: str
    s3_segredo: str
    criado_em: str = ""
    # Realtime (criados na primeira vez que ele e ligado; pilhas antigas nao tem)
    realtime_chave_base: str = ""
    realtime_cripto: str = ""


def _arquivo_segredos(pasta_dados: Path, sid: str) -> Path:
    return pasta_dados / "supabase" / f"{sid}.json"


def segredos(pasta_dados: Path, sid: str, criar: bool = True) -> Segredos | None:
    """Le (ou cria, na primeira vez) os segredos da pilha da solucao em dados/supabase/<id>.json.

    Depois de criados nunca mudam: trocar o segredo dos tokens derrubaria todos os logins e
    invalidaria a chave gravada no site; trocar a senha do banco exigiria muda-la dentro dele.
    """
    arq = _arquivo_segredos(pasta_dados, sid)
    with _trava_geral:
        if arq.is_file():
            dados = json.loads(arq.read_text(encoding="utf-8"))
            return Segredos(**{k: dados[k] for k in Segredos.__dataclass_fields__ if k in dados})
        if not criar:
            return None
        jwt = secrets.token_hex(32)
        seg = Segredos(senha_banco=secrets.token_hex(24), jwt_secret=jwt, anon=chave("anon", jwt),
                       service=chave("service_role", jwt), meta_chave=secrets.token_hex(32),
                       painel_senha=secrets.token_urlsafe(18), s3_id=secrets.token_hex(16),
                       s3_segredo=secrets.token_hex(32), criado_em=time.strftime("%Y-%m-%d %H:%M:%S"))
        arq.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(arq.parent, 0o700)
        tmp = arq.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(vars(seg), fh, indent=2)
        tmp.replace(arq)
        return seg


def _gravar_json_privado(arq: Path, dados: dict) -> None:
    arq.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(arq.parent, 0o700)
    tmp = arq.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(dados, fh, indent=2, ensure_ascii=False)
    tmp.replace(arq)


def completar_segredos(pasta_dados: Path, sid: str, seg: Segredos) -> Segredos:
    """Acrescenta os segredos do Realtime a uma pilha que ainda nao os tem (sem mexer nos outros)."""
    if seg.realtime_chave_base and len(seg.realtime_cripto) == 16:
        return seg
    with _trava_geral:
        seg.realtime_chave_base = seg.realtime_chave_base or secrets.token_urlsafe(48)
        if len(seg.realtime_cripto) != 16:
            seg.realtime_cripto = secrets.token_hex(8)  # o Realtime exige exatamente 16 caracteres
        _gravar_json_privado(_arquivo_segredos(pasta_dados, sid), vars(seg))
    return seg


# ---------------------------------------------------------------- login e e-mail por solucao

@dataclass
class LoginConfig:
    """E-mail do login (convite, confirmacao, esqueci a senha) e cadastro, por solucao.
    Servidor em branco = usa o e-mail do .env (SUPABASE_SMTP_*), igual para todas."""
    smtp_servidor: str = ""
    smtp_porta: str = "587"
    smtp_usuario: str = ""
    smtp_senha: str = ""
    smtp_remetente: str = ""
    smtp_nome: str = ""
    cadastro_aberto: bool = True


def _arquivo_login(pasta_dados: Path, sid: str) -> Path:
    return pasta_dados / "supabase" / f"{sid}.login.json"


def ler_login(pasta_dados: Path, sid: str) -> LoginConfig:
    arq = _arquivo_login(pasta_dados, sid)
    if not arq.is_file():
        return LoginConfig()
    dados = json.loads(arq.read_text(encoding="utf-8"))
    return LoginConfig(**{k: dados[k] for k in LoginConfig.__dataclass_fields__ if k in dados})


def gravar_login(pasta_dados: Path, sid: str, cfg: LoginConfig) -> None:
    _gravar_json_privado(_arquivo_login(pasta_dados, sid), vars(cfg))


def smtp_global() -> dict[str, str]:
    e = os.environ.get
    return {"servidor": e("SUPABASE_SMTP_SERVIDOR", ""), "porta": e("SUPABASE_SMTP_PORTA", "") or "587",
            "usuario": e("SUPABASE_SMTP_USUARIO", ""), "senha": e("SUPABASE_SMTP_SENHA", ""),
            "remetente": e("SUPABASE_SMTP_REMETENTE", ""), "nome": e("SUPABASE_SMTP_NOME", "")}


def smtp_efetivo(login: LoginConfig | None) -> tuple[dict[str, str], str]:
    """(dados do SMTP, origem): o da solucao quando tem servidor; senao o do .env; senao nenhum."""
    if login and login.smtp_servidor:
        return ({"servidor": login.smtp_servidor, "porta": login.smtp_porta or "587",
                 "usuario": login.smtp_usuario, "senha": login.smtp_senha,
                 "remetente": login.smtp_remetente, "nome": login.smtp_nome}, "sistema")
    g = smtp_global()
    return g, ("servidor" if g["servidor"] else "nenhum")


def conferir(pasta_dados: Path, sid: str) -> list[str]:
    """Problemas nas chaves guardadas (assinatura que nao bate com o segredo, papel errado)."""
    seg = segredos(pasta_dados, sid, criar=False)
    if seg is None:
        return ["ainda sem segredos (nunca foi criado)"]
    problemas = []
    if not chave_valida(seg.anon, seg.jwt_secret, "anon"):
        problemas.append("chave anon nao confere com o segredo dos tokens")
    if not chave_valida(seg.service, seg.jwt_secret, "service_role"):
        problemas.append("chave service_role nao confere com o segredo dos tokens")
    return problemas


# ---------------------------------------------------------------- especificacao dos containers

@dataclass
class Servico:
    nome: str
    container: str
    imagem: str
    env: dict[str, str] = field(default_factory=dict)
    apelidos: list[str] = field(default_factory=list)
    volumes: dict[str, str] = field(default_factory=dict)
    # arquivo em deploy/supabase/volumes -> caminho dentro do container (copiado antes de iniciar)
    arquivos: dict[str, str] = field(default_factory=dict)
    saude: str = ""
    comando: list[str] = field(default_factory=list)
    entrypoint: str = ""
    # True: entra tambem na rede do portal (o Caddy chega nele pelo nome do container)
    na_rede_portal: bool = False

    def resumo(self) -> str:
        """Muda quando qualquer coisa da configuracao muda (inclusive o conteudo dos arquivos)."""
        conteudo = {orig: hashlib.sha256((ARQUIVOS / orig).read_bytes()).hexdigest() for orig in self.arquivos}
        dados = {"imagem": self.imagem, "env": dict(sorted(self.env.items())), "apelidos": self.apelidos,
                 "volumes": self.volumes, "arquivos": self.arquivos, "conteudo": conteudo, "saude": self.saude,
                 "comando": self.comando, "entrypoint": self.entrypoint, "portal": self.na_rede_portal}
        return hashlib.sha256(json.dumps(dados, sort_keys=True).encode()).hexdigest()[:24]


def _smtp(login: LoginConfig | None) -> dict[str, str]:
    """E-mail do login: o da solucao (tela Gerenciar) ou o do .env do portal (SUPABASE_SMTP_*)."""
    d, _ = smtp_efetivo(login)
    return {
        "GOTRUE_SMTP_ADMIN_EMAIL": d["remetente"],
        "GOTRUE_SMTP_HOST": d["servidor"],
        "GOTRUE_SMTP_PORT": d["porta"] or "587",
        "GOTRUE_SMTP_USER": d["usuario"],
        "GOTRUE_SMTP_PASS": d["senha"],
        "GOTRUE_SMTP_SENDER_NAME": d["nome"] or "Portal LOG20",
    }


def especificacao(sid: str, seg: Segredos, url: str, realtime: bool = False,
                  login: LoginConfig | None = None) -> list[Servico]:
    """Os containers da pilha, como no compose oficial. `url` = endereco publico do sistema.
    `realtime`: inclui o servico de tempo real ([supabase] realtime = true no manifesto)."""
    url = url.rstrip("/")
    senha = seg.senha_banco
    jwt_exp = "3600"
    login = login or LoginConfig()
    smtp = _smtp(login)
    autoconfirmar = "false" if smtp["GOTRUE_SMTP_HOST"] else "true"
    esquemas = "public,graphql_public"
    c = lambda s: container(sid, s)  # noqa: E731
    return [
        Servico("db", c("db"), IMAGENS["db"], apelidos=["db"],
                env={"POSTGRES_HOST": "/var/run/postgresql", "PGPORT": "5432", "POSTGRES_PORT": "5432",
                     "PGPASSWORD": senha, "POSTGRES_PASSWORD": senha, "PGDATABASE": "postgres",
                     "POSTGRES_DB": "postgres", "JWT_EXP": jwt_exp},
                volumes={volume(sid, "dados"): "/var/lib/postgresql/data",
                         volume(sid, "config"): "/etc/postgresql-custom"},
                arquivos={"db/webhooks.sql": "/docker-entrypoint-initdb.d/init-scripts/98-webhooks.sql",
                          "db/roles.sql": "/docker-entrypoint-initdb.d/init-scripts/99-roles.sql",
                          "db/jwt.sql": "/docker-entrypoint-initdb.d/init-scripts/99-jwt.sql"},
                saude="pg_isready -U postgres -h localhost",
                comando=["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf", "-c", "log_min_messages=fatal"]),
        Servico("auth", c("auth"), IMAGENS["auth"], apelidos=["auth"],
                env={"GOTRUE_API_HOST": "0.0.0.0", "GOTRUE_API_PORT": "9999", "API_EXTERNAL_URL": url,
                     "GOTRUE_DB_DRIVER": "postgres",
                     "GOTRUE_DB_DATABASE_URL": f"postgres://supabase_auth_admin:{senha}@db:5432/postgres",
                     # O sistema fica no mesmo endereco da API: links de e-mail voltam para ele.
                     "GOTRUE_SITE_URL": url, "GOTRUE_URI_ALLOW_LIST": f"{url}/**",
                     # Cadastro ligado (o Panorama cria usuarios pela tela dele, com signUp). Sem
                     # e-mail configurado, a confirmacao e automatica.
                     "GOTRUE_DISABLE_SIGNUP": "false" if login.cadastro_aberto else "true", "GOTRUE_EXTERNAL_EMAIL_ENABLED": "true",
                     "GOTRUE_MAILER_AUTOCONFIRM": autoconfirmar,
                     "GOTRUE_EXTERNAL_ANONYMOUS_USERS_ENABLED": "false", "GOTRUE_EXTERNAL_PHONE_ENABLED": "false",
                     "GOTRUE_SMS_AUTOCONFIRM": "false", "GOTRUE_JWT_ADMIN_ROLES": "service_role",
                     "GOTRUE_JWT_AUD": "authenticated", "GOTRUE_JWT_DEFAULT_GROUP_NAME": "authenticated",
                     "GOTRUE_JWT_EXP": jwt_exp, "GOTRUE_JWT_SECRET": seg.jwt_secret, "GOTRUE_JWT_ISSUER": f"{url}/auth/v1",
                     **smtp,
                     "GOTRUE_MAILER_URLPATHS_INVITE": "/auth/v1/verify",
                     "GOTRUE_MAILER_URLPATHS_CONFIRMATION": "/auth/v1/verify",
                     "GOTRUE_MAILER_URLPATHS_RECOVERY": "/auth/v1/verify",
                     "GOTRUE_MAILER_URLPATHS_EMAIL_CHANGE": "/auth/v1/verify"},
                saude="wget --no-verbose --tries=1 --spider http://localhost:9999/health"),
        # A imagem do PostgREST nao tem shell: sem health check (o gateway so sobe depois).
        Servico("rest", c("rest"), IMAGENS["rest"], apelidos=["rest"],
                env={"PGRST_DB_URI": f"postgres://authenticator:{senha}@db:5432/postgres",
                     "PGRST_DB_SCHEMAS": esquemas, "PGRST_DB_MAX_ROWS": MAX_LINHAS_API, "PGRST_DB_EXTRA_SEARCH_PATH": "public",
                     "PGRST_DB_ANON_ROLE": "anon", "PGRST_ADMIN_SERVER_PORT": "3001",
                     "PGRST_ADMIN_SERVER_HOST": "localhost", "PGRST_JWT_SECRET": seg.jwt_secret,
                     "PGRST_DB_USE_LEGACY_GUCS": "false", "PGRST_APP_SETTINGS_JWT_EXP": jwt_exp},
                comando=["postgrest"]),
        *([Servico("realtime", c("realtime"), IMAGENS["realtime"], apelidos=[APELIDO_REALTIME, "realtime"],
                   env={"PORT": "4000", "DB_HOST": "db", "DB_PORT": "5432", "DB_USER": "supabase_admin",
                        "DB_PASSWORD": senha, "DB_NAME": "postgres",
                        "DB_AFTER_CONNECT_QUERY": "SET search_path TO _realtime",
                        "DB_ENC_KEY": seg.realtime_cripto, "API_JWT_SECRET": seg.jwt_secret,
                        "SECRET_KEY_BASE": seg.realtime_chave_base, "METRICS_JWT_SECRET": seg.jwt_secret,
                        "ERL_AFLAGS": "-proto_dist inet_tcp", "DNS_NODES": "''", "RLIMIT_NOFILE": "10000",
                        "APP_NAME": "realtime", "SEED_SELF_HOST": "true", "RUN_JANITOR": "true",
                        "DISABLE_HEALTHCHECK_LOGGING": "true"},
                   saude=f'curl -sSfL --head -o /dev/null -H "Authorization: Bearer {seg.anon}" '
                         "http://localhost:4000/api/tenants/realtime-dev/health")] if realtime else []),
        Servico("meta", c("meta"), IMAGENS["meta"], apelidos=["meta"],
                env={"PG_META_PORT": "8080", "PG_META_DB_HOST": "db", "PG_META_DB_PORT": "5432",
                     "PG_META_DB_NAME": "postgres", "PG_META_DB_USER": "postgres", "PG_META_DB_PASSWORD": senha,
                     "CRYPTO_KEY": seg.meta_chave}),
        Servico("storage", c("storage"), IMAGENS["storage"], apelidos=["storage"],
                env={"ANON_KEY": seg.anon, "SERVICE_KEY": seg.service, "POSTGREST_URL": "http://rest:3000",
                     "AUTH_JWT_SECRET": seg.jwt_secret,
                     "DATABASE_URL": f"postgres://supabase_storage_admin:{senha}@db:5432/postgres",
                     "STORAGE_PUBLIC_URL": url, "REQUEST_ALLOW_X_FORWARDED_PATH": "true",
                     "FILE_SIZE_LIMIT": "52428800", "STORAGE_BACKEND": "file", "GLOBAL_S3_BUCKET": "stub",
                     "FILE_STORAGE_BACKEND_PATH": "/var/lib/storage", "TENANT_ID": "stub", "REGION": "stub",
                     "ENABLE_IMAGE_TRANSFORMATION": "false", "S3_PROTOCOL_ACCESS_KEY_ID": seg.s3_id,
                     "S3_PROTOCOL_ACCESS_KEY_SECRET": seg.s3_segredo},
                volumes={volume(sid, "arquivos"): "/var/lib/storage"},
                # 127.0.0.1, nao localhost: na imagem (Alpine) localhost vai primeiro para o IPv6 (::1),
                # onde o servico nao escuta, e o teste falhava com o servico funcionando.
                saude="wget --no-verbose --tries=1 --spider http://127.0.0.1:5000/status"),
        # Gateway (Envoy, o padrao do compose oficial): confere as chaves e junta login, dados e
        # arquivos num endereco so. Na rede do portal, o Caddy chega nele pelo nome do container.
        Servico("gw", c("gw"), IMAGENS["gw"], apelidos=["kong", "envoy"], na_rede_portal=True,
                env={"ANON_KEY": seg.anon, "SERVICE_ROLE_KEY": seg.service, "SUPABASE_PUBLISHABLE_KEY": "",
                     "SUPABASE_SECRET_KEY": "", "ANON_KEY_ASYMMETRIC": "", "SERVICE_ROLE_KEY_ASYMMETRIC": "",
                     "SUPABASE_PUBLIC_URL": url, "DASHBOARD_USERNAME": "supabaselog",
                     "DASHBOARD_PASSWORD": seg.painel_senha},
                arquivos={"api/envoy/envoy.yaml": "/etc/envoy/envoy.yaml", "api/envoy/cds.yaml": "/etc/envoy/cds.yaml",
                          "api/envoy/lds.template.yaml": "/etc/envoy/lds.template.yaml",
                          "api/envoy/docker-entrypoint.sh": "/docker-entrypoint.sh"},
                saude="timeout 1 bash -c '</dev/tcp/127.0.0.1/8000'",
                entrypoint="/bin/sh", comando=["/docker-entrypoint.sh"]),
    ]


def especificacao_studio(sid: str, nome: str, seg: Segredos, url: str) -> Servico:
    """Painel (um so no servidor), apontando para a pilha de uma solucao."""
    return Servico(
        "studio", STUDIO, IMAGENS["studio"], apelidos=["studio"],
        env={"HOSTNAME": "0.0.0.0", "STUDIO_PG_META_URL": "http://meta:8080", "POSTGRES_HOST": "db",
             "POSTGRES_PORT": "5432", "POSTGRES_DB": "postgres", "POSTGRES_PASSWORD": seg.senha_banco,
             "POSTGRES_USER_READ_WRITE": "postgres", "PG_META_CRYPTO_KEY": seg.meta_chave,
             "PGRST_DB_SCHEMAS": "public,graphql_public", "PGRST_DB_MAX_ROWS": MAX_LINHAS_API,
             "PGRST_DB_EXTRA_SEARCH_PATH": "public", "DEFAULT_ORGANIZATION_NAME": "LOG20",
             "DEFAULT_PROJECT_NAME": nome, "OPENAI_API_KEY": "", "SUPABASE_URL": f"http://kong:{PORTA_GATEWAY}",
             "SUPABASE_PUBLIC_URL": url.rstrip("/"), "SUPABASE_ANON_KEY": seg.anon,
             "SUPABASE_SERVICE_KEY": seg.service, "AUTH_JWT_SECRET": seg.jwt_secret,
             "ENABLED_FEATURES_LOGS_ALL": "false", "SNIPPETS_MANAGEMENT_FOLDER": "/app/snippets"},
        volumes={volume(sid, "snippets"): "/app/snippets"},
        na_rede_portal=True,
    )


# ---------------------------------------------------------------- docker

def _estado(nome: str) -> dict:
    """{'existe', 'rodando', 'saude', 'hash', 'projeto', 'reinicios', 'oom'}."""
    info = docker_cli.container_inspect(nome)
    if not info:
        return {"existe": False, "rodando": False, "saude": None, "hash": "", "projeto": ""}
    st = info.get("State", {})
    rotulos = (info.get("Config") or {}).get("Labels") or {}
    return {"existe": True, "rodando": bool(st.get("Running")), "saude": (st.get("Health") or {}).get("Status"),
            "hash": rotulos.get("log20.sb.hash", ""), "projeto": rotulos.get("log20.supabase", ""),
            "reiniciando": bool(st.get("Restarting")), "oom": bool(st.get("OOMKilled")),
            "codigo": st.get("ExitCode")}


def _garantir_rede(nome: str, sid: str) -> None:
    r = docker_cli._run(["network", "inspect", nome], timeout=30)
    if r.returncode != 0:
        docker_cli.rodar(["network", "create", "--label", "log20.portal=1", "--label", f"log20.supabase={sid}", nome],
                         timeout=60)


def _criar(s: Servico, sid: str, rede_privada: str, rede_portal: str) -> None:
    """docker create + cp dos arquivos + rede do portal + docker start."""
    docker_cli.remove(s.container)
    args = ["create", "--name", s.container, "--restart", "unless-stopped",
            "--label", "log20.portal=1", "--label", f"log20.supabase={sid}",
            "--label", f"log20.sb.servico={s.nome}", "--label", f"log20.sb.hash={s.resumo()}"]
    principal = rede_portal if s.nome == "studio" and rede_portal else rede_privada
    args += ["--network", principal]
    if principal == rede_privada:
        for apelido in s.apelidos:
            args += ["--network-alias", apelido]
    if s.saude:
        args += ["--health-cmd", s.saude, "--health-interval", "5s", "--health-timeout", "5s",
                 "--health-retries", "20" if s.nome == "db" else "5", "--health-start-period", "10s"]
    for nome in s.env:
        args += ["-e", nome]  # valores pelo ambiente do processo: nao aparecem na linha de comando
    for vol, destino in s.volumes.items():
        args += ["-v", f"{vol}:{destino}"]
    if s.entrypoint:
        args += ["--entrypoint", s.entrypoint]
    args += [s.imagem, *s.comando]
    r = docker_cli._run(args, timeout=1800, env=s.env)  # baixa a imagem na primeira vez
    docker_cli._check(r, f"Falha ao criar {s.container}")
    for origem, destino in s.arquivos.items():
        docker_cli.rodar(["cp", str(ARQUIVOS / origem), f"{s.container}:{destino}"], timeout=60)
    if principal != rede_privada:
        docker_cli.rodar(["network", "connect", "--alias", "studio", rede_privada, s.container], timeout=60)
    elif s.na_rede_portal and rede_portal:
        docker_cli.rodar(["network", "connect", rede_portal, s.container], timeout=60)
    docker_cli.rodar(["start", s.container], timeout=120)


def _esperar(nome: str, rotulo: str, prazo_s: int, registrar: Callable[[str], None]) -> None:
    """Espera o health check do container ficar 'healthy'."""
    fim = time.time() + prazo_s
    avisou = False
    while time.time() < fim:
        e = _estado(nome)
        if e["saude"] == "healthy":
            return
        if e["existe"] and not e["rodando"] and not e.get("reiniciando"):
            raise ErroSupabase(f"O {rotulo} parou ao iniciar (codigo {e.get('codigo')}). Ultimas linhas:\n"
                               + (docker_cli.logs(nome, tail=25) or "(sem log)"))
        if not avisou and time.time() > fim - prazo_s + 20:
            registrar(f"  aguardando o {rotulo} ficar pronto (na primeira vez o banco leva 1 a 3 minutos)...")
            avisou = True
        time.sleep(3)
    raise ErroSupabase(f"O {rotulo} nao ficou pronto em {prazo_s // 60} minutos. Ultimas linhas:\n"
                       + (docker_cli.logs(nome, tail=25) or "(sem log)"))


def _trava(sid: str) -> threading.Lock:
    with _trava_geral:
        return _travas.setdefault(sid, threading.Lock())


# ---------------------------------------------------------------- operacoes

def _esquema_realtime(sid: str, seg: Segredos) -> None:
    """O esquema _realtime so e criado pelo banco novo na primeira vez; num banco que ja existia
    (ou copiado de outro Supabase) o Realtime nao sobe sem ele."""
    sql = "create schema if not exists _realtime; alter schema _realtime owner to supabase_admin;"
    r = docker_cli._run(["exec", "-e", "PGPASSWORD", container(sid, "db"), "psql", "-h", "localhost", "-U",
                         "supabase_admin", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-qc", sql],
                        timeout=60, env={"PGPASSWORD": seg.senha_banco})
    docker_cli._check(r, "Falha ao preparar o banco para o Realtime")


def garantir(sid: str, url: str, pasta_dados: Path, rede_portal: str,
             registrar: Callable[[str], None] = print, realtime: bool = False) -> Segredos:
    """Deixa a pilha da solucao no ar com a configuracao atual. Rodar de novo nao apaga nada.

    Container que nao existe ou com configuracao diferente (o rotulo log20.sb.hash) e recriado;
    container parado e iniciado; o resto fica como esta.
    """
    with _trava(sid):
        seg = segredos(pasta_dados, sid)
        if realtime:
            seg = completar_segredos(pasta_dados, sid, seg)
        if not _estado(container(sid, "db"))["existe"]:
            import shutil

            livre_gb = shutil.disk_usage("/").free / 1024 ** 3
            if livre_gb < 5:
                registrar(f"ATENCAO: so {livre_gb:.1f} GB livres no disco. As imagens do Supabase ocupam cerca de 3 GB e o banco cresce com os dados. Se faltar espaco, a criacao falha.")
        _garantir_rede(rede(sid), sid)
        mudou_algo = False
        if not realtime and _estado(container(sid, "realtime"))["existe"]:
            registrar(f"Supabase: {ROTULOS['realtime']}: desligado (o manifesto nao pede mais)")
            docker_cli.remove(container(sid, "realtime"))
            mudou_algo = True
        for s in especificacao(sid, seg, url, realtime, ler_login(pasta_dados, sid)):
            atual = _estado(s.container)
            rotulo = f"Supabase: {ROTULOS[s.nome]}"
            if atual["existe"] and atual["hash"] == s.resumo():
                if not atual["rodando"]:
                    registrar(f"{rotulo}: iniciando")
                    docker_cli.rodar(["start", s.container], timeout=120)
                    mudou_algo = True
            else:
                if s.nome == "realtime":
                    _esquema_realtime(sid, seg)
                registrar(f"{rotulo}: {'atualizando a configuracao' if atual['existe'] else 'criando'} "
                          f"({s.imagem})" + (" - baixando a imagem na primeira vez" if not atual["existe"] else ""))
                _criar(s, sid, rede(sid), rede_portal)
                mudou_algo = True
            if s.nome == "db":
                _esperar(s.container, "banco do Supabase", 300, registrar)
        _esperar(container(sid, "gw"), "gateway do Supabase", 180, registrar)
        _esperar(container(sid, "storage"), "servico de arquivos do Supabase", 120, registrar)
        if realtime:
            _esperar(container(sid, "realtime"), "tempo real (Realtime) do Supabase", 180, registrar)
        for s in ("auth", "storage", *(("realtime",) if realtime else ())):
            e = _estado(container(sid, s))
            if e.get("reiniciando") or (e["existe"] and not e["rodando"]):
                raise ErroSupabase(f"O {ROTULOS[s]} do Supabase nao ficou no ar. Ultimas linhas:\n"
                                   + (docker_cli.logs(container(sid, s), tail=25) or "(sem log)"))
        registrar("Supabase no ar." if mudou_algo else "Supabase ja estava no ar.")
        return seg


def parar(sid: str, registrar: Callable[[str], None] = print) -> None:
    """Para a pilha (os dados ficam nos volumes). Para tambem o Studio, se estiver neste projeto."""
    with _trava(sid):
        if _estado(STUDIO).get("projeto") == sid:
            docker_cli.remove(STUDIO)
        for s in reversed(SERVICOS):
            if _estado(container(sid, s))["rodando"]:
                docker_cli.stop(container(sid, s))
        registrar("Supabase parado. Os dados foram mantidos.")


def pausar(sid: str) -> list[str]:
    """Para os containers da pilha que estao no ar (o gateway primeiro, o banco por ultimo) e
    devolve quais eram, para `retomar`. Usado pelo backup: com o banco parado, a copia dos
    arquivos dele fica consistente. Nao mexe no Studio."""
    with _trava(sid):
        no_ar = [s for s in SERVICOS if _estado(container(sid, s))["rodando"]]
        for s in reversed(SERVICOS):
            if s in no_ar:
                docker_cli.stop(container(sid, s))
        return no_ar


def retomar(sid: str, servicos: list[str], registrar: Callable[[str], None] = print) -> None:
    """Liga de novo os containers que `pausar` parou, na ordem certa, e espera ficarem prontos."""
    with _trava(sid):
        for s in SERVICOS:
            if s not in servicos:
                continue
            docker_cli.rodar(["start", container(sid, s)], timeout=120)
            if s == "db":
                _esperar(container(sid, "db"), "banco do Supabase", 300, registrar)
        if "gw" in servicos:
            _esperar(container(sid, "gw"), "gateway do Supabase", 180, registrar)


def caminho_segredos(pasta_dados: Path, sid: str) -> Path:
    return _arquivo_segredos(pasta_dados, sid)


def gravar_segredos(pasta_dados: Path, sid: str, conteudo: bytes) -> None:
    """Grava o arquivo de segredos (vindo de um backup), so leitura para o dono."""
    json.loads(conteudo)  # confere que e JSON antes de trocar
    arq = _arquivo_segredos(pasta_dados, sid)
    arq.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(arq.parent, 0o700)
    tmp = arq.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(conteudo)
    tmp.replace(arq)


def remover_containers(sid: str, registrar: Callable[[str], None] = print) -> None:
    """Remove os containers da pilha (nunca os volumes: os dados ficam)."""
    with _trava(sid):
        if _estado(STUDIO).get("projeto") == sid:
            docker_cli.remove(STUDIO)
        for s in reversed(SERVICOS):
            docker_cli.remove(container(sid, s))
        registrar("Containers do Supabase removidos. Os dados (volumes log20-sb-*) foram mantidos.")


def estado(sid: str) -> dict:
    """Situacao de cada parte da pilha, para a tela."""
    partes = []
    for s in SERVICOS:
        e = _estado(container(sid, s))
        if not e["existe"] and s in OPCIONAIS:
            continue
        if not e["existe"]:
            situacao = "nao criado"
        elif e.get("reiniciando"):
            situacao = "reiniciando" + (" (falta de memoria)" if e.get("oom") else "")
        elif not e["rodando"]:
            situacao = "parado"
        elif e["saude"] in (None, "healthy"):
            situacao = "no ar"
        else:
            situacao = "iniciando" if e["saude"] == "starting" else "com problema"
        partes.append({"servico": s, "rotulo": ROTULOS[s], "situacao": situacao})
    no_ar = all(p["situacao"] == "no ar" for p in partes)
    criado = any(p["situacao"] != "nao criado" for p in partes)
    studio = _estado(STUDIO)
    return {"no_ar": no_ar, "criado": criado, "partes": partes,
            "studio_neste": studio["existe"] and studio.get("projeto") == sid and studio["rodando"]}


def gateway_no_ar(sid: str) -> bool:
    e = _estado(container(sid, "gw"))
    return e["rodando"] and e["saude"] in (None, "healthy")


def abrir_studio(sid: str, nome: str, url: str, pasta_dados: Path, rede_portal: str,
                 registrar: Callable[[str], None] = print, esperar_s: int = 90) -> None:
    """Aponta o painel para a pilha da solucao (recria o container do Studio se preciso) e espera
    ele responder, para a pessoa nao cair na pagina de "fora do ar"."""
    seg = segredos(pasta_dados, sid, criar=False)
    if seg is None or not gateway_no_ar(sid):
        raise ErroSupabase("O Supabase desta solucao nao esta no ar. Inicie a solucao primeiro.")
    s = especificacao_studio(sid, nome, seg, url)
    atual = _estado(STUDIO)
    if atual["existe"] and atual["hash"] == s.resumo():
        if not atual["rodando"]:
            docker_cli.rodar(["start", STUDIO], timeout=120)
    else:
        registrar(f"Studio: abrindo o projeto {sid}" + (" (baixando a imagem na primeira vez)" if not atual["existe"] else ""))
        with _trava("studio"):
            _criar(s, sid, rede(sid), rede_portal)
    if rede_portal and esperar_s:
        import urllib.error
        import urllib.request

        fim = time.time() + esperar_s
        while time.time() < fim:
            try:
                with urllib.request.urlopen(f"http://{STUDIO}:3000/", timeout=3):
                    return
            except urllib.error.HTTPError:
                return  # respondeu (ex.: redirecionamento ou 404): esta no ar
            except (urllib.error.URLError, OSError):
                time.sleep(2)


def studio_projeto() -> str:
    """Id da solucao que o Studio mostra agora ('' se desligado)."""
    e = _estado(STUDIO)
    return e.get("projeto", "") if e["rodando"] else ""
