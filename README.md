# Portal de Soluções LOG20

Portal para disponibilizar soluções conteinerizadas, com login.

- **Usuários** entram com usuário e senha e veem só os sistemas liberados para eles.
- **Administradores** também gerenciam as soluções (instalar, iniciar, parar, atualizar,
  configurar, ver logs), os usuários e os acessos:
  - **Usuários**: criar um a um ou **importar em lote** (colar do Excel ou abrir um CSV),
    procurar, filtrar, editar, redefinir senha e, selecionando várias pessoas, liberar ou
    retirar sistemas e desativar/reativar de uma vez;
  - **Acessos**, em três abas (tudo vale na hora):
    - **Por sistema**: sistemas por área com quantas pessoas usam cada um; ao escolher um, quem tem
      acesso (com "Retirar" e "retirar de todos"), um campo para adicionar pessoas pelo nome ou
      usuário, **colar uma lista de usuários** (uma coluna do Excel serve) e **copiar o acesso de
      outro sistema**;
    - **Por pessoa**: os sistemas da pessoa como cartões por área, para marcar e desmarcar, e
      **copiar acessos de outra pessoa** (só acrescenta, ninguém perde acesso);
    - **Histórico** de quem mudou o quê (criações, acessos, perfis, senhas redefinidas).
    Sistemas com `somente_admin = true` no manifesto (pgAdmin e Supabase Studio, que dão acesso
    total aos bancos) abrem só para administradores: não podem ser liberados, o backend recusa, e
    liberações antigas são retiradas ao iniciar o portal (fica registrado no histórico).

Cada solução é descrita por um manifesto (`solucoes/<id>/solucao.toml`). A primeira
publicada é a **Central do Colaborador (Yuri)**.

## Rodar na sua máquina (PowerShell)

Requisitos: Python 3.12, Git e Docker Desktop aberto.

```powershell
git clone https://github.com/ProjetoLog20/portallog20.git portal-solucoes-log20
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

**Central do Colaborador no PostgreSQL do servidor:** no primeiro início o portal cria o banco e o
usuário dela e preenche `DATABASE_URL`; a cada início aplica as migrações novas do sistema
(`antes_de_iniciar` no manifesto). O banco começa sem usuários: crie o primeiro administrador
uma vez, na VM, na pasta do portal: `bash solucoes/central-colaborador/criar-admin.sh` (senha
temporária, trocada no primeiro acesso). Com outro banco em `DATABASE_URL` (ex.: Supabase), o
portal não mexe nele nem roda as migrações.

## Rodar no servidor ou numa VM (Docker Compose)

No servidor tudo roda em Docker: o **portal** (porta 8000), o **Caddy**, que é a única
entrada das soluções (uma porta por solução, na faixa 8101-8130), e um **PostgreSQL** para as
soluções que precisam de banco. As soluções e o banco ficam numa rede interna do Docker, sem
portas próprias, então só abrem para quem está logado no portal e tem acesso a elas.

```
navegador ──► :8000  portal ──────────────┐ constrói e inicia as soluções
          └─► :8101  Caddy ─(pergunta ao portal)─► log20-central-colaborador:4000
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
   git clone https://github.com/ProjetoLog20/portallog20.git portal-solucoes-log20
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

### HTTPS com certificado próprio

Um nome para o servidor, com portas: o portal fica em `https://<nome>` e cada sistema em
`https://<nome>:<porta>` (ex.: `https://portal.log20.com.br:8117`). Quem digitar `http://` é
levado para o `https://` (porta 80 e porta de cada sistema). O acesso passa a ser **pelo nome**:
pelo IP, o HTTPS não abre.

**Antes:** o nome no DNS interno apontando para o IP da VM; as portas 443, 80 e 8101-8130
liberadas na rede interna (nada para a internet; a 80 só serve para redirecionar); o
certificado vale para o nome (curinga `*.log20.com.br` também serve).

**Pelo script (recomendado)**, na pasta do portal na VM, com o arquivo que a TI mandou:

```bash
bash deploy/ligar-https.sh portal.log20.com.br certificado.pfx            # .pfx/.p12: pede a senha
bash deploy/ligar-https.sh portal.log20.com.br certificado.crt chave.key  # certificado + chave
bash deploy/ligar-https.sh --renovar certificado-novo.pfx                 # trocar (renovação)
bash deploy/ligar-https.sh --desligar                                     # voltar para http://IP
```

Antes de mexer em qualquer coisa, ele confere se o certificado vale para o nome, se a chave é
dele, a validade, se as portas 443 e 80 estão livres e se o nome já aponta para a VM no DNS; com
problema, para e explica. Converte `.pfx`, `.cer` binário e chave com senha; guarda o certificado
anterior (`dados/certificados/anterior-<data>/`) e uma cópia do `.env`; ajusta o `.env`; roda o
`deploy/atualizar.sh` (ao renovar, só recarrega o Caddy) e, no fim, confere se `https://<nome>`
responde com o certificado novo. `--sim` não pergunta; `--sem-atualizar` só instala.

**À mão (o que o script faz):**

**1. Os arquivos**, na VM, em `dados/certificados/` dentro da pasta do portal (fora do Git). A
pasta `dados/` é do root (criada pelo container): use `sudo` nos comandos dentro dela.

| Arquivo | O que é |
| --- | --- |
| `certificado.pem` | O certificado em PEM, com a cadeia intermediária depois dele, se houver |
| `chave.pem` | A chave privada em PEM, **sem senha** |

```bash
sudo mkdir -p dados/certificados && sudo chmod 700 dados/certificados
# Veio .pfx/.p12 (pede a senha do arquivo):
sudo openssl pkcs12 -in certificado.pfx -clcerts -nokeys -out dados/certificados/certificado.pem
sudo openssl pkcs12 -in certificado.pfx -nocerts -nodes -out dados/certificados/chave.pem
# Veio .cer binário (DER):  sudo openssl x509 -inform der -in certificado.cer -out dados/certificados/certificado.pem
# Chave com senha:          sudo openssl pkey -in chave-com-senha.pem -out dados/certificados/chave.pem
sudo chmod 600 dados/certificados/chave.pem
sudo openssl x509 -in dados/certificados/certificado.pem -noout -subject -enddate -ext subjectAltName
```

**2. O `.env`:**

```
PORTAL_URL_BASE=https://portal.log20.com.br
PORTAL_URL_PUBLICA=https://portal.log20.com.br
PORTAL_BIND_PORTAL=127.0.0.1
```

Com `https://` no `PORTAL_URL_BASE`, o cookie de sessão passa a ser só de conexão segura e o
portal confia no Caddy para saber o IP de quem acessa. `PORTAL_BIND_PORTAL=127.0.0.1` fecha a
porta 8000 para a rede: o portal só é acessado pelo Caddy (no próprio servidor,
`http://localhost:8000` continua). O `deploy/atualizar.sh` não troca mais o endereço quando o IP
muda (só faz isso quando o `.env` usa um IP).

**3. Aplicar:** `bash deploy/atualizar.sh`. Os sistemas com Supabase (ex.: Panorama) são
reconstruídos sozinhos com o endereço `https://` novo. Se outra coisa na VM já usa a porta 443 ou
80, troque no `.env` (`PORTAL_PORTA_HTTPS`, `PORTAL_PORTA_HTTP`).

**Conferência:** antes de ligar o HTTPS, o portal confere os arquivos, se a chave é do
certificado, se o nome está coberto e a validade. Com problema, o Caddy continua em HTTP (os
sistemas seguem acessíveis por `http://IP:porta`) e a tela **Alertas** mostra o motivo; o alerta
"Certificado HTTPS" também avisa 30 dias antes de vencer (ajustável), e o e-mail de situação
mostra a validade.

**Trocar o certificado (renovação):** substitua os dois arquivos e clique em **Recarregar
catálogo** (Soluções) ou rode `bash deploy/atualizar.sh`. Não precisa reiniciar nada.

**Portal na internet: administração só por dentro.** Por padrão, as telas de administração
(Soluções, Usuários, Acessos, Backups, Uso, Alertas, assistente) e os sistemas só de
administradores (pgAdmin, Studio) só abrem de **redes privadas** (a rede da empresa). Um
administrador de fora vê só "Meus sistemas", com um aviso e o endereço que o portal enxergou.
Para restringir a uma faixa: `PORTAL_REDES_ADMIN=172.16.0.0/16` no `.env`; `*` libera de qualquer
lugar (só para uma emergência). Confira depois de abrir o portal na internet: se o firewall trocar
o endereço de quem vem de fora por um interno (NAT de origem), a restrição não funciona, e o
aviso nunca aparece para quem está fora.

**Cabeçalhos de segurança:** o Caddy põe em todos os sistemas `X-Content-Type-Options`,
`Referrer-Policy` e `X-Frame-Options` (só se o sistema não mandar os seus) e, com HTTPS, `HSTS`
por um ano (só neste nome, sem subdomínios); tira `Server` e `X-Powered-By`.

**Voltar para HTTP:** `PORTAL_URL_BASE=http://IP`, `PORTAL_URL_PUBLICA=http://IP:8000`, tire
`PORTAL_BIND_PORTAL` e rode `bash deploy/atualizar.sh`.

### Com um subdomínio por sistema

Alternativa com um nome para cada sistema: veja `deploy/Caddyfile.exemplo` (modelo antigo, de um
Caddy fora do Docker) e a seção comentada do `variaveis.exemplo`. Nesse modo:

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

1. Instale o Caddy: `winget install -e --id CaddyServer.Caddy` (feche e reabra o terminal).
2. Em **Soluções → Gerenciar → Acesso → Endereço público**, informe `http://localhost:9101`
   na Central e clique em **Reiniciar**
   (o endereço entra na configuração da aplicação).
3. Com o portal rodando, em outro terminal na pasta `portal`:
   `caddy run --config deploy/Caddyfile.local`

Para testar: numa janela anônima, abra `http://localhost:9101` (vai para o login do portal);
entre com um usuário sem acesso à Central (vê "Acesso não liberado"); entre com um usuário
liberado (abre a Central).

No servidor, rode o Caddy na mesma máquina, defina `PORTAL_URL_PUBLICA=http://<ip>:8000` e
use `http://<ip>:9101` como endereço público. As portas internas (8101 em diante) ficam só em `127.0.0.1`
(`PORTAL_BIND_SOLUCOES=127.0.0.1`), então de fora só se chega pelo Caddy.

## Variáveis de ambiente

| Variável | Padrão | Para que serve |
| --- | --- | --- |
| `PORTAL_URL_BASE` | `http://localhost` | Endereço do servidor; cada solução fica em `<base>:<porta>` |
| `PORTAL_BIND_SOLUCOES` | `127.0.0.1` | `0.0.0.0` publica as soluções na rede |
| `PORTAL_HOST` / `PORTAL_PORTA` | `127.0.0.1` / `8000` | Onde o portal escuta |
| `PORTAL_SESSAO_HORAS` | `12` | Horas sem uso até o login vencer (cada uso renova; no máximo 30 dias desde o login) |
| `PORTAL_COOKIE_SEGURO` | `true` com `https://` no `PORTAL_URL_BASE` | Cookie de sessão só em conexão segura |
| `PORTAL_BIND_PORTAL` | `0.0.0.0` | `127.0.0.1` fecha a porta 8000 para a rede (com HTTPS, o portal fica só pelo Caddy) |
| `PORTAL_PORTA_HTTPS` / `PORTAL_PORTA_HTTP` | `443` / `80` | Portas do Caddy na VM para o portal em https e o redirecionamento |
| `PORTAL_URL_PUBLICA` | `http://localhost:8000` | Endereço do portal, para onde o proxy manda quem não está logado |
| `PORTAL_COOKIE_DOMINIO` | | Ex.: `.log20.com.br`, para a sessão valer nos subdomínios |
| `PORTAL_PROXIES` | `127.0.0.1` (redes privadas com HTTPS) | De onde o portal aceita o IP de quem acessa informado por um proxy (o Caddy). Com HTTPS, só redes privadas: um IP público não consegue informar um IP falso |
| `PORTAL_REDES_ADMIN` | redes privadas | De onde a administração do portal e os sistemas só de administradores (pgAdmin, Studio) abrem. Faixas separadas por vírgula (ex.: `172.16.0.0/16`); `*` = de qualquer lugar |
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

## Supabase das soluções (um por sistema)

Para sistemas feitos sobre o Supabase (ex.: Panorama), o portal cria **um Supabase só para cada
sistema**: banco (PostgreSQL 17 do Supabase), login, API de dados, arquivos e o gateway, em
containers `log20-sb-<id>-*` numa rede só dele. Banco, usuários e arquivos de um sistema não se
misturam com os de outro, e o código do sistema não muda. As versões das imagens e os arquivos de
`deploy/supabase/volumes` são os do compose oficial (commit em `deploy/supabase/ORIGEM-COMMIT`),
sem edge functions, logs e pooler. O **Realtime** (telas que atualizam sozinhas) é opcional, por
sistema.

**Ligar:** no manifesto, a seção `[supabase]` (o assistente "Nova solução" põe sozinho quando o
projeto usa `@supabase/supabase-js`):

```toml
[supabase]
url = "VITE_SUPABASE_URL"            # recebe o endereço do sistema
chave_anon = "VITE_SUPABASE_ANON_KEY" # recebe a chave pública
# chave_servico = "SUPABASE_SERVICE_ROLE_KEY"  # só para código que roda no servidor
# url_interna = "SUPABASE_URL"                 # idem: endereço na rede interna (sem login do portal)
# realtime = true   # servico de tempo real (supabase-js .channel()); cerca de 200 MB a mais
```

Com `realtime = true` (o assistente marca quando acha `.channel(` ou `postgres_changes` no código),
o portal cria também `log20-sb-<id>-realtime`, em `/realtime/v1` no mesmo endereço do sistema.
Tirar a linha e reiniciar remove o container. **Edge Functions** não existem no Supabase do
servidor (o assistente avisa).

No primeiro **Iniciar**, o portal gera os segredos (em `dados/supabase/<id>.json`, nunca mais
trocados), baixa as imagens (alguns minutos), sobe a pilha e preenche as variáveis; a pilha usa de
1 a 1,5 GB de memória. Os dados ficam nos volumes `log20-sb-<id>-dados|config|arquivos`:
reiniciar, atualizar, **Parar** ou **Remover** a solução nunca os apaga (Parar também para o
Supabase dela).

| Onde | O quê | Acesso |
| --- | --- | --- |
| **Mesmo endereço do sistema** (`/rest/v1`, `/auth/v1`, `/storage/v1`, `/realtime/v1`) | API que o site usa | Login do portal e liberação do sistema em **Acessos** (como o próprio sistema); depois, as chaves e as regras (RLS) do Supabase. Sem porta extra e sem CORS. |
| **8112** | **Supabase Studio** (painel; um só no servidor) | Login do portal, **só administradores** (`somente_admin`). Mostra um sistema de cada vez: em **Gerenciar**, o botão **Abrir o Studio** aponta o painel para o sistema. Dá acesso total ao banco. |

**Troca de IP:** o endereço vai gravado no site na construção. O portal guarda com que valores
construiu a imagem; no próximo Iniciar (o `atualizar.sh` reinicia tudo quando o IP muda), se o
endereço mudou, ele reconstrói o site e recria os containers do Supabase com o endereço novo.

**Login e e-mail** (Gerenciar → Supabase da solução → **Login e e-mail**): servidor de e-mail
(SMTP) próprio do sistema, com **Testar conexão** (confere servidor, usuário e senha sem enviar
nada), e **Cadastro aberto/fechado**. Em branco, vale o `SUPABASE_SMTP_*` do `.env` (igual para
todos); sem nenhum, as contas novas são confirmadas automaticamente e "esqueci a senha" não envia
nada. O cadastro fica **aberto** por padrão: o Panorama cria os usuários dele pelo próprio cadastro
(tela de Administração) e, fechado, essa tela para de funcionar; só quem entrou no portal e tem o
sistema liberado chega ao login dele. Fica em `dados/supabase/<id>.login.json` (só root); salvar
recria só o container do login. Linha de comando:
`python -m marketplace supabase listar|conferir|subir|parar [ids]`.

**Copiar dados de outro Supabase** (ex.: o da nuvem): em **Gerenciar → Visão geral**, botão
**Copiar dados de outro Supabase**. Informe o endereço do *Session pooler* da origem (servidor,
porta, usuário `postgres.<id do projeto>`) e a senha do banco, e marque o que copiar:

- **Tabelas e dados**: o esquema `public` inteiro (tabelas, dados, funções, gatilhos, regras RLS e
  permissões), mais os gatilhos do sistema nas tabelas do login;
- **Usuários do login** (`auth.users` e `auth.identities`, com as senhas), só com as colunas que
  existem nos dois lados (versões diferentes do login);
- **Buckets e regras dos arquivos**;
- **Arquivos**: baixados da origem pela API dela (precisa da chave de serviço da origem) e enviados
  para o Supabase local.

O que vem **substitui** o que estiver no local; antes, o portal faz um **backup isolado** do sistema
(banco, usuários, arquivos e chaves do Supabase; na tela Backups, "Segurança, antes de importar"),
mesmo que ele esteja fora do backup diário. **Desfazer a última importação** (mesma tela) volta o
Supabase a esse backup, com outro backup de segurança antes; a limpeza automática nunca apaga o
backup da última importação. (Até 16/11 a cópia de segurança era só o banco, em
`<pasta de backups>/supabase/<id>/antes-de-copiar-*.dump`; essas ficam lá, sem botão.) A senha e
a chave da origem só passam pela memória: não são gravadas nem aparecem no log. Tudo roda dentro do
container do banco local, que já tem o `pg_dump`/`psql` da mesma versão. Pode repetir quando quiser
(ex.: para trazer os dados mais novos antes de desligar a nuvem).

**Criar a estrutura pelos arquivos .sql** (mesma tela), para sistemas que nunca tiveram um
Supabase de origem: só com o banco vazio (nenhuma tabela em `public`). O portal lista os `.sql` de
`supabase/migrations`, `migrations`, `sql`, `database`, `db` e `supabase` do código da última
instalação, na ordem sugerida (`schema*` primeiro, depois pela numeração, `seed*` por último);
o administrador desmarca o que não deve rodar. Cada arquivo roda numa transação, como `postgres`;
no primeiro erro, para e mostra o arquivo (os anteriores ficam). Antes, o mesmo backup isolado da
importação (dá para desfazer).

**Backup:** o Supabase de cada sistema entra no backup diário (ver "Backup e restauração").

**Quem usou a primeira versão** (`PORTAL_SUPABASE=sim`, containers `log20-supabase-*`, API na
8111): ela não é mais usada nem atualizada. O `atualizar.sh` avisa e mostra como desligá-la; os
dados ficam nos volumes `log20-supabase-*` até alguém apagá-los.

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

## Alertas de saúde

Tela **Alertas** (administradores; o número ao lado do menu é o de alertas ativos). Um vigia
dentro do portal confere a cada minuto:

| Alerta | Quando abre |
| --- | --- |
| Sistema fora do ar | Um sistema que deveria estar no ar fica "com problema" (inclui o Supabase dele) por alguns minutos (padrão 3), ou não termina de iniciar em 10 minutos. Sistemas parados de propósito não contam. |
| Sistema reiniciando | 3 ou mais reinícios em 30 minutos, ou parada por falta de memória. |
| Memória perto do limite | Acima de 90% do limite do sistema (tela Uso) por 5 minutos. |
| Pouco espaço em disco | Menos de 10 GB ou 10% livres no disco do portal ou da pasta de backup. |
| Backup | O último falhou, não foi feito (ex.: falta de espaço) ou o último completo tem mais de 26 h. |
| Segunda cópia do backup | A cópia falhou, ou a pasta dela está no **mesmo disco da VM** (a pasta de rede não está montada e a "cópia" cairia na própria VM). |
| Certificado HTTPS | Com HTTPS: o certificado vence em menos de 30 dias, venceu, ou tem problema (arquivo faltando, chave errada, nome que não bate) e o portal ficou em HTTP. |

Cada alerta tem início e fim (histórico de 180 dias). O aviso por **e-mail** sai quando começa,
quando resolve (com a duração) e, enquanto continuar, a cada 12 h. Tudo isso, os limites e os
tipos ligados ficam em **Alertas → Ajustes**. O servidor de e-mail pode ser preenchido ali, ser o
do `.env` (`SUPABASE_SMTP_*`) ou o mesmo do login de um sistema; **Enviar e-mail de teste**
confere antes de salvar. **Enviar situação atual por e-mail** (no topo da tela) manda na hora,
para os destinatários salvos, os alertas ativos, o estado de cada sistema, o espaço em disco e o
último backup. A senha fica em `dados/alertas.json` (só o portal lê) e não volta para
a tela.

Para não alertar à toa, o vigia espera: nos 10 primeiros minutos depois que o portal sobe,
enquanto o `deploy/atualizar.sh` sobe os sistemas (marca `dados/sincronizando`) e durante backups e
restaurações (o Supabase para alguns segundos).

**Limite:** o portal não vigia a si mesmo. Se a VM ou o portal cair, ninguém é avisado. Para isso,
`GET /api/saude` responde `{"ok": true}` sem login e pode ser consultado por um monitor de fora
(ex.: o da TI).

**Testar sem e-mail de verdade:** suba um servidor de teste na VM
(`sudo docker run -d --name log20-mailpit -p 1025:1025 -p 8025:8025 -e MP_SMTP_AUTH_ACCEPT_ANY=1 -e MP_SMTP_AUTH_ALLOW_INSECURE=1 axllent/mailpit`),
use servidor `IP-DA-VM`, porta `1025`, e veja os e-mails em `http://IP-DA-VM:8025`. Apague depois
(`sudo docker rm -f log20-mailpit`).

## Backup e restauração

Tela **Backups** (administradores). Por padrão, todo dia às 02:30 o portal copia:

| O quê | Como |
| --- | --- |
| Todos os bancos do PostgreSQL do servidor, inclusive os criados à mão no pgAdmin | `pg_dump -Fc`, um arquivo por banco, conferido logo após gravar |
| As contas do banco | `pg_dumpall --globals-only` |
| As pastas de dados das soluções (`[execucao.volumes]`) | `.tar.gz` de cada volume |
| Bancos externos, como o Supabase da Central | `pg_dump` da URL na configuração indicada em `[backup] banco_externo` do manifesto |
| O Supabase de cada sistema (`[supabase]`) | chaves (`dados/supabase/<id>.json`), `pg_dump` do banco para conferência e `.tar.gz` dos volumes `log20-sb-<id>-dados|config|arquivos|snippets` |
| Os dados do portal | `portal.db` (usuários, acessos, histórico) e `dados/*.json` (configurações e senhas das soluções), as chaves do Supabase de cada sistema e as soluções criadas pelo assistente |

O backup só lê: nada do que está em uso é alterado. A única parada é a do **Supabase de cada
sistema**: para a cópia dos arquivos do banco ficar idêntica e consistente, a pilha dele para por
alguns segundos (o tempo de compactar os volumes) e volta sozinha; o registro mostra quanto tempo
ficou parada. A restauração do Supabase volta tudo junto (banco, usuários do login, arquivos e
chaves), porque partes de momentos diferentes não combinam; na tela, é a opção "Supabase do
sistema" de **Restaurar…**, e na linha de comando, `--sem-supabase` deixa de fora. Cada backup é uma pasta
`AAAA-MM-DD_HHMMSS_<motivo>` com um `manifesto.json` (arquivos, tamanhos, sha256, erros); a pasta
só ganha o nome final quando termina. Os arquivos ficam fechados (só root), porque têm senhas.

**Tudo é configurável** na aba **Ajustes** (os padrões vêm do `.env`, em `variaveis.exemplo`):
liga/desliga, um ou mais horários, quanto guardar (dias, semanas, meses e manuais; os fixados e o
último backup completo nunca são apagados), o que copiar, quais soluções, espaço mínimo em disco,
segunda cópia e um webhook (Teams/Power Automate) avisado quando um backup falha. No manifesto,
`[backup] incluir = false` tira uma solução do backup.

**Backup de uma solução só:** botão **Backup de uma solução** (ou
`python -m marketplace backup <id>`). Guarda só o que é dela (banco, pastas de dados, banco
externo e Supabase), sem o portal nem as outras soluções, e aparece na lista como "Só de uma
solução". A limpeza automática guarda os mais recentes de **cada** solução (o número de
manuais), sem que os backups de uma empurrem os das outras ou os completos para fora. Em
**Mostrar**, a lista filtra os backups com dados de uma solução (os completos e os só dela).

**Apagar de uma vez:** **Apagar todos** (com "Todas as soluções") apaga todos os backups da
pasta; com uma solução escolhida em **Mostrar**, só os backups feitos só dela (os completos
ficam). Os fixados sempre ficam, a segunda cópia não é tocada e é preciso digitar APAGAR.

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

## Automações do GitHub

Ficam em `.github/` e rodam sozinhas no GitHub (aba **Actions**):

- **Conferência** (`.github/workflows/conferencia.yml`): em cada pull request e em cada push na
  `main`, confere a sintaxe do Python e do JavaScript, os manifestos das soluções
  (`python -m marketplace validar`), os scripts shell (shellcheck) e, quando existir a pasta
  `tests/`, roda os testes. O resultado aparece no PR (✔ ou ✖) antes do merge.
- **Versão** (`.github/workflows/versao.yml`): cria a Release com as notas geradas a partir dos
  PRs. Pelo Git (`git tag v1.4.0 && git push origin v1.4.0`) ou pela tela (Actions → Versão →
  Run workflow → informar `v1.4.0`; a tag é criada na `main`).
- **Dependabot** (`.github/dependabot.yml`): uma vez por mês, abre PRs agrupados com as
  atualizações das bibliotecas Python, das imagens base dos Dockerfiles e do
  `docker-compose.yml` (sem trocar versão principal) e das ações do GitHub. As imagens do
  Supabase (`IMAGENS` em `marketplace/supabase.py`) ficam de fora e são atualizadas à mão.

Os alertas de falhas de segurança das dependências são ligados no repositório, em
Settings → Advanced Security → **Dependabot alerts**.

## Comandos úteis

```powershell
python -m marketplace criar-admin                      # novo administrador
docker rm -f log20-central-colaborador                 # apagar o container de uma solução
docker volume rm log20-central-colaborador-dados       # apagar os dados dela (irreversível)
```
