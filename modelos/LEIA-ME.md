# Publicar uma nova solução no portal

> **Mais fácil:** no portal, **Soluções → Nova solução** analisa o repositório, aponta o que
> precisa de ajuste e gera o manifesto e o Dockerfile. Este modelo é para quem prefere
> escrever o manifesto à mão e publicá-lo pelo Git.

O manifesto (`solucao.toml`) diz ao portal **de onde vem o código** e **como rodar**. O modelo
comentado está em [`solucao/solucao.toml`](solucao/solucao.toml).

## Passo a passo

1. Confira se o repositório da solução atende aos requisitos abaixo.
2. Copie `modelos/solucao/` para `solucoes/<id>/` (o `id` do manifesto, ex.: `solucoes/controle-frota/`).
3. Troque tudo que está marcado com `<<TROCAR>>` e apague as partes opcionais que não usar.
   Se tiver logo, coloque o arquivo na mesma pasta e descomente a linha `logo`.
4. Valide no seu computador, na pasta do portal:
   ```powershell
   python -m marketplace validar
   ```
5. Commit e push do portal. Se o repositório da solução for privado, inclua-o no token de
   leitura de cada servidor (github.com → Settings → Developer settings → Fine-grained tokens →
   editar o token → *Repository access*).
6. No servidor: `git pull` na pasta do portal e, no portal, **Soluções → Recarregar catálogo**.
7. **Instalar e iniciar** e, em **Usuários**, libere a solução para quem deve vê-la.

## Requisitos do repositório da solução

- [ ] **`Dockerfile`** que constrói e roda a aplicação sozinho, sem passos manuais.
- [ ] A aplicação **escuta em `0.0.0.0`** (não só `127.0.0.1`) na porta informada em `porta_container`.
- [ ] **Configuração por variáveis de ambiente**: nada de senha, chave ou URL fixa no código ou na imagem.
- [ ] **Dados em pastas fixas** (ex.: `/app/data`) declaradas em `[execucao.volumes]`; o resto do
      container é descartado a cada atualização.
- [ ] Uma **rota de saúde** que responde 2xx quando está pronta (ex.: `/health`); de preferência
      também um `HEALTHCHECK` no Dockerfile.
- [ ] **Funciona por `http://` num IP** (ex.: `http://192.168.0.10:8103`): sem forçar HTTPS,
      sem `upgrade-insecure-requests` na CSP, links e chamadas de API **relativos** (`/api/...`).
      Use a variável com `{url}` se a aplicação precisar saber o próprio endereço.
- [ ] Sem dados reais de pessoas no repositório (planilhas, bancos, logs).

## Exemplos mínimos de Dockerfile

**Python (FastAPI/Flask com gunicorn ou uvicorn):**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && mkdir -p /app/data && chown -R app /app/data
USER app
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

**Node (Express servindo um frontend já compilado):**

```dockerfile
FROM node:22-trixie-slim
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
USER node
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s \
  CMD node -e "fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
```

O Dockerfile completo da Central do Colaborador (build do frontend + backend, SQLite em volume,
seed na primeira execução) está no repositório `ProjetoLog20/Chatbot-rh-central-colabotador`.
