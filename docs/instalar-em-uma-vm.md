# Instalar o Portal de Soluções LOG20 em uma VM

Guia para quem vai subir o portal numa máquina nova. Leva uns 30 minutos, a maior parte
esperando instalações e o primeiro build.

## O que o dono dos repositórios faz antes (uma vez por VM)

Os repositórios são privados. Para cada VM, crie um **token só de leitura** e entregue à
pessoa por um canal seguro (não por e-mail aberto ou grupo):

1. github.com → foto → **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
2. **Token name:** o nome da VM (ex.: `vm-teste-maria`). **Expiration:** 30 ou 90 dias.
3. **Resource owner:** `ProjetoLog20`. **Repository access → Only select repositories:**
   `portallog20` e `Chatbot-rh-central-colabotador`.
   Atenção: um token fine-grained só enxerga os repositórios de **uma** conta. A Jornada da
   Liderança (`Jornada-da-lideranca`) e o Orçamento OBZ (`log20-orcamento_obz`) ainda estão
   na conta `JungGustavo`; enquanto não forem transferidos para a `ProjetoLog20`, o mesmo
   `PORTAL_GIT_TOKEN` não instala esses dois e o Yuri na mesma VM.
4. **Permissions → Repository permissions → Contents: Read-only.** Nada mais.
5. **Generate token** e copie o valor (começa com `github_pat_`; só aparece uma vez).

Para cortar o acesso daquela VM depois, basta apagar o token nessa mesma tela.

## O que a pessoa faz

### 1. Criar a VM

- Ubuntu Server 24.04 ou mais novo, 2 CPUs, 4 GB de RAM, 30 GB de disco, com internet.
- Na instalação do Ubuntu, marcar **Install OpenSSH server**.
- Rede: no Hyper-V, *Default Switch*; no VirtualBox, *Placa em modo Bridge*.
- Ao terminar, entrar na VM e anotar o IP: `ip -4 addr show eth0` (número depois de `inet`).

### 2. Entrar na VM por SSH

No PowerShell do Windows (assim dá para colar comandos e o token):

```powershell
ssh usuario-da-vm@IP-DA-VM
```

### 3. Baixar e instalar o portal

```bash
git clone https://github.com/ProjetoLog20/portallog20.git portal-solucoes-log20
cd portal-solucoes-log20
bash deploy/preparar-vm.sh
```

- No `git clone`: **Username** = o seu usuário do GitHub, **Password** = o token (colar; não aparece).
- O repositório se chama `portallog20` (conta `ProjetoLog20`); o comando acima baixa na pasta
  `portal-solucoes-log20`, o nome usado no resto deste guia e nos scripts.
- O script pede a senha da VM (sudo), instala o Docker, pede o **token de novo** (é o que o
  portal usa para baixar as soluções), sobe o portal e o Caddy.
- Se ao final ele não pedir os dados do administrador, crie à mão:

```bash
sudo docker compose exec portal python -m marketplace criar-admin
```

### 4. Usar

No navegador de qualquer computador que enxergue a VM:

1. `http://IP-DA-VM:8000` → entrar com o administrador.
2. **Soluções → Central do Colaborador → Instalar e iniciar** (primeira vez: alguns minutos).
3. **Usuários** → criar as pessoas e marcar os sistemas de cada uma.
4. Cada solução tem sua porta (a Central em `http://IP-DA-VM:8101`) e só abre para quem
   está logado no portal e tem acesso.

## Atualizar depois

Na pasta `portal-solucoes-log20`:

```bash
bash deploy/atualizar.sh
```

Atualiza o portal, corrige o IP no `.env` se a VM ganhou outro, e instala ou reinicia as
soluções (inclusive as que entraram no catálogo depois). Com `--atualizar-solucoes`, também
reconstrói as soluções com o último código. Uma solução nova precisa estar no token de leitura
da VM (peça ao dono dos repositórios).

Antes de mexer em qualquer coisa, o script faz um **backup completo** (bancos, arquivos das
soluções e dados do portal); se o backup falhar, nada é atualizado. Os bancos e volumes que já
existem nunca são apagados nem recriados pela atualização.

Para atualizar só **uma solução**: no portal, botão **Atualizar**.

## Backup

O portal faz backup sozinho todo dia (tela **Backups**, onde também se restaura). Os arquivos
ficam em `backups/` dentro da pasta do portal. Para guardar uma cópia fora da VM (recomendado),
monte uma pasta da rede na VM e coloque no `.env`:

```bash
PORTAL_BACKUP_COPIA_HOST=/mnt/rede/backups-portal
```

e rode `bash deploy/atualizar.sh`. Detalhes no README, em **Backup e restauração**.

## Problemas comuns

| Sintoma | O que fazer |
| --- | --- |
| `git clone` pede senha e recusa | Usar o **token** como senha, não a senha do GitHub. Conferir se ele não expirou. |
| "You do not have the initial commit yet" no `git pull` | O clone ficou incompleto: renomear a pasta e clonar de novo (copiar o `.env` e `dados/` da antiga). |
| Instalar falha com "Sem acesso ao repositório" | Token errado ou sem o repositório daquele sistema (ex.: `Chatbot-rh-central-colabotador`). Corrigir `PORTAL_GIT_TOKEN` no `.env` e rodar `sudo docker compose up -d`. |
| Página "Sistema fora do ar" | A solução não foi instalada/iniciada ainda: **Soluções → Instalar e iniciar**. |
| IP da VM mudou (Hyper-V *Default Switch* muda ao reiniciar o Windows) | Apagar o `.env` e rodar `bash deploy/preparar-vm.sh` de novo (pede o token outra vez). |
| Ver o que está acontecendo | `sudo docker compose ps` e `sudo docker compose logs --tail 50 portal` |
