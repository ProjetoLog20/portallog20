"""HTTPS com certificado proprio (um nome para o servidor, com portas).

Ligado quando `PORTAL_URL_BASE` comeca com `https://` (ex.: https://portal.log20.com.br). O
certificado e a chave ficam em `dados/certificados/certificado.pem` e `chave.pem` (o Caddy le a
mesma pasta em /certificados). Com eles:

- o portal fica em https://<nome> (porta 443) e cada sistema em https://<nome>:<porta>;
- http://<nome> (porta 80) leva para https://<nome>; http://<nome>:<porta> leva para https.

Antes de ligar o HTTPS no Caddy, o portal confere o certificado: arquivos presentes, chave do
mesmo certificado, nome coberto e dentro da validade. Com problema, o Caddy continua em HTTP (os
sistemas seguem acessiveis pelo IP) e a tela Alertas avisa. Trocar o certificado: substituir os dois
arquivos e clicar em "Recarregar catalogo" (ou rodar deploy/atualizar.sh).
"""

from __future__ import annotations

import hashlib
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config

ARQ_CERTIFICADO = "certificado.pem"
ARQ_CHAVE = "chave.pem"
PASTA_NO_CADDY = "/certificados"


def pasta(cfg: Config) -> Path:
    return cfg.pasta_dados / "certificados"


def nome_https(cfg: Config) -> str:
    """O nome do servidor quando PORTAL_URL_BASE e https; vazio em HTTP."""
    partes = urlsplit(cfg.url_base)
    return (partes.hostname or "") if partes.scheme == "https" else ""


def _cobre(padrao: str, nome: str) -> bool:
    padrao, nome = padrao.lower().rstrip("."), nome.lower().rstrip(".")
    if padrao.startswith("*."):
        # Curinga cobre um nivel so: *.log20.com.br vale para a.log20.com.br, nao para log20.com.br.
        base = padrao[2:]
        return nome.endswith("." + base) and "." not in nome[: -len(base) - 1]
    return padrao == nome


def ler(cfg: Config) -> dict:
    """Situacao do certificado. `ok` = pode ligar o HTTPS. Sem HTTPS configurado: configurado=False."""
    nome = nome_https(cfg)
    info: dict = {"configurado": bool(nome), "nome": nome, "ok": False, "erro": "", "nomes": [],
                  "emissor": "", "valido_ate": None, "dias": None, "impressao": ""}
    if not nome:
        return info
    cert, chave = pasta(cfg) / ARQ_CERTIFICADO, pasta(cfg) / ARQ_CHAVE
    if not cert.is_file() or not chave.is_file():
        faltam = [p.name for p in (cert, chave) if not p.is_file()]
        info["erro"] = f"Falta(m) {', '.join(faltam)} em dados/certificados/ na pasta do portal."
        return info
    try:
        dados = ssl._ssl._test_decode_cert(str(cert))  # noqa: SLF001 - leitor de PEM da propria biblioteca
    except (ssl.SSLError, OSError, ValueError) as exc:
        info["erro"] = f"Não foi possível ler {ARQ_CERTIFICADO} (precisa ser PEM): {exc}"
        return info
    info["nomes"] = [v for t, v in dados.get("subjectAltName", ()) if t == "DNS"]
    if not info["nomes"]:
        info["nomes"] = [v for rdn in dados.get("subject", ()) for k, v in rdn if k == "commonName"]
    info["emissor"] = ", ".join(v for rdn in dados.get("issuer", ()) for k, v in rdn
                                if k in ("organizationName", "commonName"))
    try:
        ate = ssl.cert_time_to_seconds(dados["notAfter"])
        info["valido_ate"] = ate
        info["dias"] = int((ate - time.time()) // 86400)
    except (KeyError, ValueError):
        pass
    texto, texto_chave = cert.read_bytes(), chave.read_bytes()
    info["impressao"] = hashlib.sha256(texto + texto_chave).hexdigest()[:16]
    if b"ENCRYPTED" in texto_chave:
        info["erro"] = (f"{ARQ_CHAVE}: a chave está protegida por senha. Tire a senha com "
                        "'openssl pkey -in chave-com-senha.pem -out chave.pem' (pede a senha uma vez).")
        return info
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # A senha falsa evita que o OpenSSL pare esperando alguem digitar a senha de uma chave protegida.
        ctx.load_cert_chain(str(cert), str(chave), password=lambda: b"chave-sem-senha-esperada")
    except ssl.SSLError as exc:
        texto = str(exc).lower()
        if "mismatch" in texto:
            motivo = "a chave não é deste certificado"
        else:
            motivo = f"chave inválida ({exc.reason or exc})"
        info["erro"] = f"{ARQ_CHAVE}: {motivo}."
        return info
    except OSError as exc:
        info["erro"] = f"Não foi possível ler {ARQ_CHAVE}: {exc}"
        return info
    if not any(_cobre(p, nome) for p in info["nomes"]):
        info["erro"] = (f"O certificado não vale para {nome}; vale para: {', '.join(info['nomes']) or 'nenhum nome'}. "
                        "Ajuste PORTAL_URL_BASE ou use outro certificado.")
        return info
    if info["valido_ate"] is not None and info["valido_ate"] < time.time():
        info["erro"] = f"O certificado venceu em {_data(info['valido_ate'])}."
        return info
    info["ok"] = True
    return info


def para_caddy(cfg: Config) -> dict | None:
    """O que o gerador do Caddyfile precisa para ligar o HTTPS, ou None para continuar em HTTP."""
    info = ler(cfg)
    if not info["ok"]:
        if info["configurado"]:
            print(f"[https] HTTPS não ligado: {info['erro']}", flush=True)
        return None
    return {"nome": info["nome"], "certificado": f"{PASTA_NO_CADDY}/{ARQ_CERTIFICADO}",
            "chave": f"{PASTA_NO_CADDY}/{ARQ_CHAVE}", "impressao": info["impressao"]}


def resumo(cfg: Config) -> dict:
    """Para a tela e o e-mail de situacao (sem caminhos nem conteudo dos arquivos)."""
    info = ler(cfg)
    return {k: info[k] for k in ("configurado", "nome", "ok", "erro", "nomes", "emissor", "valido_ate", "dias")}


def _data(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%d/%m/%Y")
