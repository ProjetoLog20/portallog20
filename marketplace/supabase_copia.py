"""Copia os dados de outro Supabase (ex.: o da nuvem) para o Supabase de uma solucao.

Tela: Gerenciar > Visao geral > "Copiar dados de outro Supabase". Partes, nesta ordem:

1. **seguranca**: copia completa do banco local (pg_dump) em <backups>/supabase/<id>/, antes de
   mexer em qualquer coisa (as 3 ultimas ficam);
2. **tabelas** (esquema public): o public local e apagado e recriado com o mesmo dono e as mesmas
   permissoes de antes; o da origem entra por pg_dump/pg_restore (tabelas, dados, funcoes,
   gatilhos, regras RLS, permissoes);
3. **usuarios do login** (auth.users e auth.identities, com as senhas): os locais sao apagados e
   os da origem entram so com as colunas que existem nos dois lados (versoes diferentes do login
   tem colunas diferentes);
4. **buckets e regras dos arquivos** (storage.buckets e as regras de storage.objects);
5. **arquivos** (opcional): baixados da origem pela API dela, com a chave de servico, e enviados
   para o Supabase local pela API dele.

Tudo que fala com o banco roda DENTRO do container do banco local (log20-sb-<id>-db), que tem o
pg_dump/psql da mesma versao e acesso a internet. A senha da origem e a chave de servico vao so
pelo ambiente do processo (`docker exec -e NOME`), nunca na linha de comando, no log ou em disco.
"""

from __future__ import annotations

import csv
import io
import json
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import docker_cli
from . import supabase as sb

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
USUARIO_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")
BANCO_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,62}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d+)?/?$")
ARQ = "/tmp/log20-copia"  # pasta de trabalho dentro do container do banco
MANTER_SEGURANCA = 3
# Erros do pg_restore que nao atrapalham (o Supabase local ja tem o que eles tentam criar).
ERROS_ESPERADOS = (
    'schema "public" already exists',
    "permission denied to change default privileges",
    "must be owner of extension",
    "must be owner of schema public",
    'publication "supabase_realtime" does not exist',
)


class ErroCopia(RuntimeError):
    pass


@dataclass
class Origem:
    host: str
    porta: int
    usuario: str
    senha: str
    banco: str = "postgres"
    url: str = ""          # endereco do projeto (https://<ref>.supabase.co), so para os arquivos
    chave_servico: str = ""  # service_role da origem, so para os arquivos

    def env(self) -> dict[str, str]:
        return {"PGHOST": self.host, "PGPORT": str(self.porta), "PGUSER": self.usuario, "PGDATABASE": self.banco,
                "PGPASSWORD": self.senha, "PGSSLMODE": "require", "PGCONNECT_TIMEOUT": "20"}


def validar(dados: dict) -> tuple[Origem, dict[str, bool]]:
    host = str(dados.get("host") or "").strip()
    usuario = str(dados.get("usuario") or "").strip()
    banco = str(dados.get("banco") or "postgres").strip()
    url = str(dados.get("url") or "").strip().rstrip("/")
    try:
        porta = int(dados.get("porta") or 5432)
    except (TypeError, ValueError) as exc:
        raise ErroCopia("Porta inválida.") from exc
    if not HOST_RE.match(host):
        raise ErroCopia("Servidor inválido (ex.: aws-1-sa-east-1.pooler.supabase.com).")
    if not USUARIO_RE.match(usuario):
        raise ErroCopia("Usuário inválido (no pooler da Supabase é postgres.<id do projeto>).")
    if not BANCO_RE.match(banco) or not 1 <= porta <= 65535:
        raise ErroCopia("Banco ou porta inválidos.")
    if not dados.get("senha"):
        raise ErroCopia("Informe a senha do banco de origem.")
    partes = {k: bool((dados.get("partes") or {}).get(k)) for k in ("tabelas", "usuarios", "storage", "arquivos")}
    if partes["arquivos"]:
        partes["storage"] = True  # os arquivos so entram num bucket que existe
    if not any(partes.values()):
        raise ErroCopia("Marque ao menos uma parte para copiar.")
    if partes["arquivos"]:
        if not url:
            ref = usuario.split(".", 1)[1] if usuario.startswith("postgres.") else ""
            m = re.match(r"^db\.([a-z0-9]+)\.supabase\.co$", host)
            ref = ref or (m.group(1) if m else "")
            url = f"https://{ref}.supabase.co" if re.fullmatch(r"[a-z0-9]{10,40}", ref) else ""
        if not URL_RE.match(url):
            raise ErroCopia("Para copiar os arquivos, informe o endereço do projeto de origem (https://<id>.supabase.co).")
        chave = str(dados.get("chave_servico") or "").strip()
        if not chave:
            raise ErroCopia("Para copiar os arquivos, informe a chave de serviço (service_role) do projeto de origem.")
        if chave.startswith("sb_publishable_") or _papel_jwt(chave) == "anon":
            raise ErroCopia("Essa é a chave pública (anon/publishable): com ela a origem esconde os arquivos privados. "
                            "Use a chave de serviço: Project Settings → API Keys → service_role (ou uma secret key, sb_secret_...).")
        dados["chave_servico"] = chave
    return Origem(host, porta, usuario, str(dados["senha"]), banco, url, str(dados.get("chave_servico") or "")), partes


def _papel_jwt(token: str) -> str:
    """Papel (role) de uma chave JWT antiga do Supabase; '' se nao for JWT."""
    partes = token.split(".")
    if len(partes) != 3:
        return ""
    try:
        import base64

        corpo = json.loads(base64.urlsafe_b64decode(partes[1] + "=" * (-len(partes[1]) % 4)))
        return str(corpo.get("role") or "")
    except (ValueError, json.JSONDecodeError):
        return ""


@dataclass
class Copia:
    sid: str
    estado: str = "executando"  # executando | concluida | concluida_com_avisos | falhou
    etapa: str = ""
    log: deque = field(default_factory=lambda: deque(maxlen=2000))
    avisos: list[str] = field(default_factory=list)
    resumo: dict = field(default_factory=dict)
    erro: str = ""
    inicio: float = field(default_factory=time.time)
    fim: float | None = None

    def registrar(self, linha: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {linha}")

    def como_dict(self) -> dict:
        return {"estado": self.estado, "etapa": self.etapa, "log": "\n".join(self.log), "avisos": self.avisos,
                "resumo": self.resumo, "erro": self.erro, "inicio": self.inicio, "fim": self.fim}


_copias: dict[str, Copia] = {}
_trava = threading.Lock()


def situacao(sid: str) -> dict | None:
    c = _copias.get(sid)
    return c.como_dict() if c else None


def iniciar(sid: str, nome: str, origem: Origem, partes: dict[str, bool], pasta_dados: Path, pasta_backup: Path,
            antes: Callable[[], None] | None = None) -> Copia:
    with _trava:
        atual = _copias.get(sid)
        if atual and atual.estado == "executando":
            raise ErroCopia("Já existe uma cópia em andamento para este sistema.")
        copia = Copia(sid)
        _copias[sid] = copia
    threading.Thread(target=_rodar, args=(copia, nome, origem, partes, pasta_dados, pasta_backup, antes),
                     daemon=True, name=f"copia-supabase-{sid}").start()
    return copia


# ---------------------------------------------------------------- execucao

class _Banco:
    """Comandos no container do banco local, contra a origem ou contra o proprio banco."""

    def __init__(self, sid: str, senha_local: str, origem: Origem):
        self.container = sb.container(sid, "db")
        self.senha_local = senha_local
        self.origem = origem

    def _exec(self, args: list[str], env: dict[str, str], timeout: float = 3600, entrada: Path | None = None) -> str:
        cmd = ["exec", *(["-i"] if entrada else [])]
        for nome in env:
            cmd += ["-e", nome]
        cmd += [self.container, *args]
        if entrada is not None:
            return docker_cli.de_arquivo(cmd, entrada, env=env, timeout=timeout)
        r = docker_cli._run(cmd, timeout=timeout, env=env)
        saida = "\n".join(p for p in (r.stdout, r.stderr) if p).strip()
        if r.returncode != 0:
            raise ErroCopia(_limpar_erro(saida))
        return r.stdout

    def origem_cmd(self, args: list[str], timeout: float = 3600) -> str:
        return self._exec(args, self.origem.env(), timeout)

    def local_env(self, usuario: str) -> dict[str, str]:
        return {"PGHOST": "localhost", "PGPORT": "5432", "PGUSER": usuario, "PGDATABASE": "postgres",
                "PGPASSWORD": self.senha_local, "PGSSLMODE": "disable"}

    def local_cmd(self, args: list[str], usuario: str = "supabase_admin", timeout: float = 3600) -> str:
        return self._exec(args, self.local_env(usuario), timeout)

    def local_sql(self, sql: str, usuario: str = "supabase_admin", transacao: bool = True) -> str:
        """Roda um script SQL no banco local (pelo stdin), parando no primeiro erro."""
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as fh:
            fh.write(sql)
            caminho = Path(fh.name)
        try:
            args = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", *(["-1"] if transacao else []), "-f", "-"]
            return self._exec(args, self.local_env(usuario), 3600, entrada=caminho)
        except docker_cli.DockerError as exc:
            raise ErroCopia(_limpar_erro(str(exc))) from exc
        finally:
            caminho.unlink(missing_ok=True)

    def origem_csv(self, consulta: str) -> list[list[str]]:
        consulta = " ".join(consulta.split())  # \\copy precisa de uma linha so
        saida = self.origem_cmd(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", f"\\copy ({consulta}) to stdout csv"])
        return list(csv.reader(io.StringIO(saida)))

    def local_csv(self, consulta: str, usuario: str = "supabase_admin") -> list[list[str]]:
        consulta = " ".join(consulta.split())
        saida = self.local_cmd(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", f"\\copy ({consulta}) to stdout csv"],
                               usuario)
        return list(csv.reader(io.StringIO(saida)))


def _limpar_erro(texto: str) -> str:
    linhas = [l for l in texto.splitlines() if l.strip() and "PGPASSWORD" not in l]
    importantes = [l for l in linhas if re.search(r"(?i)error|fatal|erro|could not|failed|refused|timeout", l)] or linhas
    msg = " | ".join(importantes[-3:])[:600]
    if re.search(r"(?i)network is unreachable|cannot assign requested address", msg):
        msg += " — o endereço 'Direct connection' da Supabase só funciona por IPv6; use o do 'Session pooler'."
    elif re.search(r"(?i)password authentication failed", msg):
        msg += " — senha do banco de origem errada (é a do banco, não a do login no site da Supabase)."
    elif re.search(r"(?i)tenant or user not found", msg):
        msg += " — usuário errado: no pooler é postgres.<id do projeto> (20 letras)."
    return msg


def _colunas(banco: _Banco, esquema: str, tabela: str, lado: str) -> list[str]:
    consulta = ("select column_name from information_schema.columns where table_schema = "
                f"'{esquema}' and table_name = '{tabela}' and is_generated = 'NEVER' order by ordinal_position")
    linhas = banco.origem_csv(consulta) if lado == "origem" else banco.local_csv(consulta)
    return [l[0] for l in linhas if l]


def _exportar(banco: _Banco, esquema: str, tabela: str) -> tuple[str, str]:
    """Le da origem as linhas de esquema.tabela, so com as colunas que os dois lados tem (versoes
    diferentes do login tem colunas diferentes). Devolve (lista de colunas, arquivo csv)."""
    origem = _colunas(banco, esquema, tabela, "origem")
    local = set(_colunas(banco, esquema, tabela, "local"))
    cols = [c for c in origem if c in local]
    if not cols:
        raise ErroCopia(f"{esquema}.{tabela}: nenhuma coluna em comum entre a origem e o Supabase local.")
    lista = ", ".join(f'"{c}"' for c in cols)
    arq = f"{ARQ}/{esquema}.{tabela}.csv"
    banco.origem_cmd(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
                      f"\\copy (select {lista} from {esquema}.{tabela}) to '{arq}' csv"])
    return lista, arq


def _contar(banco: _Banco, tabela: str) -> int:
    linhas = banco.local_csv(f"select count(*) from {tabela}")
    return int(linhas[0][0]) if linhas else 0


def _copiar_usuarios(banco: _Banco) -> tuple[int, int]:
    """Troca os usuarios do login locais pelos da origem, numa transacao so: primeiro le tudo da
    origem; se a leitura falhar, nada local e apagado. Sem disparar gatilhos (ex.: um gatilho do
    sistema que cria o perfil de cada usuario novo): as linhas que eles criariam ja vem da origem."""
    lu, au = _exportar(banco, "auth", "users")
    li, ai = _exportar(banco, "auth", "identities")
    # Apaga os usuarios e o que depende deles DENTRO do esquema auth (identidades, sessoes...), sem
    # o "on delete cascade" chegar as tabelas do sistema: quem copia so os usuarios nao pode perder,
    # por exemplo, os perfis ligados a eles.
    apagar = " ".join(l[0] for l in banco.local_csv(SQL_DEPENDENTES_AUTH) if l)
    banco.local_sql("set local session_replication_role = replica;\n"
                    f"{apagar}\n"
                    f"\\copy auth.users ({lu}) from '{au}' csv\n"
                    f"\\copy auth.identities ({li}) from '{ai}' csv\n")
    return _contar(banco, "auth.users"), _contar(banco, "auth.identities")


# Tabelas do esquema auth ligadas (direta ou indiretamente) a auth.users.
SQL_DEPENDENTES_AUTH = r"""
with recursive dep(rel) as (
  select 'auth.users'::regclass
  union
  select c.conrelid::regclass from pg_constraint c join dep on c.confrelid = dep.rel
    join pg_class k on k.oid = c.conrelid join pg_namespace n on n.oid = k.relnamespace
   where c.contype = 'f' and n.nspname = 'auth'
)
select format('delete from %s;', rel) from dep
"""


def _copiar_buckets(banco: _Banco, sid: str, seg) -> int:
    """Buckets da origem, criados (ou atualizados) pela API de arquivos do Supabase local.

    Nao vai direto na tabela: as versoes novas do servico de arquivos bloqueiam mudancas em
    storage.buckets que nao venham dele ("bucket control columns may only be changed by the
    configured storage service role")."""
    linhas = banco.origem_csv("select to_jsonb(b)::text from storage.buckets b order by b.id")
    base = f"http://{sb.gateway(sid)}/storage/v1/bucket"
    cab = {"Authorization": f"Bearer {seg.service}", "apikey": seg.service, "Content-Type": "application/json"}
    n = 0
    for (texto,) in linhas:
        b = json.loads(texto)
        dados = {"id": b["id"], "name": b.get("name") or b["id"], "public": bool(b.get("public")),
                 "file_size_limit": b.get("file_size_limit"), "allowed_mime_types": b.get("allowed_mime_types")}
        corpo = json.dumps({k: v for k, v in dados.items() if v is not None}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(base, data=corpo, method="POST", headers=cab), timeout=60).close()
        except urllib.error.HTTPError as exc:
            detalhe = exc.read()[:300].decode("utf-8", "replace")
            if exc.code not in (400, 409) or "exist" not in detalhe.lower():
                raise ErroCopia(f"Bucket {b['id']}: {exc.code} {detalhe}") from exc
            # ja existe: atualiza publico/privado, limite de tamanho e tipos aceitos
            mudar = json.dumps({k: v for k, v in dados.items() if k not in ("id", "name") and v is not None}).encode()
            try:
                urllib.request.urlopen(urllib.request.Request(f"{base}/{urllib.parse.quote(b['id'], safe='')}",
                                                              data=mudar, method="PUT", headers=cab), timeout=60).close()
            except urllib.error.HTTPError as exc2:
                raise ErroCopia(f"Bucket {b['id']}: {exc2.code} {exc2.read()[:300].decode('utf-8', 'replace')}") from exc2
        except (urllib.error.URLError, OSError) as exc:
            raise ErroCopia(f"Bucket {b['id']}: o servico de arquivos local nao respondeu ({exc}).") from exc
        n += 1
    return n


# SQL que recria o esquema public com o mesmo dono, as mesmas permissoes e as mesmas permissoes
# padrao de antes (as que a imagem do Supabase define para anon, authenticated e service_role).
SQL_RECRIAR_PUBLIC = r"""
select * from (
  select 1 as o, 'drop schema public cascade;' as sql
  union all
  select 2, format('create schema public authorization %s;', nspowner::regrole::text) from pg_namespace where nspname = 'public'
  union all
  select 3, format('grant %s on schema public to %s;', a.privilege_type,
                   case a.grantee when 0 then 'public' else a.grantee::regrole::text end)
    from pg_namespace n, aclexplode(n.nspacl) a where n.nspname = 'public'
  union all
  select 4, format('alter default privileges for role %s in schema public grant %s on %s to %s;',
                   d.defaclrole::regrole::text, a.privilege_type,
                   case d.defaclobjtype when 'r' then 'tables' when 'S' then 'sequences' when 'f' then 'functions'
                                        when 'T' then 'types' else 'schemas' end,
                   case a.grantee when 0 then 'public' else a.grantee::regrole::text end)
    from pg_default_acl d, aclexplode(d.defaclacl) a where d.defaclnamespace = 'public'::regnamespace
) x order by o
"""

# Regras (RLS) dos arquivos na origem, prontas para recriar no local.
SQL_REGRAS_STORAGE = r"""
select format('drop policy if exists %I on storage.%I; create policy %I on storage.%I as %s for %s to %s%s%s;',
              policyname, tablename, policyname, tablename, permissive, cmd,
              array_to_string(array(select case r when 'public' then 'public' else quote_ident(r) end
                                    from unnest(roles) r), ', '),
              coalesce(' using (' || qual || ')', ''), coalesce(' with check (' || with_check || ')', ''))
  from pg_policies where schemaname = 'storage' and tablename in ('objects', 'buckets')
"""


# Extensoes ligadas na origem (ex.: pg_trgm, unaccent), para ligar no local antes das tabelas.
SQL_EXTENSOES = r"""
select e.extname, n.nspname from pg_extension e join pg_namespace n on n.oid = e.extnamespace order by 1
"""


# Gatilhos que o sistema criou nas tabelas do login e dos arquivos (ex.: criar o perfil de cada
# usuario novo). O pg_dump do esquema public nao os leva, porque a tabela e de outro esquema.
SQL_GATILHOS = r"""
select pg_get_triggerdef(t.oid) || ';'
  from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n on n.oid = c.relnamespace
  join pg_proc p on p.oid = t.tgfoid join pg_namespace pn on pn.oid = p.pronamespace
 where not t.tgisinternal and n.nspname in ('auth', 'storage') and pn.nspname = 'public'
"""


def _rodar(c: Copia, nome: str, origem: Origem, partes: dict[str, bool], pasta_dados: Path, pasta_backup: Path,
           antes: Callable[[], None] | None) -> None:
    try:
        seg = sb.segredos(pasta_dados, c.sid, criar=False)
        if seg is None or not sb.gateway_no_ar(c.sid):
            raise ErroCopia("O Supabase deste sistema não está no ar. Inicie a solução e tente de novo.")
        banco = _Banco(c.sid, seg.senha_banco, origem)
        banco.local_cmd(["mkdir", "-p", ARQ])
        banco.local_cmd(["sh", "-c", f"rm -f {ARQ}/*"])

        c.etapa = "Conferindo a origem"
        c.registrar(f"Origem: {origem.usuario}@{origem.host}:{origem.porta}/{origem.banco}")
        info = banco.origem_csv("select current_setting('server_version'), "
                                "(select count(*) from auth.users), "
                                "(select count(*) from pg_tables where schemaname = 'public'), "
                                "(select count(*) from storage.buckets), (select count(*) from storage.objects)")
        versao, n_usu, n_tab, n_buck, n_obj = info[0]
        c.registrar(f"Origem conectada: PostgreSQL {versao.split()[0]}; {n_usu} usuário(s), {n_tab} tabela(s), "
                    f"{n_buck} bucket(s), {n_obj} arquivo(s).")
        c.resumo["origem"] = {"usuarios": int(n_usu), "tabelas": int(n_tab), "buckets": int(n_buck), "arquivos": int(n_obj)}
        if int(versao.split(".")[0]) > 17:
            raise ErroCopia(f"A origem usa PostgreSQL {versao}, mais novo que o do Supabase local (17).")

        c.etapa = "Copiando o banco local antes (segurança)"
        destino = pasta_backup / "supabase" / c.sid
        destino.mkdir(parents=True, exist_ok=True)
        arq_seg = destino / f"antes-de-copiar-{time.strftime('%Y-%m-%d_%H%M%S')}.dump"
        env = banco.local_env("supabase_admin")
        docker_cli.para_arquivo(["exec", *[a for n in env for a in ("-e", n)], banco.container,
                                 "pg_dump", "-Fc", "--no-owner"], arq_seg, env=env)
        c.registrar(f"Cópia de segurança do Supabase local: {arq_seg.name} ({arq_seg.stat().st_size // 1024} KB).")
        c.resumo["seguranca"] = str(arq_seg)
        for velho in sorted(destino.glob("antes-de-copiar-*.dump"))[:-MANTER_SEGURANCA]:
            velho.unlink(missing_ok=True)
        if antes:
            antes()

        if partes["tabelas"]:
            c.etapa = "Lendo as tabelas da origem"
            banco.origem_cmd(["pg_dump", "-Fc", "--schema=public", "--no-owner", "-f", f"{ARQ}/public.dump"])
            c.registrar("Esquema public da origem lido (tabelas, dados, funções, regras).")
            c.etapa = "Limpando o esquema public local"
            recriar = "\n".join(l[1] for l in banco.local_csv(SQL_RECRIAR_PUBLIC) if len(l) > 1)
            banco.local_sql(recriar)
            c.registrar("Esquema public local recriado vazio, com as mesmas permissões.")
            _ligar_extensoes(c, banco)

        if partes["usuarios"]:
            c.etapa = "Copiando os usuários do login"
            n1, n2 = _copiar_usuarios(banco)
            c.registrar(f"Usuários do login: {n1} usuário(s), {n2} identidade(s), com as mesmas senhas.")
            c.resumo["usuarios"] = n1

        if partes["tabelas"]:
            c.etapa = "Gravando as tabelas no Supabase local"
            env = banco.local_env("supabase_admin")
            r = docker_cli._run(["exec", *[a for n in env for a in ("-e", n)], banco.container, "pg_restore",
                                 "--no-owner", "--role=postgres", "-d", "postgres", f"{ARQ}/public.dump"],
                                timeout=3600, env=env)
            erros = [l.split("ERROR:", 1)[-1].replace("pg_restore: error:", "").strip()
                     for l in (r.stderr or "").splitlines()
                     if re.search(r"(?i)error", l) and "errors ignored on restore" not in l and "Command was:" not in l]
            inesperados = [e for e in erros if not any(x in e for x in ERROS_ESPERADOS)]
            if erros:
                c.registrar(f"pg_restore: {len(erros) - len(inesperados)} aviso(s) esperado(s) "
                            "(coisas que o Supabase local ja tem)" + (f", {len(inesperados)} outro(s)" if inesperados else "") + ".")
            for e in inesperados[:15]:
                c.avisos.append(f"Tabelas: {e[:300]}")
            gatilhos = [l[0] for l in banco.origem_csv(SQL_GATILHOS) if l]
            for g in gatilhos:
                try:
                    banco.local_sql(g)
                except ErroCopia as exc:
                    c.avisos.append(f"Gatilho: {exc}")
            if gatilhos:
                c.registrar(f"{len(gatilhos)} gatilho(s) do sistema nas tabelas do login/arquivos recriado(s).")
            n_local = int(banco.local_csv("select count(*) from pg_tables where schemaname = 'public'")[0][0])
            c.registrar(f"Tabelas no Supabase local: {n_local} (origem: {n_tab}).")
            c.resumo["tabelas"] = n_local
            if n_local < int(n_tab):
                c.avisos.append(f"Vieram {n_local} de {n_tab} tabelas. Veja o log do pg_restore.")

        if partes["storage"]:
            c.etapa = "Copiando os buckets dos arquivos"
            n_b = _copiar_buckets(banco, c.sid, seg)
            c.registrar(f"Arquivos: {n_b} bucket(s).")
            c.resumo["buckets"] = n_b
        # As regras dos arquivos costumam usar funcoes do public: recriar o public (tabelas) as apaga
        # junto. Por isso voltam sempre que as tabelas ou os buckets vem da origem.
        if partes["storage"] or partes["tabelas"]:
            c.etapa = "Copiando as regras de acesso aos arquivos"
            regras = [l[0] for l in banco.origem_csv(SQL_REGRAS_STORAGE) if l]
            if regras:
                try:
                    banco.local_sql("\n".join(regras))
                except ErroCopia as exc:
                    c.avisos.append(f"Regras dos arquivos: {exc}")
            c.registrar(f"Arquivos: {len(regras)} regra(s) de acesso.")

        banco.local_sql("notify pgrst, 'reload schema';", transacao=False)

        if partes["arquivos"]:
            c.etapa = "Copiando os arquivos"
            _copiar_arquivos(c, banco, origem, seg)

        banco.local_cmd(["sh", "-c", f"rm -rf {ARQ}"])
        c.estado = "concluida_com_avisos" if c.avisos else "concluida"
        c.registrar("Cópia concluída." + (f" Avisos: {len(c.avisos)}." if c.avisos else ""))
    except (ErroCopia, docker_cli.DockerError, sb.ErroSupabase) as exc:
        c.estado, c.erro = "falhou", str(exc)
        c.registrar(f"ERRO: {exc}")
        if c.resumo.get("seguranca"):
            c.registrar("O Supabase local pode ter ficado pela metade. A cópia de segurança feita antes está em "
                        f"{Path(c.resumo['seguranca']).name} (pasta de backups, supabase/{c.sid}).")
        try:
            _Banco(c.sid, "", origem).local_cmd(["sh", "-c", f"rm -rf {ARQ}"])
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 - qualquer falha vira mensagem para a tela
        c.estado, c.erro = "falhou", f"Erro inesperado: {exc}"
        c.registrar(f"ERRO: {exc}")
    finally:
        c.etapa = ""
        c.fim = time.time()


def _ligar_extensoes(c: Copia, banco: _Banco) -> None:
    """Liga no local as extensoes que a origem usa e que o local tem (sem elas, tabelas e funcoes
    que dependem delas nao sao criadas)."""
    origem = {(l[0], l[1]) for l in banco.origem_csv(SQL_EXTENSOES) if len(l) > 1}
    local = {l[0] for l in banco.local_csv(SQL_EXTENSOES) if l}
    disponiveis = {l[0] for l in banco.local_csv("select name from pg_available_extensions") if l}
    faltam = sorted((n, e) for n, e in origem if n not in local)
    for nome, esquema in faltam:
        if nome not in disponiveis:
            c.avisos.append(f"Extensão {nome}: existe na origem, mas não neste Supabase; o que depende dela não vem.")
            continue
        try:
            banco.local_sql(f'create schema if not exists "{esquema}";\ncreate extension if not exists "{nome}" with schema "{esquema}";')
            c.registrar(f"Extensão {nome} ligada (esquema {esquema}).")
        except ErroCopia as exc:
            c.avisos.append(f"Extensão {nome}: {exc}")


def _copiar_arquivos(c: Copia, banco: _Banco, origem: Origem, seg) -> None:
    """Baixa cada arquivo da origem (API de arquivos, chave de servico) e envia para o local."""
    lista = banco.origem_csv("select bucket_id, name, coalesce(metadata->>'mimetype', '') from storage.objects "
                             "where name is not null order by bucket_id, name")
    local = f"http://{sb.gateway(c.sid)}"
    ok = falhas = 0
    for i, (bucket, nome, tipo) in enumerate(lista, 1):
        caminho = f"{urllib.parse.quote(bucket, safe='')}/{urllib.parse.quote(nome, safe='/')}"
        # Chave nova (sb_secret_...) vai so em apikey; a antiga (JWT service_role) tambem no Authorization.
        cab = {"apikey": origem.chave_servico}
        if not origem.chave_servico.startswith("sb_"):
            cab["Authorization"] = f"Bearer {origem.chave_servico}"
        lado = "baixar da origem"
        try:
            req = urllib.request.Request(f"{origem.url}/storage/v1/object/authenticated/{caminho}", headers=cab)
            with urllib.request.urlopen(req, timeout=120) as resp:
                conteudo = resp.read()
                tipo = tipo or resp.headers.get("Content-Type", "application/octet-stream")
            lado = "enviar para o Supabase local"
            envio = urllib.request.Request(f"{local}/storage/v1/object/{caminho}", data=conteudo, method="POST", headers={
                "Authorization": f"Bearer {seg.service}", "apikey": seg.service, "x-upsert": "true",
                "Content-Type": tipo or "application/octet-stream"})
            with urllib.request.urlopen(envio, timeout=120):
                pass
            ok += 1
        except urllib.error.HTTPError as exc:
            falhas += 1
            detalhe = " ".join(exc.read()[:300].decode("utf-8", "replace").split())
            msg = f"{bucket}/{nome}: erro ao {lado} ({exc.code}) {detalhe}"
            if ok == 0 and falhas == 1:
                # O primeiro ja falhou: quase sempre e o mesmo motivo para todos. Para e explica.
                dica = ""
                if lado.startswith("baixar") and exc.code in (400, 401, 403, 404):
                    dica = (" A origem não entregou o arquivo: confira se a chave é a de serviço (service_role ou "
                            f"sb_secret_...) do mesmo projeto e se o endereço {origem.url} é o desse projeto.")
                raise ErroCopia(msg + dica) from exc
            c.avisos.append(msg)
        except (urllib.error.URLError, OSError) as exc:
            falhas += 1
            msg = f"{bucket}/{nome}: erro ao {lado}: {exc}"
            if ok == 0 and falhas == 1:
                raise ErroCopia(msg) from exc
            c.avisos.append(msg)
        if i % 20 == 0 or i == len(lista):
            c.registrar(f"Arquivos: {i} de {len(lista)} ({falhas} com erro).")
    c.resumo["arquivos"] = ok
    if len(c.avisos) > 50:
        c.avisos[:] = c.avisos[:50] + [f"... e mais {len(c.avisos) - 50} aviso(s)."]


def para_json(c: Copia) -> str:
    return json.dumps(c.como_dict())
