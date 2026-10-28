# Instalar o Portal de Soluções LOG20 em uma VM

Guia para quem vai subir o portal numa máquina nova. Leva uns 30 minutos, a maior parte
esperando instalações e o primeiro build.

## O que o dono dos repositórios faz antes (uma vez por VM)

Os repositórios são privados. Para cada VM, crie um **token só de leitura** e entregue à
pessoa por um canal seguro (não por e-mail aberto ou grupo):

1. github.com → foto → **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
2. **Token name:** o nome da VM (ex.: `vm-teste-maria`). **Expiration:** 30 ou 90 dias.
3. **Repository access → Only select repositories:** `portal-solucoes-log20` e
   `yuri-central-colaborador`.
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
git clone https://github.com/JungGustavo/portal-solucoes-log20.git
cd portal-solucoes-log20
bash deploy/preparar-vm.sh
```

- No `git clone`: **Username** `JungGustavo`, **Password** = o token (colar; não aparece).
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
4. Cada solução tem sua porta (a Central em `http://IP-DA-VM:8101`, a Homologação em
   `:8102`) e só abre para quem está logado no portal e tem acesso.

## Atualizar depois

- **Portal:** na pasta `portal-solucoes-log20`, `git pull`, `sudo docker compose up -d --build` e
  `sudo docker compose restart portal` (o `restart` é o que carrega o código novo).
- **Uma solução:** no portal, botão **Atualizar** (busca o último commit e reconstrói).

## Problemas comuns

| Sintoma | O que fazer |
| --- | --- |
| `git clone` pede senha e recusa | Usar o **token** como senha, não a senha do GitHub. Conferir se ele não expirou. |
| "You do not have the initial commit yet" no `git pull` | O clone ficou incompleto: renomear a pasta e clonar de novo (copiar o `.env` e `dados/` da antiga). |
| Instalar falha com "Sem acesso ao repositório" | Token errado ou sem `yuri-central-colaborador`. Corrigir `PORTAL_GIT_TOKEN` no `.env` e rodar `sudo docker compose up -d`. |
| Página "Sistema fora do ar" | A solução não foi instalada/iniciada ainda: **Soluções → Instalar e iniciar**. |
| IP da VM mudou (Hyper-V *Default Switch* muda ao reiniciar o Windows) | Apagar o `.env` e rodar `bash deploy/preparar-vm.sh` de novo (pede o token outra vez). |
| Ver o que está acontecendo | `sudo docker compose ps` e `sudo docker compose logs --tail 50 portal` |
