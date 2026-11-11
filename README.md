# Portal de Soluções LOG20

Portal para disponibilizar soluções conteinerizadas, com login.

- **Usuários** entram com usuário e senha e veem só os sistemas liberados para eles.
- **Administradores** também gerenciam as soluções (instalar, iniciar, parar, atualizar,
  configurar, ver logs), os usuários e os acessos:
  - **Usuários**: criar um a um ou **importar em lote** (colar do Excel ou abrir um CSV),
    procurar, filtrar, editar, redefinir senha e, selecionando várias pessoas, liberar ou
    retirar sistemas e desativar/reativar de uma vez;
  - **Acessos**: tabela pessoa × sistema para marcar quem abre o quê (vale na hora) e o
    **histórico** de quem mudou o quê (criações, acessos, perfis, senhas redefinidas).

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

No servidor tudo roda em Docker: o **portal** (porta 8000), o **Caddy**, que é a única
entrada das soluções (uma porta por solução, na faixa 8101-8130), e um **PostgreSQL** para as
soluções que precisam de banco. As soluções e o banco ficam numa rede interna do Docker, sem
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

### Atualizar (um comando)

Na pasta do portal, na VM:

```bash
bash deploy/atualizar.sh                       # portal + soluções novas ou com problema
bash deploy/atualizar.sh --atualizar-solucoes  # também reconstrói as soluções com o último commit
```

O script:
1. faz `git pull` do portal (e para, sem mexer em nada, se alguém alterou arquivos à mão na VM);
2. confere o IP da VM e corrige o `.env` se ele mudou (só quando o `.env` usa IP, não domínio);
3. sobe portal e Caddy e recria o portal, para carregar o código e o `.env` novos;
4. roda `python -m marketplace sincronizar`: instala e inicia as soluções que faltam, reinicia as
   que estão com problema (e todas, se o IP mudou) e regera o Caddy. Soluções paradas de
   propósito continuam paradas.

Uma solução nova só instala se o repositório dela estiver no token de leitura da VM. Para não
precisar editar o token a cada repositório, crie o token com *Repository access → All
repositories* (vale também para os repositórios criados depois; em troca, a VM lê todos os
repositórios da conta).

Também dá para atualizar **uma solução** pelo botão **Atualizar** no portal.

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

## Importar usuários em lote

Em **Usuários → Importar em lote**, cole uma pessoa por linha (ou abra um `.csv`):

```
login;nome;perfil
maria.silva;Maria Silva
10452;Ana Paula Lima;usuario
joao.souza;João Souza;administrador
```

- O separador pode ser `;`, `,` ou tabulação (colar direto do Excel). A linha de cabeçalho é opcional.
- O perfil é opcional (padrão: usuário). Marque os sistemas que todos os importados recebem.
- A prévia aponta login inválido, repetido ou já cadastrado. **Ou cria todos, ou nenhum.**
- No fim, baixe a planilha com as senhas temporárias: elas não aparecem de novo. Entregue
  cada senha só à pessoa dona do login e apague o arquivo depois. No primeiro acesso cada um
  cria a própria senha.
- Máximo de 500 pessoas por vez.

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

## Banco de dados do servidor (PostgreSQL)

O serviço `postgres` do `docker-compose.yml` (container `log20-postgres`, volume
`log20-postgres-dados`) atende as soluções cujo manifesto tem `[banco]`. Ao instalar ou
iniciar uma delas, o portal:

1. cria um **banco e um usuário só daquela solução** (ex.: banco `jornada_lideranca`, usuário
   `app_jornada_lideranca`), com senha aleatória guardada em `dados/` e nunca mostrada na tela.
   Os outros usuários do servidor não conectam nesse banco;
2. na primeira vez, preenche as configurações de banco da solução (servidor, porta, banco,
   usuário, senha), se elas ainda estiverem vazias ou no padrão.

Depois disso o portal não mexe mais nessas configurações: para usar outro banco (ex.: Supabase),
troque os campos em **Configurações** e clique em **Reiniciar**; o banco do servidor deixa de ser
usado e nem precisa estar no ar. O banco fica só na rede interna (nenhuma porta aberta na
máquina) e a conexão não usa SSL, porque não sai do servidor.

A senha do administrador do banco (`PORTAL_POSTGRES_SENHA`) é gerada pelos scripts de deploy e
fica só no `.env`; ninguém precisa digitá-la. O backup diário dos bancos é feito pelo portal
(veja **Backup e restauração** abaixo).

Comandos úteis, na pasta do portal:

```bash
sudo docker compose exec postgres psql -U postgres -c '\l'                  # bancos e donos
sudo docker compose exec postgres psql -U postgres -d jornada_lideranca      # abrir um banco
sudo docker compose exec postgres pg_dump -U postgres -Fc jornada_lideranca > jornada.dump
```

**Pelo navegador:** a solução **pgAdmin** (porta 8104) já vem apontada para esse banco e fica
atrás do portal, como as outras: só abre para quem está logado e tem acesso liberado (libere só
para quem administra os bancos). Antes de iniciar, defina em **Configurações** a senha do login
do pgAdmin; ao abrir o servidor, ele pede a senha do banco (`PORTAL_POSTGRES_SENHA` do `.env`,
ou a do usuário de uma solução, que só enxerga o próprio banco).

Remover uma solução do portal não apaga o banco dela. Para apagar (irreversível):
`sudo docker compose exec postgres psql -U postgres -c 'DROP DATABASE jornada_lideranca'`.

## Supabase do servidor (opcional)

Para sistemas feitos sobre o Supabase (ex.: Panorama), o portal pode subir um Supabase próprio na
VM, em `deploy/supabase/`: banco (PostgreSQL 17 do Supabase), login, API de dados, arquivos, o
painel Studio e o gateway. As versões são as do compose oficial (commit em
`deploy/supabase/ORIGEM-COMMIT`), sem realtime, edge functions, logs e pooler.

**Ligar:** no `.env`, `PORTAL_SUPABASE=sim`, e `bash deploy/atualizar.sh`. Na primeira vez
`deploy/supabase/subir.sh` gera no `.env` os segredos (senha do banco, `SUPABASE_JWT_SECRET`, as
chaves `SUPABASE_ANON_KEY` e `SUPABASE_SERVICE_ROLE_KEY` assinadas com ele, usuário e senha do
painel) e **nunca mais os troca** (trocar o segredo derruba os logins e invalida as chaves gravadas
nos sites). O endereço da API acompanha o `PORTAL_URL_BASE` (troca de IP). A primeira subida baixa
as imagens (alguns minutos) e a pilha usa de 1,5 a 3 GB de memória.

| Porta | O quê | Acesso |
| --- | --- | --- |
| **8111** (`PORTAL_SUPABASE_PORTA_API`) | API para os sistemas: `/rest/v1`, `/auth/v1`, `/storage/v1` | Sem login do portal: as chaves e as regras (RLS) do Supabase protegem os dados, como na nuvem. Os outros caminhos (o painel) respondem 404. |
| **8112** | **Supabase Studio** (solução do catálogo, "gerenciada pelo servidor") | Login do portal + liberação em **Acessos** + usuário do painel (`SUPABASE_PAINEL_USUARIO`, padrão `supabaselog`; senha em `SUPABASE_PAINEL_SENHA` no `.env`). Dá acesso total ao banco: só administradores. |

Os dados ficam nos volumes `log20-supabase-dados` e `log20-supabase-arquivos`; rodar de novo nunca
apaga nada. Os containers aparecem na tela **Uso**. Soluções com `[execucao] externo =
"container:porta"` (como o Studio) não são construídas nem iniciadas pelo portal: ele só publica a
porta, controla o acesso e mostra se está no ar; `requer = "supabase"` esconde a solução quando o
Supabase está desligado. Login sem e-mail: cadastro ligado com confirmação automática (o Panorama
cria usuários pela tela dele); `SUPABASE_SMTP_*` no `.env` liga o envio de e-mail.

**Ainda não incluído** (próximas etapas): o banco do Supabase no backup diário, preencher sozinho
`VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY` dos sistemas e copiar os dados do Supabase da nuvem.

## Variáveis na construção (sites Vite, React, Next)

Num site Vite/React/Vue servido como arquivos, as variáveis **não são lidas quando o site está no
ar**: o `npm run build` grava os valores dentro do site. Por isso o portal passa para o
`docker build` (como `--build-arg`, pelo ambiente do processo, sem aparecer no log):

- as configurações com `construcao = true` no manifesto (coluna **Construção** no assistente);
- as configurações e variáveis fixas com prefixo `VITE_`, `REACT_APP_`, `NEXT_PUBLIC_`, `VUE_APP_`,
  `NUXT_PUBLIC_`, `PUBLIC_` ou `EXPO_PUBLIC_` (sempre);
- num **site estático** (`[build] variaveis_na_construcao = true`, que o assistente marca para
  Vite/React/Vue), todas as variáveis fixas e todas as configurações.

O portal acrescenta `ARG <nome>` depois de cada `FROM` numa cópia do Dockerfile (o original não
muda), então funciona também com o Dockerfile do repositório. Mudou um valor desses? Clique em
**Atualizar** (reconstrói), não em Reiniciar; a tela avisa. O assistente acha essas variáveis em
`import.meta.env.*`, no `vite.config` (`loadEnv` → `env.X`, como o `GEMINI_API_KEY` do
`.env.local` dos projetos do Google AI Studio) e nos `.env*` do repositório. **Atenção:** o que
entra na construção fica visível para quem abre o site (F12); chave de API paga num site estático
fica exposta, e o assistente avisa.

## Uso e limites de memória/CPU

Tela **Uso** (administradores): memória, CPU e disco da VM; gráficos de memória e CPU de cada
sistema (1 hora a 7 dias, com a leitura de todos os sistemas ao passar o mouse ou com as setas do
teclado); uma tabela com o uso de agora, o pico das últimas 24 horas e o disco de cada sistema
(banco + arquivos + imagem); onde está o espaço em disco; e os reinícios e encerramentos por falta
de memória. O botão **Liberar** apaga o cache de construção e as imagens sem nome do Docker
(nunca volumes, bancos nem sistemas em uso).

**Limites:** cada sistema roda com um limite de memória e de CPU. Passou do limite de memória, só
ele é reiniciado (sem swap extra, para não deixar a VM lenta); passou do de CPU, ele só fica mais
lento. Os outros sistemas não sentem. O limite vem, nesta ordem, de:

1. **Uso → Limites** do sistema (vale na hora, sem reiniciar; mostra o pico de 24 h e sugere um valor);
2. `[recursos]` do manifesto (quem fez o sistema sabe quanto ele precisa);
3. o padrão do servidor: `PORTAL_LIMITE_MEMORIA` (padrão `1g`) e `PORTAL_LIMITE_CPUS` (padrão `1`).

```toml
[recursos]
memoria = "1.5g"   # 512m, 1g, 1.5g... 0 = sem limite
cpus = 2           # núcleos; 0.5 = meio núcleo
```

Sistemas que já estavam rodando antes desta versão ficam sem limite até serem reiniciados; a tela
Uso avisa e, em **Limites → Salvar**, o limite passa a valer na hora. A coleta roda dentro do
portal a cada 60 s (`PORTAL_USO_INTERVALO`) e guarda 7 dias (`PORTAL_USO_DIAS`) em `dados/uso.db`.

## Backup e restauração

Tela **Backups** (administradores). Por padrão, todo dia às 02:30 o portal copia:

| O quê | Como |
| --- | --- |
| Todos os bancos do PostgreSQL do servidor, inclusive os criados à mão no pgAdmin | `pg_dump -Fc`, um arquivo por banco, conferido logo após gravar |
| As contas do banco | `pg_dumpall --globals-only` |
| As pastas de dados das soluções (`[execucao.volumes]`) | `.tar.gz` de cada volume |
| Bancos externos, como o Supabase da Central | `pg_dump` da URL na configuração indicada em `[backup] banco_externo` do manifesto |
| Os dados do portal | `portal.db` (usuários, acessos, histórico) e `dados/*.json` (configurações e senhas das soluções), mais as soluções criadas pelo assistente |

O backup só lê: nada do que está em uso é parado ou alterado. Cada backup é uma pasta
`AAAA-MM-DD_HHMMSS_<motivo>` com um `manifesto.json` (arquivos, tamanhos, sha256, erros); a pasta
só ganha o nome final quando termina. Os arquivos ficam fechados (só root), porque têm senhas.

**Tudo é configurável** na aba **Ajustes** (os padrões vêm do `.env`, em `variaveis.exemplo`):
liga/desliga, um ou mais horários, quanto guardar (dias, semanas, meses e manuais; os fixados e o
último backup completo nunca são apagados), o que copiar, quais soluções, espaço mínimo em disco,
segunda cópia e um webhook (Teams/Power Automate) avisado quando um backup falha. No manifesto,
`[backup] incluir = false` tira uma solução do backup.

**Onde fica:** `backups/` na pasta do portal (fora do Git). `PORTAL_BACKUP_PASTA_HOST` no `.env`
muda para outro disco e `PORTAL_BACKUP_COPIA_HOST` grava uma segunda cópia (ex.: pasta da rede
montada na VM pelo `/etc/fstab`). **Sem cópia fora da VM, perder a VM é perder os backups.**

**Antes de cada atualização:** `deploy/atualizar.sh` faz um backup completo antes do `git pull`; se
ele falhar, a atualização não acontece (`--sem-backup` pula, não recomendado). A atualização nunca
apaga nem recria os bancos ou volumes: os dados do PostgreSQL ficam no volume
`log20-postgres-dados`. Nunca use `docker compose down -v` (o `-v` apaga os volumes).

**Restaurar uma solução:** na lista, **Restaurar…**, escolha a solução e o banco e/ou os arquivos,
e digite o identificador para confirmar. O portal faz antes um backup de segurança da solução,
para o container, restaura o banco **num banco temporário** e só troca pelo atual se tudo deu
certo (com erro, o atual fica intacto), restaura os arquivos e inicia de novo se estava no ar.
**Conferir** faz uma restauração de teste de cada banco num banco temporário e confere o sha256 de
cada arquivo, sem tocar no que está em uso; vale fazer de vez em quando.

**Importar um arquivo de fora** (aba **Importar arquivo**): por exemplo, o banco da máquina de
quem desenvolveu o sistema ou as planilhas que ele já tinha. Envie um `.sql` / `.sql.gz` (gerado
com `pg_dump -d <banco> --no-owner --no-privileges -f banco.sql`), um `.dump` (`pg_dump -Fc`) ou
um `.zip` / `.tar.gz` com arquivos. O portal confere o arquivo antes (recusa os que criam banco ou
usuários, os incompletos, `.zip` com caminhos para fora da pasta e `.dump` de uma versão mais nova
do PostgreSQL, que não abre no servidor; nesse caso, gere em SQL puro). Linhas de dono e
permissões da máquina de origem são ignoradas: tudo fica do usuário da solução. Depois você
escolhe a solução (e a pasta, para arquivos) e confirma; o caminho é o mesmo da restauração
(backup de segurança, banco temporário, troca só se der certo). O arquivo enviado é apagado
depois de usado, ou em 7 dias. Limite: `PORTAL_IMPORTACAO_MAX_GB` (padrão 10).

Pelo terminal, na pasta do portal:

```bash
sudo docker compose exec portal python -m marketplace backups                         # lista
sudo docker compose exec portal python -m marketplace backup                          # backup agora
sudo docker compose exec portal python -m marketplace verificar-backup <nome>         # restauração de teste
sudo docker compose exec portal python -m marketplace restaurar <nome> --solucao orcamento-obz
sudo docker compose exec portal python -m marketplace restaurar <nome> --portal       # usuários, acessos, senhas
```

**VM nova (recuperar tudo):** instale o portal com `deploy/preparar-vm.sh`, copie a pasta do
backup para `backups/` e rode
`sudo docker compose exec portal python -m marketplace restaurar <nome> --tudo`. Ele volta os dados
do portal, recria as contas do banco (com as mesmas senhas; o administrador `postgres` fica com a
senha do `.env` da VM nova), os bancos e os arquivos de cada solução. Depois:
`sudo docker compose restart portal` e `bash deploy/atualizar.sh`. Bancos externos (Supabase) não
são restaurados automaticamente: o `.dump` em `externos/` serve para `pg_restore` manual.

## Publicar uma nova solução pelo assistente

Em **Soluções → Nova solução**, informe o repositório (e o branch, e a subpasta se o sistema
estiver numa pasta dele). O portal baixa o código, **só lê os arquivos** (nada do repositório é
executado nessa etapa) e mostra:

- a **tecnologia** encontrada: Django, Flask, FastAPI, Streamlit, Node (Express e afins,
  Next.js, sites React/Vite/Vue), PHP, .NET, Java (Maven/Gradle), HTML estático, ou o
  Dockerfile do próprio projeto;
- os **pontos de atenção**: senhas e chaves no código, `.env` ou planilhas com CPF no
  repositório, HTTPS forçado, `ALLOWED_HOSTS` ou banco fixos no código, endereço `localhost`
  no frontend, entre outros;
- as **configurações que o sistema lê**: o portal procura no código as variáveis de ambiente
  (`os.environ`, `os.getenv`, `env()`/`config()` do django-environ e decouple,
  `process.env`, `getenv()` do PHP, `Environment.GetEnvironmentVariable`, `${X:padrao}` do
  Spring) e no `.env.example`. Cada uma vira um campo em **Gerenciar → Configurações**, com tipo
  (texto, segredo ou sim/não), valor padrão e **obrigatória** quando o código não tem padrão.
  Chaves como `SECRET_KEY`/`JWT_SECRET` viram segredos gerados pelo portal; variáveis de
  endereço (`BASE_URL`, `SITE_URL`…) recebem o endereço público. Dá para desmarcar ou adicionar
  linhas;
- um **Dockerfile sugerido** (quando o projeto não tem), a porta, a rota de saúde, os volumes e
  se dá para usar o banco do servidor. Tudo editável antes de criar.

Ao clicar em **Criar solução**, o manifesto e o Dockerfile ficam em
`dados/solucoes-locais/<id>/`, **só neste servidor** e fora do Git: o repositório de quem
desenvolveu não muda. A solução aparece com a etiqueta *Neste servidor*.

Na ficha da solução:

- **Editar** reabre o mesmo formulário (o identificador não muda). Mudanças nas configurações e
  portas valem no próximo **Reiniciar**; se o Dockerfile mudar, use **Atualizar**. Na edição,
  **Analisar de novo** lê o código mais recente do branch e mostra o que mudou: variáveis novas
  (para adicionar com um clique) e se o Dockerfile sugerido ficou diferente do guardado.
- **Baixar arquivos (.zip)** entrega a pasta `solucoes/<id>/` pronta: descompacte na raiz do
  repositório do portal, faça o commit, e depois **Remover do catálogo** a cópia local.

## Publicar uma nova solução (pelo Git)

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
| `[build] dockerfile_portal` | Dockerfile guardado na pasta do manifesto, usado no lugar do do repositório (o assistente usa) |
| `[build] imagem` | Nome da imagem Docker; soluções com o mesmo nome compartilham o build |
| `[execucao] porta_container`, `porta_host` | Porta da aplicação e porta publicada no servidor. No Git use **8101-8115**; de 8116 em diante ficam as criadas pelo assistente em cada servidor |
| `caminho_saude`, `caminho_abrir` | Rota para saber se está no ar e rota aberta pelo botão |
| `[execucao] caminhos_publicos` | Caminhos que abrem sem login no portal (links públicos da solução, ex.: `["/autoavaliacao/*", "/static/*"]`) |
| `[execucao.ambiente]` | Variáveis fixas; `{url}` vira o endereço público da solução e `{host}`, só o nome ou IP dele |
| `[execucao.volumes]` | Pastas do container que devem persistir |
| `segredos` | Variáveis geradas automaticamente uma única vez |
| `logo` | Imagem quadrada da solução (png, jpg, svg ou webp) dentro da pasta dela; sem logo, aparece um monograma |
| `[[configuracoes]]` | Campos editáveis pelo admin (`texto`, `segredo` ou `booleano`); `obrigatoria = true` impede iniciar sem preencher |
| `[banco]` + `[banco.configuracoes]` | Banco no PostgreSQL do servidor: qual configuração recebe cada dado (`{db_host}`, `{db_porta}`, `{db_nome}`, `{db_usuario}`, `{db_senha}` ou `{db_url}`) |
| `[[acessos_demo]]` | Usuários de teste exibidos ao admin |

## Comandos úteis

```powershell
python -m marketplace criar-admin                      # novo administrador
docker rm -f log20-central-colaborador                 # apagar o container de uma solução
docker volume rm log20-central-colaborador-dados       # apagar os dados dela (irreversível)
```
