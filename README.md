# Portal de Soluções LOG20

Portal para disponibilizar soluções conteinerizadas, com login.

- **Usuários** entram com usuário e senha e veem só os sistemas liberados para eles.
- **Administradores** também gerenciam as soluções (instalar, iniciar, parar, atualizar,
  configurar, ver logs) e os usuários (criar, liberar sistemas, redefinir senha, desativar).

Cada solução é descrita por um manifesto (`solucoes/<id>/solucao.toml`). A primeira
publicada é a **Central do Colaborador (Yuri)**.

## Rodar na sua máquina (PowerShell)

Requisitos: Python 3.12, Git e Docker Desktop aberto.

```powershell
git clone https://github.com/JungGustavo/portal-solucoes-log20.git
cd portal-solucoes-log20
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m marketplace criar-admin      # só na primeira vez
python -m marketplace
```

No MSYS2/Git Bash, ative o ambiente com `source .venv/Scripts/activate`.

Se os repositórios das soluções forem privados, defina o token de leitura antes de iniciar
o portal: `$env:PORTAL_GIT_TOKEN = "github_pat_..."`.

Abra http://localhost:8000 e entre com o administrador criado. Em **Soluções**,
abra a Central do Colaborador e clique em **Instalar e iniciar** (o primeiro build leva
alguns minutos). Em **Usuários**, crie as pessoas e marque os sistemas de cada uma.

## Rodar no servidor ou numa VM (Docker Compose)

No servidor tudo roda em Docker: o **portal** (porta 8000) e o **Caddy**, que é a única
entrada das soluções (uma porta por solução, na faixa 8101-8130). As soluções ficam numa rede interna do Docker, sem
portas próprias, então só abrem para quem está logado no portal e tem acesso a elas.

```
navegador ──► :8000  portal ──────────────┐ constrói e inicia as soluções
          └─► :8101  Caddy ─(pergunta ao portal)─► log20-central-colaborador:4000
              :8102  Caddy ──────────────────────► log20-central-colaborador-homologacao:4000
```

### Testar numa VM

Passo a passo completo para entregar a outra pessoa, incluindo como dar acesso aos
repositórios privados: [docs/instalar-em-uma-vm.md](docs/instalar-em-uma-vm.md).


1. **Crie a VM** com Ubuntu Server 24.04 (2 CPUs, 4 GB de RAM e 30 GB de disco bastam).
   - *Hyper-V* (Windows Pro): use o **Default Switch**; a VM ganha um IP que o seu computador alcança.
   - *VirtualBox*: na rede da VM, escolha **Placa em modo Bridge**.
   - Na instalação do Ubuntu, marque **Install OpenSSH server**.
2. **Crie um token de leitura do GitHub** para o portal clonar as soluções privadas:
   github.com → *Settings → Developer settings → Fine-grained tokens → Generate new token*,
   com acesso só aos repositórios das soluções e permissão **Contents: Read-only**. Se o
   repositório do portal também for privado, inclua-o no token: ele serve para o clone abaixo.
3. **Na VM** (pelo `ssh usuario@IP-DA-VM`):
   ```bash
   git clone https://github.com/JungGustavo/portal-solucoes-log20.git
   cd portal-solucoes-log20
   bash deploy/preparar-vm.sh
   ```
   No `git clone` de um repositório privado, use o seu usuário do GitHub e cole o token
   como senha. O script instala o Docker, pede o token (para o portal clonar as soluções),
   cria o `.env` com o IP da VM, sobe portal + Caddy e pede os dados do administrador.
4. **No seu computador**, abra `http://IP-DA-VM:8000`, entre e, em **Soluções**, clique em
   **Instalar e iniciar**: o portal clona o repositório da solução, constrói e sobe.
   Depois teste `http://IP-DA-VM:8101` numa janela anônima.

Para atualizar: **o portal** com `git pull && docker compose up -d --build` na pasta dele;
**uma solução** com o botão **Atualizar** (busca o último commit do branch e reconstrói).

### Instalação manual

```bash
cd portal-solucoes-log20
cp variaveis.exemplo .env          # ajuste o IP e o PORTAL_GIT_TOKEN
docker compose up -d --build
docker compose exec portal python -m marketplace criar-admin
```

A configuração do Caddy é **gerada pelo portal** a partir dos manifestos (em
`dados/caddy/Caddyfile`) e recarregada sozinha; não edite esse arquivo. O Caddy publica a faixa
de portas `8101-8130`, uma por solução; para ampliar, mude a faixa no serviço `caddy` e em
`PORTAL_FAIXA_PORTAS`, no `docker-compose.yml`.

O container do portal recebe acesso ao Docker do servidor (`/var/run/docker.sock`), então
quem é administrador do portal controla o Docker daquela máquina. Crie poucos administradores.

### Com domínio e HTTPS

Para produção, coloque um proxy reverso na frente: veja `deploy/Caddyfile.exemplo` e a
seção comentada do `variaveis.exemplo`. Nesse modo:

- cada solução ganha um subdomínio (ex.: `https://rh.log20.com.br`), configurado em
  **Soluções → Gerenciar → Acesso → Endereço público**;
- as portas das soluções ficam fechadas para a rede (`PORTAL_BIND_SOLUCOES=127.0.0.1`);
- o proxy pergunta ao portal (`/api/auth/verificar`) se a pessoa logada tem acesso à
  solução antes de deixar passar. Quem não tem login vai para o portal; quem não tem
  permissão recebe "acesso negado".

**Importante:** sem esse proxy, o login do portal controla o que cada pessoa *vê*, mas
quem souber o endereço direto de uma solução consegue abri-la (e cai no login próprio da
solução). Para o controle valer também no acesso direto, use o modo com proxy.

## Bloquear o acesso direto na sua máquina (Caddy local)

Sem proxy, o login do portal controla o que cada pessoa *vê*, mas quem souber o endereço
direto de uma solução (ex.: `:8101`) chega até ela. Com o Caddy na frente, cada solução
ganha um endereço protegido que só abre para quem está logado no portal **e** tem acesso a ela.
Sem login, a pessoa vai para o login do portal e volta ao sistema depois de entrar.

Funciona com o mesmo endereço e portas diferentes (os cookies não olham a porta), então não
precisa de domínio nem de certificado:

| Endereço protegido | Solução | Porta interna |
| --- | --- | --- |
| `http://localhost:9101` | Central do Colaborador | 8101 |
| `http://localhost:9102` | Central do Colaborador - Homologação | 8102 |

1. Instale o Caddy: `winget install -e --id CaddyServer.Caddy` (feche e reabra o terminal).
2. Em **Soluções → Gerenciar → Acesso → Endereço público**, informe `http://localhost:9101`
   na Central e `http://localhost:9102` na Homologação, e clique em **Reiniciar** em cada uma
   (o endereço entra na configuração da aplicação).
3. Com o portal rodando, em outro terminal na pasta `portal`:
   `caddy run --config deploy/Caddyfile.local`

Para testar: numa janela anônima, abra `http://localhost:9101` (vai para o login do portal);
entre com um usuário sem acesso à Central (vê "Acesso não liberado"); entre com um usuário
liberado (abre a Central).

No servidor, rode o Caddy na mesma máquina, defina `PORTAL_URL_PUBLICA=http://<ip>:8000` e
use `http://<ip>:9101` como endereço público. As portas 8101/8102 ficam só em `127.0.0.1`
(`PORTAL_BIND_SOLUCOES=127.0.0.1`), então de fora só se chega pelo Caddy.

## Variáveis de ambiente

| Variável | Padrão | Para que serve |
| --- | --- | --- |
| `PORTAL_URL_BASE` | `http://localhost` | Endereço do servidor; cada solução fica em `<base>:<porta>` |
| `PORTAL_BIND_SOLUCOES` | `127.0.0.1` | `0.0.0.0` publica as soluções na rede |
| `PORTAL_HOST` / `PORTAL_PORTA` | `127.0.0.1` / `8000` | Onde o portal escuta |
| `PORTAL_SESSAO_HORAS` | `12` | Duração do login |
| `PORTAL_COOKIE_SEGURO` | `false` | `true` quando houver HTTPS |
| `PORTAL_URL_PUBLICA` | `http://localhost:8000` | Endereço do portal, para onde o proxy manda quem não está logado |
| `PORTAL_COOKIE_DOMINIO` | | Ex.: `.log20.com.br`, para a sessão valer nos subdomínios |
| `PORTAL_PROXIES` | `127.0.0.1` | IPs de proxies confiáveis (`*` atrás de proxy) |
| `PORTAL_HOST_INTERNO` | `127.0.0.1` | Como o portal alcança as soluções para checar se estão no ar |
| `PORTAL_REDE_DOCKER` | | Rede do Docker das soluções (`log20` no compose); o Caddy as alcança pelo nome |
| `PORTAL_PUBLICAR_PORTAS` | `true` | `false` não abre porta para as soluções: só o Caddy na mesma rede chega nelas |
| `PORTAL_GIT_TOKEN` | | Token de leitura para clonar repositórios privados das soluções |
| `PORTAL_GIT_HOST` | `github.com` | Único servidor Git que recebe o token |
| `PORTAL_CADDY_CONTAINER` | | Container do Caddy que o portal configura (`log20-caddy` no compose) |
| `PORTAL_FAIXA_PORTAS` | | Faixa publicada pelo Caddy (`8101-8130` no compose); portas fora dela são recusadas |
| `PORTAL_DOCS_API` | `false` | `true` libera a documentação da API em `/docs` |

## Segurança

- Senhas guardadas com scrypt; a sessão fica num cookie `HttpOnly` e só o hash do token vai
  para o banco (`dados/portal.db`).
- Usuários criados ou com senha redefinida recebem uma senha temporária e são obrigados a
  trocá-la no primeiro acesso. Trocar a senha encerra as outras sessões.
- Após 8 tentativas erradas, o login daquele usuário/IP fica bloqueado por 15 minutos.
- Desativar um usuário encerra as sessões dele na hora. Sempre fica pelo menos um
  administrador ativo, e ninguém consegue tirar o próprio acesso de administrador.
- Segredos das soluções (`JWT_SECRET`, chaves de API) ficam em `dados/`, nunca são enviados
  ao navegador e não aparecem na linha de comando do `docker`.

## Publicar uma nova solução

1. A solução precisa de um repositório Git com um `Dockerfile` que suba a aplicação numa
   porta HTTP e, de preferência, com uma rota de saúde (ex.: `/api/health`).
2. No repositório do portal, copie o modelo comentado `modelos/solucao/` para
   `solucoes/<id>/` e troque as linhas marcadas com `<<TROCAR>>` (requisitos e exemplos de
   Dockerfile em [`modelos/LEIA-ME.md`](modelos/LEIA-ME.md)). Confira com
   `python -m marketplace validar` e faça commit e push.
3. No servidor, na pasta do portal: `git pull`.
4. No portal: **Soluções → Recarregar catálogo**. O portal gera a configuração do Caddy,
   valida e aplica sozinho; a porta nova passa a responder na hora.
5. **Instalar e iniciar** na solução nova e, em **Usuários**, libere-a para quem deve vê-la.

Se o manifesto ou a configuração gerada tiverem algum problema, ele aparece num aviso no
topo da tela de Soluções, e o acesso às soluções que já estavam no ar continua como estava.

| Campo | Para que serve |
| --- | --- |
| `[build] repositorio`, `ref` | Repositório Git (URL `https://`) e branch ou tag de onde o portal clona o código |
| `[build] subpasta`, `dockerfile` | Pasta dentro do repositório onde fica o `Dockerfile` (padrão: a raiz) |
| `[build] contexto` | Alternativa ao repositório: pasta local com o código, relativa ao manifesto |
| `[build] imagem` | Nome da imagem Docker; soluções com o mesmo nome compartilham o build |
| `[execucao] porta_container`, `porta_host` | Porta da aplicação e porta publicada no servidor |
| `caminho_saude`, `caminho_abrir` | Rota para saber se está no ar e rota aberta pelo botão |
| `[execucao.ambiente]` | Variáveis fixas; `{url}` vira o endereço público da solução |
| `[execucao.volumes]` | Pastas do container que devem persistir |
| `segredos` | Variáveis geradas automaticamente uma única vez |
| `logo` | Imagem quadrada da solução (png, jpg, svg ou webp) dentro da pasta dela; sem logo, aparece um monograma |
| `[[configuracoes]]` | Campos editáveis pelo admin (`texto`, `segredo` ou `booleano`) |
| `[[acessos_demo]]` | Usuários de teste exibidos ao admin |

## Comandos úteis

```powershell
python -m marketplace criar-admin                      # novo administrador
docker rm -f log20-central-colaborador                 # apagar o container de uma solução
docker volume rm log20-central-colaborador-dados       # apagar os dados dela (irreversível)
```
