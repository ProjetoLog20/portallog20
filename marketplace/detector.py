"""Detector de tecnologia: olha um repositório clonado e sugere como pública-lo no portal.

So LE arquivos (nunca executa o código do repositório). Devolve:
- a tecnologia encontrada e as evidencias (quais arquivos indicaram isso);
- um Dockerfile sugerido, quando o projeto não tem um (o portal guarda esse Dockerfile com o
  manifesto, sem precisar mexer no repositório de quem desenvolveu);
- porta, rota de saúde, variáveis, volumes e banco sugeridos para o manifesto;
- avisos de seguranca e LGPD (senhas no código, planilhas com CPF, HTTPS forcado...).

Cobertura: Django, Flask, FastAPI, Streamlit, Node (Express e afins, Next.js, sites React/Vite/
Vue estáticos), PHP, .NET, Java (Maven/Gradle), HTML estatico e projetos que já tem Dockerfile.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

IGNORAR_PASTAS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".next", "out", "vendor", "bin", "obj", "target", ".gradle", ".idea", ".vscode",
    "staticfiles", "coverage", ".tox",
}
MAX_ARQUIVOS = 20000
MAX_LEITURA = 300_000  # bytes lidos por arquivo
TEXTO_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".php", ".cs", ".java",
             ".kt", ".html", ".htm", ".json", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".txt", ".md", ".env",
             ".xml", ".gradle", ".properties", ".sh", ".csv", ".sql"}


@dataclass
class Aviso:
    nivel: str  # "erro" (resolver antes de publicar) | "atencao" | "info"
    titulo: str
    detalhe: str
    arquivos: list[str] = field(default_factory=list)


@dataclass
class Analise:
    tecnologia: str
    nome_tecnologia: str
    evidencias: list[str]
    suportado: bool
    tem_dockerfile: bool
    dockerfile: str | None
    porta_container: int
    caminho_saude: str
    comando: str
    ambiente: dict[str, str] = field(default_factory=dict)
    volumes: dict[str, str] = field(default_factory=dict)
    segredos: list[str] = field(default_factory=list)
    banco: dict[str, str] | None = None  # configuracao -> modelo ({db_url}...) se der para usar o banco do servidor
    # Sistema feito sobre o Supabase: variaveis que o portal preenche com o Supabase proprio dele
    # ({"url": "VITE_SUPABASE_URL", "chave_anon": ..., "url_interna": "", "chave_servico": ""}).
    supabase: dict[str, str] | None = None
    configuracoes: list[dict] = field(default_factory=list)
    avisos: list[Aviso] = field(default_factory=list)
    # Arquivos auxiliares do Dockerfile sugerido (nome -> conteudo), guardados com o manifesto.
    arquivos: dict[str, str] = field(default_factory=dict)
    id_sugerido: str = ""
    nome_sugerido: str = ""
    resumo_sugerido: str = ""

    def como_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- leitura do repositorio

class Repo:
    def __init__(self, raiz: Path):
        self.raiz = raiz
        self._cache: dict[str, str] = {}
        self.arquivos: list[str] = []
        self.grandes: list[tuple[str, int]] = []
        self.tem_node_modules = False
        for pasta, dirs, nomes in os.walk(raiz):
            rel = Path(pasta).relative_to(raiz)
            if "node_modules" in dirs and rel == Path("."):
                self.tem_node_modules = True
            dirs[:] = sorted(d for d in dirs if d not in IGNORAR_PASTAS and not d.startswith("."))
            for nome in sorted(nomes):
                caminho = (rel / nome).as_posix()
                self.arquivos.append(caminho)
                try:
                    tamanho = (Path(pasta) / nome).stat().st_size
                except OSError:
                    continue
                if tamanho > 50 * 1024 * 1024:
                    self.grandes.append((caminho, tamanho))
                if len(self.arquivos) >= MAX_ARQUIVOS:
                    return

    def existe(self, rel: str) -> bool:
        return (self.raiz / rel).is_file()

    def ler(self, rel: str) -> str:
        if rel not in self._cache:
            try:
                with (self.raiz / rel).open("rb") as fh:
                    self._cache[rel] = fh.read(MAX_LEITURA).decode("utf-8", errors="replace")
            except OSError:
                self._cache[rel] = ""
        return self._cache[rel]

    def com_extensao(self, *exts: str) -> list[str]:
        return [a for a in self.arquivos if Path(a).suffix.lower() in exts]

    def json(self, rel: str) -> dict:
        try:
            return json.loads(self.ler(rel))
        except (ValueError, TypeError):
            return {}


def _modulo(rel: str) -> str:
    """"app/main.py" -> "app.main"."""
    return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel


def _slug(texto: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")
    s = re.sub(r"-+", "-", s)[:41].strip("-")
    return s if len(s) >= 2 else "nova-solucao"


def _resumo_readme(repo: Repo) -> str:
    for nome in ("README.md", "readme.md", "README.MD", "README.txt", "README"):
        if repo.existe(nome):
            for bloco in re.split(r"\n\s*\n", repo.ler(nome)):
                linha = " ".join(bloco.split())
                if not linha or linha.startswith(("#", "!", "[", "<", "```", "|", "-", "*", ">")):
                    continue
                linha = re.sub(r"[*_`]", "", linha)
                return linha[:157] + "..." if len(linha) > 160 else linha
    return ""


# ---------------------------------------------------------------- python

def _requisitos(repo: Repo) -> tuple[str, str]:
    """Texto das dependências e como instalar: ("requirements", ...) | ("pyproject", ...) | ("", "")."""
    if repo.existe("requirements.txt"):
        return "requirements", repo.ler("requirements.txt").lower()
    if repo.existe("pyproject.toml"):
        return "pyproject", repo.ler("pyproject.toml").lower()
    if repo.existe("Pipfile"):
        return "pipfile", repo.ler("Pipfile").lower()
    return "", ""


def _versao_python(repo: Repo) -> str:
    for nome in (".python-version", "runtime.txt"):
        if repo.existe(nome):
            m = re.search(r"3\.(1[0-4]|9)", repo.ler(nome))
            if m:
                return f"3.{m.group(1)}"
    m = re.search(r"requires-python\s*=\s*[\"'][^\"']*3\.(1[0-4])", repo.ler("pyproject.toml")) if repo.existe("pyproject.toml") else None
    return f"3.{m.group(1)}" if m else "3.12"


# Arquivos auxiliares gerados junto com o Dockerfile. Ficam na pasta do manifesto e chegam ao
# build pelo contexto extra "portal" (docker build --build-context portal=<pasta do manifesto>).
ARQ_SAUDE = """#!/usr/bin/env python3
# Gerado pelo Portal LOG20: a aplicação esta no ar se responder com qualquer código abaixo de 500.
import sys, urllib.error, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=4)
except urllib.error.HTTPError as erro:
    sys.exit(0 if erro.code < 500 else 1)
except Exception:
    sys.exit(1)
"""
SAUDE_PY = "COPY --from=portal --chmod=755 portal-saude /usr/local/bin/portal-saude"


def _dockerfile_python(repo: Repo, porta: int, saude: str, extras: list[str], cmd: str,
                       antes_copiar: str = "", depois_copiar: str = "", arquivos: dict | None = None) -> str:
    if arquivos is not None:
        arquivos["portal-saude"] = ARQ_SAUDE
    tipo, deps = _requisitos(repo)
    faltam = [p for p in extras if p.split("[")[0].split("=")[0].lower() not in deps]
    if tipo == "requirements":
        instalar = "COPY requirements.txt .\nRUN pip install -r requirements.txt" + (f" {' '.join(faltam)}" if faltam else "")
        copiar = "COPY . ."
    elif tipo == "pyproject":
        instalar = ""
        copiar = "COPY . .\nRUN pip install ." + (f" {' '.join(faltam)}" if faltam else "")
    elif tipo == "pipfile":
        instalar = "COPY Pipfile* ./\nRUN pip install pipenv && pipenv install --system --deploy" + (f" && pip install {' '.join(faltam)}" if faltam else "")
        copiar = "COPY . ."
    else:
        instalar = f"RUN pip install {' '.join(extras)}" if extras else ""
        copiar = "COPY . ."
    partes = [
        "# Gerado pelo Portal de Soluções LOG20 a partir do repositório. Revise antes de usar.",
        f"FROM python:{_versao_python(repo)}-slim",
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1",
        "WORKDIR /app",
        instalar, antes_copiar, copiar, depois_copiar, SAUDE_PY,
        "RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app /app && chown -R app /app/data",
        "USER app",
        f"EXPOSE {porta}",
        "HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \\\n"
        f"  CMD portal-saude http://127.0.0.1:{porta}{saude}",
        cmd,
    ]
    return "\n".join(p for p in partes if p) + "\n"


def _achar_py(repo: Repo, padrao: str, preferidos: tuple[str, ...]) -> tuple[str, str] | None:
    """Arquivo .py onde `padrão` (regex com um grupo = nome da variável) aparece; preferidos primeiro."""
    candidatos = [a for a in repo.com_extensao(".py") if "test" not in a.lower()]
    candidatos.sort(key=lambda a: (Path(a).name not in preferidos, a.count("/"), a))
    for arq in candidatos:
        m = re.search(padrao, repo.ler(arq), re.M)
        if m:
            return arq, m.group(1)
    return None


def _rota_saude(repo: Repo, arquivos: list[str]) -> str | None:
    for rota in ("/health", "/api/health", "/healthz", "/saude", "/status", "/ping"):
        alvo = re.compile(r"[\"']" + re.escape(rota) + r"/?[\"']")
        if any(alvo.search(repo.ler(a)) for a in arquivos):
            return rota
    return None


def _usa_banco_url(texto: str) -> bool:
    return bool(re.search(r"DATABASE_URL|dj_database_url|env\.db\(|SQLALCHEMY_DATABASE_URI", texto))


def _analisar_django(repo: Repo, an: dict) -> None:
    manage = repo.ler("manage.py")
    m = re.search(r"DJANGO_SETTINGS_MODULE[\"']\s*,\s*[\"']([\w.]+)[\"']", manage)
    settings_mod = m.group(1) if m else ""
    settings_arq = settings_mod.replace(".", "/") + ".py" if settings_mod else ""
    if settings_arq and not repo.existe(settings_arq):
        pacote = settings_mod.replace(".", "/") + "/__init__.py"
        settings_arq = pacote if repo.existe(pacote) else ""
    settings = repo.ler(settings_arq) if settings_arq else ""
    tipo, deps = _requisitos(repo)
    avisos: list[Aviso] = an["avisos"]

    extras = ["gunicorn", "whitenoise"]
    usa_url = _usa_banco_url(settings)
    if usa_url and "psycopg" not in deps:
        extras.append("psycopg[binary]")
    an["arquivos"]["portal_wsgi.py"] = f'''"""Gerado pelo Portal LOG20: inicia o Django e serve os arquivos estáticos (WhiteNoise)
sem precisar mudar o settings.py do projeto."""
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{settings_mod or 'config.settings'}")
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
try:
    from django.conf import settings
    from whitenoise import WhiteNoise
    if getattr(settings, "STATIC_ROOT", None):
        application = WhiteNoise(application, root=str(settings.STATIC_ROOT), prefix=settings.STATIC_URL)
except Exception as exc:  # o sistema sobe mesmo sem os estáticos
    print(f"[portal] arquivos estáticos não configurados: {{exc}}", flush=True)
'''
    wsgi = "COPY --from=portal portal_wsgi.py /app/portal_wsgi.py"
    coletar = ("RUN SECRET_KEY=somente-para-o-build DEBUG=False python manage.py collectstatic --noinput \\\n"
               "    || echo 'collectstatic falhou: confira STATIC_ROOT no settings.py'")
    cmd = ('CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn portal_wsgi:application '
           '--bind 0.0.0.0:8000 --workers 3 --timeout 60 --access-logfile -"]')
    an["dockerfile"] = _dockerfile_python(repo, 8000, "/", extras, cmd, depois_copiar=wsgi + "\n" + coletar,
                                          arquivos=an["arquivos"])
    an["porta_container"] = 8000
    an["caminho_saude"] = _rota_saude(repo, repo.com_extensao(".py")) or "/"
    an["comando"] = "migrate + gunicorn (WSGI do portal com WhiteNoise)"
    an["segredos"] = ["SECRET_KEY"] if re.search(r"SECRET_KEY\s*=\s*.*(environ|getenv|env\()", settings) else []
    an["ambiente"] = {"DEBUG": "False", "ALLOWED_HOSTS": "{host},127.0.0.1,localhost", "CSRF_TRUSTED_ORIGINS": "{url}",
                      "TZ": "America/Sao_Paulo"}
    an["volumes"] = {"dados": "/app/data"}
    if usa_url:
        an["banco"] = {"DATABASE_URL": "{db_url}"}
        an["configuracoes"].append({"nome": "DATABASE_URL", "rotulo": "Banco de dados (string de conexão)", "tipo": "segredo",
                                    "ajuda": "Preenchido pelo portal com o banco do servidor. Para outro banco, troque aqui."})
    if settings:
        if not an["segredos"]:
            avisos.append(Aviso("atencao", "SECRET_KEY fixa no settings.py",
                                "A chave do Django esta escrita no código. Troque por os.environ['SECRET_KEY'] para o portal "
                                "gerar uma chave própria em cada servidor.", [settings_arq]))
        linhas_hosts = re.findall(r"^ALLOWED_HOSTS\s*\+?=.*$", settings, re.M)
        if linhas_hosts and not any(re.search(r"environ|getenv|env[.(]", l) for l in linhas_hosts):
            avisos.append(Aviso("atencao", "ALLOWED_HOSTS fixo no settings.py",
                                "O Django só aceita os endereços escritos no código e vai recusar o do servidor (erro 400). "
                                "Leia de os.environ['ALLOWED_HOSTS'].split(',').", [settings_arq]))
        if not usa_url and "sqlite3" in settings:
            avisos.append(Aviso("atencao", "Banco SQLite fixo no settings.py",
                                "O arquivo db.sqlite3 fica dentro do container e se perde a cada atualização. Leia o banco de "
                                "DATABASE_URL (dj-database-url ou django-environ) para usar o banco do servidor.", [settings_arq]))
        if "STATIC_ROOT" not in settings:
            avisos.append(Aviso("atencao", "STATIC_ROOT não definido",
                                "Sem STATIC_ROOT o collectstatic falha e CSS/JS não aparecem em produção. "
                                "Adicione STATIC_ROOT = BASE_DIR / 'staticfiles'.", [settings_arq]))
        if re.search(r"^DEBUG\s*=\s*True", settings, re.M):
            avisos.append(Aviso("atencao", "DEBUG = True fixo",
                                "Em produção o DEBUG mostra detalhes internos em caso de erro. Leia de uma variável de ambiente.",
                                [settings_arq]))
    if not tipo:
        avisos.append(Aviso("atencao", "Sem lista de dependências",
                            "Não achei requirements.txt nem pyproject.toml. O Dockerfile instala só o Django básico.", []))
        an["dockerfile"] = an["dockerfile"].replace("RUN pip install gunicorn whitenoise", "RUN pip install django gunicorn whitenoise", 1)


def _analisar_flask_fastapi(repo: Repo, an: dict, fastapi: bool) -> None:
    padrao = r"^(\w+)\s*=\s*FastAPI\(" if fastapi else r"^(\w+)\s*=\s*Flask\("
    achado = _achar_py(repo, padrao, ("main.py", "app.py", "server.py", "api.py", "wsgi.py"))
    fabrica = None
    if not achado and not fastapi:
        fabrica = _achar_py(repo, r"^def (create_app)\(", ("__init__.py", "app.py"))
    if achado:
        arq, var = achado
        alvo = f"{_modulo(arq)}:{var}"
    elif fabrica:
        arq, var = fabrica
        mod = _modulo(arq).removesuffix(".__init__")
        alvo = f"{mod}:create_app()"
    else:
        arq, alvo = "", "app:app"
        an["avisos"].append(Aviso("atencao", "Não achei onde a aplicação é criada",
                                  "Usei 'app:app'. Ajuste o CMD do Dockerfile com o módulo e a variável certos.", []))
    porta = 8000
    if fastapi:
        cmd = f'CMD ["uvicorn", "{alvo}", "--host", "0.0.0.0", "--port", "{porta}", "--proxy-headers", "--forwarded-allow-ips", "*"]'
        extras = ["uvicorn"]
        an["comando"] = f"uvicorn {alvo}"
    else:
        cmd = f'CMD ["gunicorn", "{alvo}", "--bind", "0.0.0.0:{porta}", "--workers", "3", "--access-logfile", "-"]'
        extras = ["gunicorn"]
        an["comando"] = f"gunicorn {alvo}"
    saude = _rota_saude(repo, repo.com_extensao(".py")) or ("/docs" if fastapi else "/")
    an.update(porta_container=porta, caminho_saude=saude, volumes={"dados": "/app/data"},
              ambiente={"TZ": "America/Sao_Paulo"},
              dockerfile=_dockerfile_python(repo, porta, saude, extras, cmd, arquivos=an["arquivos"]))
    codigo = "\n".join(repo.ler(a) for a in repo.com_extensao(".py")[:300])
    if _usa_banco_url(codigo):
        an["banco"] = {"DATABASE_URL": "{db_url}"}
        an["configuracoes"].append({"nome": "DATABASE_URL", "rotulo": "Banco de dados (string de conexão)", "tipo": "segredo",
                                    "ajuda": "Preenchido pelo portal com o banco do servidor. Para outro banco, troque aqui."})
    if re.search(r"SECRET_KEY|secret_key", codigo) and re.search(r"(environ|getenv)\([\"']SECRET_KEY", codigo):
        an["segredos"] = ["SECRET_KEY"]


def _analisar_streamlit(repo: Repo, an: dict) -> None:
    preferidos = ("streamlit_app.py", "app.py", "main.py", "Home.py", "home.py", "dashboard.py")
    candidatos = [a for a in repo.com_extensao(".py") if re.search(r"^\s*import streamlit|^\s*from streamlit", repo.ler(a), re.M)]
    candidatos.sort(key=lambda a: (Path(a).name not in preferidos, "pages/" in a, a.count("/"), a))
    principal = candidatos[0] if candidatos else "app.py"
    porta = 8501
    cmd = (f'CMD ["streamlit", "run", "{principal}", "--server.port={porta}", "--server.address=0.0.0.0", '
           '"--server.headless=true", "--browser.gatherUsageStats=false"]')
    an.update(porta_container=porta, caminho_saude="/_stcore/health", comando=f"streamlit run {principal}",
              volumes={"dados": "/app/data"}, ambiente={"TZ": "America/Sao_Paulo"},
              dockerfile=_dockerfile_python(repo, porta, "/_stcore/health", ["streamlit"], cmd, arquivos=an["arquivos"]))


# ---------------------------------------------------------------- node

def _gerenciador_node(repo: Repo, base: str) -> tuple[str, str, str]:
    """(arquivos a copiar, instalar tudo, instalar só produção)."""
    if repo.existe(f"{base}pnpm-lock.yaml"):
        return ("package.json pnpm-lock.yaml ./", "corepack enable && pnpm install --frozen-lockfile",
                "corepack enable && pnpm install --frozen-lockfile --prod")
    if repo.existe(f"{base}yarn.lock"):
        return ("package.json yarn.lock ./", "corepack enable && yarn install --frozen-lockfile",
                "corepack enable && yarn install --frozen-lockfile --production")
    if repo.existe(f"{base}package-lock.json"):
        return ("package*.json ./", "npm ci", "npm ci --omit=dev")
    return ("package.json ./", "npm install", "npm install --omit=dev")


def _versao_node(repo: Repo, pkg: dict) -> str:
    texto = repo.ler(".nvmrc") if repo.existe(".nvmrc") else str((pkg.get("engines") or {}).get("node", ""))
    m = re.search(r"(1[89]|2[0-4])", texto)
    return m.group(1) if m else "22"


def _porta_node(repo: Repo, arquivos: list[str]) -> int:
    for arq in arquivos:
        texto = repo.ler(arq)
        m = re.search(r"PORT\s*(?:\|\||\?\?)\s*(\d{4,5})", texto) or re.search(r"\.listen\(\s*(\d{4,5})", texto)
        if m:
            return int(m.group(1))
    return 3000


ARQ_NGINX = """# Gerado pelo Portal LOG20: site estatico; rotas desconhecidas voltam para o index.html (SPA).
server {
    listen 80;
    root /usr/share/nginx/html;
    location / { try_files $uri $uri/ /index.html; }
}
"""
NGINX_SPA = "COPY --from=portal nginx.conf /etc/nginx/conf.d/default.conf"


def _analisar_node(repo: Repo, an: dict) -> None:
    pkg = repo.json("package.json")
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    so_prod = pkg.get("dependencies") or {}
    scripts = pkg.get("scripts") or {}
    copiar_pkg, instalar, instalar_prod = _gerenciador_node(repo, "")
    v = _versao_node(repo, pkg)
    servidor = {"express", "fastify", "koa", "@hapi/hapi", "@nestjs/core", "hono", "restify"} & set(so_prod)
    estatico = {"vite", "react-scripts", "@vue/cli-service", "parcel"} & set(deps)

    if "next" in deps:
        an["tecnologia"], an["nome_tecnologia"] = "nextjs", "Node · Next.js"
        porta = 3000
        an["dockerfile"] = "\n".join([
            "# Gerado pelo Portal de Soluções LOG20 a partir do repositório. Revise antes de usar.",
            f"FROM node:{v}-slim", "WORKDIR /app", f"COPY {copiar_pkg}", f"RUN {instalar}", "COPY . .",
            "ENV NEXT_TELEMETRY_DISABLED=1", "RUN npm run build", "ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0",
            "USER node", f"EXPOSE {porta}",
            "HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \\\n"
            "  CMD node -e \"fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))\"",
            'CMD ["npm", "start"]']) + "\n"
        an.update(porta_container=porta, caminho_saude="/", comando="next build + next start")
        return

    if estatico and not servidor:
        saida = "build" if "react-scripts" in deps else "dist"
        an["arquivos"]["nginx.conf"] = ARQ_NGINX
        an["tecnologia"], an["nome_tecnologia"] = "spa", "Site estatico (" + (
            "Vite" if "vite" in deps else "React" if "react-scripts" in deps else "Vue" if "@vue/cli-service" in deps else "Parcel") + ")"
        an["dockerfile"] = "\n".join([
            "# Gerado pelo Portal de Soluções LOG20 a partir do repositório. Revise antes de usar.",
            f"FROM node:{v}-slim AS build", "WORKDIR /app", f"COPY {copiar_pkg}", f"RUN {instalar}", "COPY . .",
            "RUN npm run build", "", "FROM nginx:1.27-alpine", NGINX_SPA,
            f"COPY --from=build /app/{saida} /usr/share/nginx/html", "EXPOSE 80"]) + "\n"
        an.update(porta_container=80, caminho_saude="/", comando=f"npm run build -> nginx servindo {saida}/")
        if "build" not in scripts:
            an["avisos"].append(Aviso("atencao", "Sem script 'build' no package.json",
                                      "O Dockerfile roda 'npm run build'. Ajuste para o comando que gera o site.", ["package.json"]))
        api_fixa = [a for a in repo.com_extensao(".js", ".ts", ".jsx", ".tsx", ".vue", ".env")
                    if re.search(r"https?://(localhost|127\.0\.0\.1):\d+", repo.ler(a))]
        if api_fixa:
            an["avisos"].append(Aviso("atencao", "Endereço localhost no frontend",
                                      "O navegador de quem usa vai procurar a API no próprio computador. Use caminhos "
                                      "relativos (/api/...) ou uma variável de ambiente de build.", api_fixa[:5]))
        return

    an["tecnologia"] = "node"
    an["nome_tecnologia"] = "Node · " + (", ".join(sorted(servidor)) if servidor else "aplicacao")
    codigo = [a for a in repo.com_extensao(".js", ".mjs", ".cjs", ".ts") if not a.startswith(("public/", "frontend/"))]
    porta = _porta_node(repo, codigo[:200])
    saude = _rota_saude(repo, codigo[:300]) or "/"
    build = "RUN npm run build" if "build" in scripts else ""
    linhas = ["# Gerado pelo Portal de Soluções LOG20 a partir do repositório. Revise antes de usar.",
              f"FROM node:{v}-slim", "WORKDIR /app", f"COPY {copiar_pkg}"]
    if build:
        linhas += [f"RUN {instalar}", "COPY . .", build, "RUN npm prune --omit=dev || true"]
    else:
        linhas += [f"RUN {instalar_prod}", "COPY . ."]
    linhas += [f"ENV NODE_ENV=production PORT={porta}", "RUN mkdir -p /app/data && chown node /app /app/data", "USER node",
               f"EXPOSE {porta}",
               "HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \\\n"
               f"  CMD node -e \"fetch('http://127.0.0.1:{porta}{saude}').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))\""]
    if "start" in scripts:
        linhas.append('CMD ["npm", "start"]')
        an["comando"] = f"npm start ({scripts['start']})"
    else:
        principal = pkg.get("main") or next((a for a in ("server.js", "index.js", "app.js", "src/index.js", "src/server.js")
                                             if repo.existe(a)), "index.js")
        linhas.append(f'CMD ["node", "{principal}"]')
        an["comando"] = f"node {principal}"
        an["avisos"].append(Aviso("info", "Sem script 'start'", f"O Dockerfile inicia com 'node {principal}'.", ["package.json"]))
    an["dockerfile"] = "\n".join(l for l in linhas if l) + "\n"
    an.update(porta_container=porta, caminho_saude=saude, volumes={"dados": "/app/data"},
              ambiente={"TZ": "America/Sao_Paulo"})
    texto = "\n".join(repo.ler(a) for a in codigo[:300])
    if re.search(r"process\.env\.DATABASE_URL", texto):
        an["banco"] = {"DATABASE_URL": "{db_url}"}
        an["configuracoes"].append({"nome": "DATABASE_URL", "rotulo": "Banco de dados (string de conexão)", "tipo": "segredo",
                                    "ajuda": "Preenchido pelo portal com o banco do servidor. Para outro banco, troque aqui."})
    if re.search(r"process\.env\.JWT_SECRET", texto):
        an["segredos"].append("JWT_SECRET")
    if re.search(r"process\.env\.SESSION_SECRET", texto):
        an["segredos"].append("SESSION_SECRET")


# ---------------------------------------------------------------- outros

def _analisar_php(repo: Repo, an: dict) -> None:
    raiz_web = "public" if repo.existe("public/index.php") else ""
    linhas = ["# Gerado pelo Portal de Soluções LOG20 a partir do repositório. Revise antes de usar.",
              "FROM php:8.3-apache", "RUN docker-php-ext-install pdo pdo_mysql mysqli"]
    if repo.existe("composer.json"):
        linhas += ["COPY --from=composer:2 /usr/bin/composer /usr/bin/composer",
                   "RUN apt-get update && apt-get install -y --no-install-recommends unzip && rm -rf /var/lib/apt/lists/*"]
    linhas += ["WORKDIR /var/www/html", "COPY . ."]
    if repo.existe("composer.json"):
        linhas.append("RUN composer install --no-dev --optimize-autoloader --no-interaction")
    if raiz_web:
        linhas += ["ENV APACHE_DOCUMENT_ROOT=/var/www/html/public",
                   "RUN sed -ri 's!/var/www/html!${APACHE_DOCUMENT_ROOT}!g' /etc/apache2/sites-available/*.conf "
                   "/etc/apache2/apache2.conf && a2enmod rewrite"]
    linhas += ["RUN chown -R www-data:www-data /var/www/html", "EXPOSE 80"]
    an.update(dockerfile="\n".join(linhas) + "\n", porta_container=80, caminho_saude="/",
              comando="Apache + PHP 8.3" + (" (raiz em public/)" if raiz_web else ""))
    if repo.existe("artisan"):
        an["tecnologia"], an["nome_tecnologia"] = "laravel", "PHP · Laravel"
        an["avisos"].append(Aviso("atencao", "Laravel precisa de ajustes manuais",
                                  "Defina APP_KEY (php artisan key:generate --show) e o banco nas configurações, e de "
                                  "permissão de escrita em storage/ e bootstrap/cache/. Revise o Dockerfile.", ["artisan"]))


def _analisar_dotnet(repo: Repo, an: dict) -> None:
    projs = [a for a in repo.com_extensao(".csproj") if "test" not in a.lower()]
    proj = sorted(projs, key=lambda a: (a.count("/"), a))[0]
    texto = repo.ler(proj)
    m = re.search(r"<TargetFramework>net(\d+)\.0", texto)
    versao = f"{m.group(1)}.0" if m else "8.0"
    nome = re.search(r"<AssemblyName>([^<]+)", texto)
    dll = (nome.group(1) if nome else Path(proj).stem) + ".dll"
    an.update(porta_container=8080, caminho_saude=_rota_saude(repo, repo.com_extensao(".cs")[:300]) or "/",
              comando=f"dotnet {dll}", volumes={"dados": "/app/data"}, ambiente={"TZ": "America/Sao_Paulo"},
              dockerfile="\n".join([
                  "# Gerado pelo Portal de Soluções LOG20 a partir do repositório. Revise antes de usar.",
                  f"FROM mcr.microsoft.com/dotnet/sdk:{versao} AS build", "WORKDIR /src", "COPY . .",
                  f'RUN dotnet publish "{proj}" -c Release -o /out', "",
                  f"FROM mcr.microsoft.com/dotnet/aspnet:{versao}", "WORKDIR /app", "COPY --from=build /out .",
                  "ENV ASPNETCORE_URLS=http://0.0.0.0:8080 ASPNETCORE_FORWARDEDHEADERS_ENABLED=true",
                  "RUN mkdir -p /app/data && chown $APP_UID /app/data", "USER $APP_UID", "EXPOSE 8080",
                  f'ENTRYPOINT ["dotnet", "{dll}"]']) + "\n")
    if "UseHttpsRedirection" in "\n".join(repo.ler(a) for a in repo.com_extensao(".cs")[:300]):
        an["avisos"].append(Aviso("atencao", "UseHttpsRedirection ativo",
                                  "Sem HTTPS no servidor, o sistema redireciona para https:// e não abre. Desligue fora do "
                                  "ambiente de desenvolvimento ou quando o portal ainda não tiver dominio.", []))


def _analisar_java(repo: Repo, an: dict) -> None:
    maven = repo.existe("pom.xml")
    texto = repo.ler("pom.xml") if maven else repo.ler("build.gradle") + repo.ler("build.gradle.kts")
    m = re.search(r"<java\.version>(\d+)|JavaVersion\.VERSION_(\d+)|languageVersion\.set\(JavaLanguageVersion\.of\((\d+)", texto)
    versao = next((g for g in (m.groups() if m else ()) if g), "21")
    if maven:
        build = [f"FROM maven:3.9-eclipse-temurin-{versao} AS build", "WORKDIR /src", "COPY . .",
                 "RUN mvn -q -DskipTests package"]
        jar = "/src/target/*.jar"
    else:
        build = [f"FROM gradle:8-jdk{versao} AS build", "WORKDIR /src", "COPY . .",
                 "RUN gradle bootJar --no-daemon -x test"]
        jar = "/src/build/libs/*.jar"
    actuator = "actuator" in texto
    an.update(porta_container=8080, caminho_saude="/actuator/health" if actuator else "/",
              comando="java -jar app.jar", ambiente={"TZ": "America/Sao_Paulo", "SERVER_FORWARD_HEADERS_STRATEGY": "framework"},
              dockerfile="\n".join(["# Gerado pelo Portal de Soluções LOG20 a partir do repositório. Revise antes de usar.",
                                    *build, "", f"FROM eclipse-temurin:{versao}-jre", "WORKDIR /app",
                                    f"COPY --from=build {jar} /app/app.jar", "EXPOSE 8080",
                                    'ENTRYPOINT ["java", "-jar", "/app/app.jar"]']) + "\n")
    an["avisos"].append(Aviso("info", "Um único .jar esperado",
                              "Se o build gerar mais de um .jar (ex.: -plain.jar), ajuste a linha COPY do Dockerfile.", []))


def _analisar_estatico(repo: Repo, an: dict) -> None:
    an.update(porta_container=80, caminho_saude="/", comando="nginx servindo os arquivos",
              dockerfile="\n".join(["# Gerado pelo Portal de Soluções LOG20 a partir do repositório. Revise antes de usar.",
                                    "FROM nginx:1.27-alpine", "COPY . /usr/share/nginx/html", "EXPOSE 80"]) + "\n")


def _analisar_dockerfile(repo: Repo, an: dict, caminho: str) -> None:
    texto = repo.ler(caminho)
    m = re.search(r"^\s*EXPOSE\s+(\d+)", texto, re.M | re.I)
    porta = int(m.group(1)) if m else 8080
    if not m:
        an["avisos"].append(Aviso("atencao", "Dockerfile sem EXPOSE",
                                  "Não consegui descobrir a porta da aplicação; confira o campo 'Porta da aplicação'.", [caminho]))
    bloco = "\n".join(re.findall(r"^\s*HEALTHCHECK(?:[^\n]*\\\n)*[^\n]*", texto, re.M))
    h = (re.search(r"https?://[^/\s'\"]+(/[^\s'\")]*)", bloco)
         or re.search(r"[\"'](/[\w/.-]*(?:health|saude|status|ping)[\w/.-]*)[\"']", bloco))
    an.update(porta_container=porta, caminho_saude=(h.group(1) if h else None) or "/", comando="Dockerfile do repositório",
              dockerfile=None, tem_dockerfile=True, arquivos={})
    if re.search(r"^\s*USER\s+", texto, re.M) is None:
        an["avisos"].append(Aviso("info", "Container roda como administrador (root)",
                                  "Recomendado: um USER sem privilégios no Dockerfile.", [caminho]))
    if not re.search(r"^\s*HEALTHCHECK", texto, re.M):
        an["avisos"].append(Aviso("info", "Dockerfile sem HEALTHCHECK",
                                  "O portal testa a rota de saúde por fora; um HEALTHCHECK deixa o status mais confiável.", [caminho]))


# ---------------------------------------------------------------- avisos gerais (seguranca e LGPD)

SEGREDOS_CODIGO = [
    (r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}", "Chave da OpenAI"),
    (r"AKIA[0-9A-Z]{16}", "Chave da AWS"),
    (r"(?:ghp|gho|ghs)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}", "Token do GitHub"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "Chave privada"),
    (r"postgres(?:ql)?://[^:\s\"'/@]+:[^@\s\"'{}$]{3,}@", "Senha de banco numa string de conexão"),
    (r"mongodb(?:\+srv)?://[^:\s\"'/@]+:[^@\s\"'{}$]{3,}@", "Senha de banco numa string de conexão"),
    (r"xox[bpa]-[A-Za-z0-9-]{10,}", "Token do Slack"),
]
CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")


def _avisos_gerais(repo: Repo) -> list[Aviso]:
    avisos: list[Aviso] = []
    env_reais = [a for a in repo.arquivos if re.search(r"(^|/)\.env(\.[\w-]+)?$", a)
                 and not re.search(r"\.(example|exemplo|sample|template|dist)$", a)]
    chaves = [a for a in repo.arquivos if re.search(r"(^|/)(id_rsa|id_ed25519)$|\.(pem|key|p12|pfx)$|credentials\.json$|service-account.*\.json$", a)]
    if env_reais or chaves:
        avisos.append(Aviso("erro", "Arquivo de senhas ou chaves no repositório",
                            "Quem tem acesso ao repositório ve essas senhas. Remova do Git (e do histórico), troque as senhas "
                            "e use as Configurações do portal.", (env_reais + chaves)[:8]))

    achados: dict[str, list[str]] = {}
    for arq in repo.arquivos:
        if Path(arq).suffix.lower() not in TEXTO_EXT or arq.endswith((".min.js", "package-lock.json", "yarn.lock")):
            continue
        texto = repo.ler(arq)
        for padrao, nome in SEGREDOS_CODIGO:
            for m in re.finditer(padrao, texto):
                trecho = m.group(0)
                # Exemplos de documentacao (usuario:senha@, <senha>, ***) nao contam.
                if re.search(r"(?i)senha|password|passwd|pass@|usuario:|user:|<|\*\*\*|xxx|example|exemplo|changeme", trecho):
                    continue
                achados.setdefault(nome, []).append(arq)
                break
    for nome, arqs in achados.items():
        avisos.append(Aviso("erro", f"{nome} no código",
                            "Senha ou chave escrita no código. Troque-a (considere vazada) e leia de uma variável de ambiente.",
                            arqs[:5]))

    planilhas = [a for a in repo.arquivos if Path(a).suffix.lower() in {".xlsx", ".xls", ".xlsm", ".ods"}]
    bancos = [a for a in repo.arquivos if Path(a).suffix.lower() in {".sqlite", ".sqlite3", ".db", ".dump", ".bak", ".mdb", ".accdb"}]
    com_cpf = [a for a in repo.arquivos if Path(a).suffix.lower() in {".csv", ".txt", ".sql", ".json"}
               and len(CPF.findall(repo.ler(a))) >= 3]
    if com_cpf:
        avisos.append(Aviso("erro", "Arquivos com CPF no repositório",
                            "Possíveis dados pessoais (LGPD). Remova do repositório e do histórico do Git; dados reais "
                            "entram pelo sistema depois de publicado.", com_cpf[:8]))
    if planilhas or bancos:
        avisos.append(Aviso("atencao", "Planilhas ou bancos de dados no repositório",
                            "Confira se não tem dados reais de pessoas. Se tiver, remova do repositório e do histórico.",
                            (bancos + planilhas)[:8]))

    forca_https = [a for a in repo.arquivos if Path(a).suffix.lower() in {".py", ".js", ".ts", ".mjs", ".cjs"}
                   and re.search(r"SECURE_SSL_REDIRECT\s*=\s*True|upgrade-insecure-requests|upgradeInsecureRequests|require\([\"']helmet[\"']\)|from [\"']helmet[\"']", repo.ler(a))]
    if forca_https:
        avisos.append(Aviso("atencao", "Forca HTTPS",
                            "Enquanto o servidor estiver em http://, o navegador vai tentar https e a tela fica em branco. "
                            "Torne isso configurável (no helmet, o padrão inclui upgrade-insecure-requests).", forca_https[:5]))
    if repo.tem_node_modules:
        avisos.append(Aviso("atencao", "Pasta node_modules no repositório",
                            "Deixa o repositório pesado e pode quebrar o build. Adicione ao .gitignore e remova.", ["node_modules/"]))
    if repo.grandes:
        avisos.append(Aviso("info", "Arquivos grandes", "Arquivos acima de 50 MB deixam o clone e o build lentos.",
                            [f"{a} ({t // (1024 * 1024)} MB)" for a, t in repo.grandes[:5]]))
    return avisos


# ---------------------------------------------------------------- variaveis de ambiente lidas pelo codigo

# Variaveis que o portal ou o container ja resolvem: nao viram configuracao.
VARS_DO_SISTEMA = {
    "PORT", "HOST", "HOSTNAME", "PATH", "HOME", "PWD", "USER", "LANG", "TZ", "TERM", "SHELL", "CI", "NODE_ENV",
    "PYTHONPATH", "PYTHONUNBUFFERED", "DJANGO_SETTINGS_MODULE", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "DEBUG",
    "ASPNETCORE_ENVIRONMENT", "ASPNETCORE_URLS", "JAVA_OPTS", "WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "APP_ENV",
    "FLASK_APP", "FLASK_ENV", "FLASK_DEBUG", "ENVIRONMENT", "ENV", "STREAMLIT_SERVER_PORT", "NEXT_TELEMETRY_DISABLED",
}
# Endereco do proprio sistema: vai fixo no ambiente como {url}, ninguem precisa digitar.
VARS_ENDERECO = re.compile(r"^(APP|BASE|SITE|PUBLIC|FRONTEND|SERVER|WEB|ROOT|HOST)_?URL$")
VAR_SEGREDO = re.compile(r"SECRET|PASSWORD|PASSWD|PASS$|SENHA|TOKEN$|_TOKEN_|API_?KEY|PRIVATE_KEY|CREDENTIAL|_KEY$|^KEY$|DSN$|DATABASE_URL|_URI$|_URL_DB")
# Pastas cujo codigo nao roda no servidor (scripts avulsos, migracoes, testes, frontend compilado).
PASTAS_SEM_VARS = re.compile(r"(^|/)(scripts?|migrations?|seeds?|tests?|__tests__|spec|fixtures|docs?|frontend|client|web|public|static)/", re.I)
VAR_BOOL = re.compile(r"ENABLE|ENABLED|^USE_|_USE_|DISABLE|^DEBUG_|_DEBUG$|ATIVO|ATIVAR|^IS_|^HAS_")
VAR_GERADA = re.compile(r"^(SECRET_KEY|SECRET|APP_SECRET|APP_KEY_SECRET|JWT_SECRET|JWT_SECRET_KEY|SESSION_SECRET|COOKIE_SECRET|FLASK_SECRET_KEY|ENCRYPTION_KEY)$")
VARS_BUILD = re.compile(r"^(VITE_|REACT_APP_|NEXT_PUBLIC_|VUE_APP_|NUXT_PUBLIC_|PUBLIC_|EXPO_PUBLIC_)")
# import.meta.env que o proprio Vite preenche.
VARS_VITE_INTERNAS = {"BASE_URL", "MODE", "DEV", "PROD", "SSR"}
# Arquivos de variaveis que o Vite/CRA leem na construcao (o .env.local costuma ficar fora do Git).
ARQS_ENV_FRONT = re.compile(r"(^|/)\.env(\.local|\.production|\.production\.local|\.development)?$")
_NOME = r"([A-Z][A-Z0-9_]{1,63})"
_LIT = r"""\s*(?:"([^"\n]{0,200})"|'([^'\n]{0,200})'|(-?\d+(?:\.\d+)?|True|False|true|false|None|null))"""

# (regex, tem_valor_padrao, obrigatoria_sem_padrao): grupo 1 = nome; grupos 2-4 = valor padrao (quando ha).
PADROES_VARS = [
    # Python
    (r"os\.environ\[\s*[\"']" + _NOME + r"[\"']\s*\]", False, True),
    (r"(?:os\.environ\.get|os\.getenv|getenv)\(\s*[\"']" + _NOME + r"[\"']\s*(?:,(?:\s*default\s*=)?" + _LIT + r")?", True, False),
    (r"\benv(?:\.(?:str|bool|int|float|list|db|url|json))?\(\s*[\"']" + _NOME + r"[\"']\s*(?:,\s*default\s*=" + _LIT + r")?", True, True),
    (r"\bconfig\(\s*[\"']" + _NOME + r"[\"']\s*(?:,\s*default\s*=" + _LIT + r")?", True, True),
    # Node
    (r"process\.env\.([A-Z][A-Z0-9_]{1,63})(?:\s*(?:\|\||\?\?)" + _LIT + r")?", True, False),
    # Vite (frontend): lidas na construcao
    (r"import\.meta\.env\.([A-Z][A-Z0-9_]{1,63})(?:\s*(?:\|\||\?\?)" + _LIT + r")?", True, False),
    (r"process\.env\[\s*[\"']" + _NOME + r"[\"']\s*\](?:\s*(?:\|\||\?\?)" + _LIT + r")?", True, False),
    # PHP
    (r"(?:getenv|env)\(\s*[\"']" + _NOME + r"[\"']\s*(?:," + _LIT + r")?", True, False),
    (r"\$_(?:ENV|SERVER)\[\s*[\"']" + _NOME + r"[\"']\s*\]", False, False),
    # .NET / Java
    (r"Environment\.GetEnvironmentVariable\(\s*\"" + _NOME + r"\"", False, False),
    (r"System\.getenv\(\s*\"" + _NOME + r"\"\s*\)", False, False),
    (r"\$\{" + _NOME + r"(?::([^}]{0,200}))?\}", True, False),  # application.properties / yml
]
EXT_VARS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".php", ".cs", ".java", ".kt", ".properties", ".yml", ".yaml"}


def _exemplos_env(repo: Repo) -> dict[str, tuple[str, str]]:
    """Variaveis de .env.example/.env.sample: nome -> (valor de exemplo, comentario acima)."""
    vars_: dict[str, tuple[str, str]] = {}
    for arq in repo.arquivos:
        if not re.search(r"(^|/)\.env\.(example|exemplo|sample|template|dist)$|(^|/)env\.(example|exemplo|sample)$", arq):
            continue
        comentario = ""
        for linha in repo.ler(arq).splitlines():
            linha = linha.strip()
            if linha.startswith("#"):
                texto_com = linha.lstrip("# ").strip()
                # "# DATABASE_URL=..." e uma variavel comentada, nao uma explicacao.
                comentario = "" if re.match(r"[A-Z][A-Z0-9_]+\s*=", texto_com) else texto_com
                continue
            m = re.match(r"(?:export\s+)?([A-Z][A-Z0-9_]{1,63})\s*=\s*(.*)$", linha)
            if m:
                valor = m.group(2).strip().strip("\"'")
                vars_.setdefault(m.group(1), (valor, comentario))
            comentario = ""
    return vars_


def _arquivos_env(repo: Repo) -> dict[str, tuple[str, str]]:
    """Variaveis de .env, .env.local, .env.production... que estejam no repositorio (sites estaticos).
    O valor so serve de padrao quando nao parece segredo (quem decide e _variaveis)."""
    vars_: dict[str, tuple[str, str]] = {}
    for arq in repo.arquivos:
        if not ARQS_ENV_FRONT.search(arq):
            continue
        for linha in repo.ler(arq).splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,63})\s*=\s*(.*)$", linha)
            if m:
                vars_.setdefault(m.group(1), (m.group(2).strip().strip("\"'"), f"Está em {arq}."))
    return vars_


def _parece_exemplo(valor: str) -> bool:
    return bool(re.search(r"(?i)troque|change|your|seu|sua|xxx|<|exemplo|example|aqui|here|placeholder|\*\*\*|senha|password|secret", valor))


def _variaveis(repo: Repo, an: dict) -> None:
    """Acha as variáveis de ambiente que o código le e propoe cada uma como configuração."""
    achadas: dict[str, dict] = {}
    for arq in repo.arquivos:
        if (Path(arq).suffix.lower() not in EXT_VARS or "test" in arq.lower() or arq.endswith(".min.js")
                or (PASTAS_SEM_VARS.search(arq) and Path(arq).suffix.lower() not in {".properties", ".yml", ".yaml"})):
            continue
        texto = repo.ler(arq)
        for padrao, aceita_padrao, obrig in PADROES_VARS:
            # ${NOME} so e variavel em application.properties/yml; em JS/TS e template de texto.
            if padrao.startswith(r"\$\{") and Path(arq).suffix.lower() not in {".properties", ".yml", ".yaml"}:
                continue
            for m in re.finditer(padrao, texto):
                nome = m.group(1)
                padrao_valor = next((g for g in m.groups()[1:] if g is not None), None) if aceita_padrao else None
                if padrao_valor in ("None", "null"):
                    padrao_valor = None
                # env("X", default=algo_calculado): tem padrao, so nao e um valor fixo que da para mostrar.
                if obrig and padrao_valor is None and re.match(r"\s*,", texto[m.end():m.end() + 60]):
                    obrig = False
                v = achadas.setdefault(nome, {"arquivos": [], "padrao": None, "obrigatoria": True, "bool": False})
                if m.group(0).startswith(("env.bool", "config(")) and "cast=bool" in texto[m.end():m.end() + 80] or m.group(0).startswith("env.bool"):
                    v["bool"] = True
                if arq not in v["arquivos"]:
                    v["arquivos"].append(arq)
                if padrao_valor is not None:
                    v["padrao"] = padrao_valor
                # Obrigatoria so se todos os usos sao do tipo que quebra sem valor (ex.: os.environ["X"]).
                v["obrigatoria"] = v["obrigatoria"] and obrig and padrao_valor is None
    site = an["tecnologia"] == "spa"  # tudo o que um site estatico le vem da construcao
    apelidos: set[str] = set()
    lidas_config: set[str] = set()
    if site or an["tecnologia"] == "nextjs":
        for arq in repo.arquivos:
            if not re.match(r"^(vite|next)\.config\.(ts|js|mjs|cjs|mts)$", Path(arq).name):
                continue
            texto = repo.ler(arq)
            # define: { 'process.env.API_KEY': JSON.stringify(env.GEMINI_API_KEY) } -> API_KEY e so um apelido.
            apelidos |= set(re.findall(r"""['"]process\.env\.([A-Z_][A-Z0-9_]*)['"]\s*:""", texto))
            # Lidas de verdade: env.X / env['X'] (loadEnv) e process.env.X fora de aspas (as chaves do
            # define ficam entre aspas e nao contam).
            for m in re.finditer(r"""(?<![.\w])env\.([A-Z][A-Z0-9_]{1,63})\b|(?<![.\w])env\[\s*['"]([A-Z][A-Z0-9_]{1,63})['"]\s*\]"""
                                 r"""|(?<!['"])process\.env\.([A-Z][A-Z0-9_]{1,63})\b""", texto):
                nome = next(g for g in m.groups() if g)
                lidas_config.add(nome)
                v = achadas.setdefault(nome, {"arquivos": [], "padrao": None, "obrigatoria": False, "bool": False})
                v["obrigatoria"] = False
                if arq not in v["arquivos"]:
                    v["arquivos"].append(arq)
        for nome in apelidos - lidas_config:
            achadas.pop(nome, None)
    exemplos = _exemplos_env(repo)
    if site:
        exemplos = {**_arquivos_env(repo), **exemplos}
    for nome, (valor, comentario) in exemplos.items():
        v = achadas.setdefault(nome, {"arquivos": [], "padrao": None, "obrigatoria": False})
        v["exemplo"], v["comentario"] = valor, comentario
        if v["padrao"] is None and valor and not _parece_exemplo(valor) and not VAR_SEGREDO.search(nome):
            v["padrao"] = valor

    ja = {c["nome"] for c in an["configuracoes"]} | set(an["segredos"]) | set(an["ambiente"]) | set(an.get("banco") or {})
    expostas = []
    for nome in sorted(achadas):
        v = achadas[nome]
        if nome in VARS_DO_SISTEMA or nome in ja or nome in VARS_VITE_INTERNAS:
            continue
        construcao = site or bool(VARS_BUILD.match(nome))
        if not construcao and VARS_ENDERECO.match(nome):
            an["ambiente"][nome] = "{url}"
            continue
        # Chave interna do proprio sistema (assinar sessoes/tokens): o portal gera e guarda sozinho.
        if not construcao and VAR_GERADA.search(nome) and not re.search(r"API|CLIENT|OAUTH|AWS|STRIPE|GOOGLE", nome):
            if nome not in an["segredos"]:
                an["segredos"].append(nome)
            continue
        padrao_txt = "" if v["padrao"] is None else str(v["padrao"])
        if VAR_SEGREDO.search(nome):
            tipo, padrao_txt = "segredo", ""
        elif v.get("bool") or (padrao_txt.lower() in ("true", "false", "1", "0") and (VAR_BOOL.search(nome) or padrao_txt.lower() in ("true", "false"))):
            tipo, padrao_txt = "booleano", "true" if padrao_txt.lower() in ("true", "1") else "false"
        else:
            tipo = "texto"
        onde = ", ".join(v["arquivos"][:2]) or "arquivo de exemplo de variáveis"
        ajuda = v.get("comentario") or ""
        ajuda = (ajuda + " " if ajuda else "") + f"Lida em {onde}."
        if v["obrigatoria"]:
            ajuda += " O código não tem valor padrão: sem ela o sistema não sobe."
        if construcao:
            ajuda += " Entra no site na construção: depois de mudar, clique em Atualizar."
            if VAR_SEGREDO.search(nome):
                expostas.append(nome)
        an["configuracoes"].append({"nome": nome, "rotulo": nome, "tipo": tipo, "padrao": padrao_txt, "ajuda": ajuda,
                                    "obrigatoria": bool(v["obrigatoria"] and not padrao_txt), "detectada": True,
                                    "construcao": construcao})
        if len(an["configuracoes"]) >= 40:
            break
    if expostas:
        an["avisos"].append(Aviso(
            "atencao", "Chave secreta dentro do site",
            "Estas variáveis são gravadas no site quando ele é construído: qualquer pessoa que abrir o sistema "
            "consegue ler o valor pelo navegador (F12). Para chaves de API pagas (Gemini, OpenAI...), o certo é o "
            "site chamar um servidor que guarda a chave. Se for usar assim mesmo, crie uma chave só para este "
            "sistema, com limite de gastos.", expostas[:8]))


# ---------------------------------------------------------------- Supabase

PACOTES_SUPABASE = ("@supabase/supabase-js", "@supabase/ssr")
RE_SB_URL = re.compile(r"SUPABASE_URL$")
RE_SB_CHAVE = re.compile(r"SUPABASE_(ANON|PUBLISHABLE)(_DEFAULT)?_KEY$|SUPABASE_KEY$")
RE_SB_SERVICO = re.compile(r"SUPABASE_(SERVICE(_ROLE)?|SECRET)_KEY$")


def _supabase(repo: Repo, an: dict) -> None:
    """Projeto que usa o Supabase (supabase-js): o portal cria um Supabase so para ele e preenche o
    endereco e a chave; essas variaveis saem da lista de configuracoes que o administrador preenche."""
    if not repo.existe("package.json"):
        return
    pkg = repo.json("package.json")
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    if not any(p in deps for p in PACOTES_SUPABASE):
        return
    nomes = [c["nome"] for c in an["configuracoes"]]
    publica = lambda n: bool(VARS_BUILD.match(n))  # noqa: E731
    escolher = lambda r: next((n for n in sorted(nomes, key=lambda n: not publica(n)) if r.search(n)), "")  # noqa: E731
    site = an["tecnologia"] in ("spa", "nextjs")
    url = escolher(RE_SB_URL) or ("VITE_SUPABASE_URL" if an["tecnologia"] == "spa" else "")
    chave = escolher(RE_SB_CHAVE) or ("VITE_SUPABASE_ANON_KEY" if an["tecnologia"] == "spa" else "")
    servico = next((n for n in nomes if RE_SB_SERVICO.search(n)), "")
    if servico and publica(servico):
        an["avisos"].append(Aviso(
            "erro", "Chave de serviço do Supabase dentro do site",
            f"{servico} é gravada no site e dá acesso total ao banco, sem as regras (RLS), a qualquer pessoa "
            "que abrir o sistema. O portal não preenche essa variável; o site deve usar só a chave pública (anon).",
            [servico]))
        servico = ""
    if site and servico:
        servico = ""  # site estatico: nada roda no servidor que possa guardar a chave
    an["supabase"] = {"url": url, "chave_anon": chave, "url_interna": "", "chave_servico": servico}
    gerenciadas = {n for n in an["supabase"].values() if n}
    an["configuracoes"] = [c for c in an["configuracoes"] if c["nome"] not in gerenciadas]
    # A chave anon e publica de proposito (as regras RLS protegem os dados): nao e "segredo exposto".
    for av in list(an["avisos"]):
        if av.titulo == "Chave secreta dentro do site":
            av.arquivos = [n for n in av.arquivos if n not in gerenciadas]
            if not av.arquivos:
                an["avisos"].remove(av)
    texto = "\n".join(repo.ler(a) for a in repo.com_extensao(".js", ".ts", ".jsx", ".tsx", ".vue", ".mjs")[:600])
    an["avisos"].append(Aviso(
        "info", "Usa o Supabase: o portal cria um só para este sistema",
        "Banco, login e arquivos ficam num Supabase próprio, no servidor, no mesmo endereço do sistema. "
        f"O portal preenche {', '.join(sorted(gerenciadas)) or 'as variáveis do Supabase'} sozinho. O banco começa vazio: "
        "traga as tabelas e os dados do Supabase de origem.", sorted(gerenciadas)))
    if re.search(r"\.channel\(|\.subscribe\(\s*\)|postgres_changes", texto):
        an["avisos"].append(Aviso(
            "atencao", "Usa Realtime do Supabase",
            "O Supabase do servidor não tem o serviço de tempo real: telas que atualizam sozinhas param de "
            "atualizar (o resto funciona).", []))
    if re.search(r"functions\.invoke\(", texto) or repo.existe("supabase/functions"):
        an["avisos"].append(Aviso(
            "atencao", "Usa Edge Functions do Supabase",
            "O Supabase do servidor não roda Edge Functions: as chamadas a functions.invoke vão falhar.", []))


# ---------------------------------------------------------------- entrada

def analisar(pasta: Path, nome_repo: str = "") -> Analise:
    repo = Repo(pasta)
    an: dict = {"tecnologia": "desconhecida", "nome_tecnologia": "Não identificada", "evidencias": [], "suportado": True,
                "tem_dockerfile": False, "dockerfile": None, "porta_container": 8080, "caminho_saude": "/", "comando": "",
                "ambiente": {}, "volumes": {}, "segredos": [], "banco": None, "supabase": None, "configuracoes": [], "avisos": [],
                "arquivos": {}}
    ev = an["evidencias"]
    py = "\n".join(repo.ler(a) for a in repo.com_extensao(".py")[:400])
    dockerfiles = [a for a in ("Dockerfile", "dockerfile") if repo.existe(a)]

    def marca(tec: str, nome: str, *evidencias: str) -> None:
        an["tecnologia"], an["nome_tecnologia"] = tec, nome
        ev.extend(e for e in evidencias if e)

    if repo.existe("manage.py") and "django" in repo.ler("manage.py").lower():
        marca("django", "Python · Django", "manage.py")
        if not dockerfiles:
            _analisar_django(repo, an)
    elif re.search(r"^\s*(import streamlit|from streamlit)", py, re.M):
        marca("streamlit", "Python · Streamlit", "import streamlit")
        if not dockerfiles:
            _analisar_streamlit(repo, an)
    elif re.search(r"=\s*FastAPI\(", py):
        marca("fastapi", "Python · FastAPI", "FastAPI()")
        if not dockerfiles:
            _analisar_flask_fastapi(repo, an, fastapi=True)
    elif re.search(r"=\s*Flask\(|def create_app\(", py) and "flask" in py.lower():
        marca("flask", "Python · Flask", "Flask()")
        if not dockerfiles:
            _analisar_flask_fastapi(repo, an, fastapi=False)
    elif repo.existe("package.json"):
        marca("node", "Node", "package.json")
        if not dockerfiles:
            _analisar_node(repo, an)
    elif repo.existe("composer.json") or repo.com_extensao(".php"):
        marca("php", "PHP", "composer.json" if repo.existe("composer.json") else "arquivos .php")
        if not dockerfiles:
            _analisar_php(repo, an)
    elif repo.com_extensao(".csproj"):
        marca("dotnet", ".NET (C#)", repo.com_extensao(".csproj")[0])
        if not dockerfiles:
            _analisar_dotnet(repo, an)
    elif repo.existe("pom.xml") or repo.existe("build.gradle") or repo.existe("build.gradle.kts"):
        marca("java", "Java (Maven)" if repo.existe("pom.xml") else "Java (Gradle)",
              "pom.xml" if repo.existe("pom.xml") else "build.gradle")
        if not dockerfiles:
            _analisar_java(repo, an)
    elif repo.existe("index.html"):
        marca("estatico", "Site estatico (HTML)", "index.html")
        if not dockerfiles:
            _analisar_estatico(repo, an)
    elif repo.com_extensao(".py"):
        marca("python", "Python (sem servidor web identificado)", "arquivos .py")
        an["suportado"] = False

    if dockerfiles:
        ev.append(dockerfiles[0])
        _analisar_dockerfile(repo, an, dockerfiles[0])
        if an["tecnologia"] == "desconhecida":
            an["tecnologia"], an["nome_tecnologia"] = "dockerfile", "Projeto com Dockerfile"
        an["suportado"] = True
    elif an["tecnologia"] in ("desconhecida", "python"):
        an["suportado"] = False
        an["avisos"].insert(0, Aviso(
            "erro", "Não consegui preparar este projeto sozinho",
            "Não encontrei um servidor web conhecido (Django, Flask, FastAPI, Streamlit, Node, PHP, .NET, Java ou HTML). "
            "Scripts e rotinas sem tela não rodam como solução do portal. Peça um Dockerfile a quem desenvolveu.", []))

    if an["suportado"]:
        _variaveis(repo, an)
        _supabase(repo, an)
    an["avisos"].extend(_avisos_gerais(repo))
    ordem = {"erro": 0, "atencao": 1, "info": 2}
    an["avisos"].sort(key=lambda a: ordem.get(a.nivel, 3))
    base = nome_repo or pasta.name
    an["id_sugerido"] = _slug(base)
    an["nome_sugerido"] = re.sub(r"[-_]+", " ", base).strip().title() or "Nova solução"
    an["resumo_sugerido"] = _resumo_readme(repo)
    return Analise(**an)

