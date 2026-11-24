"""Envio de e-mail pelo SMTP (alertas do portal) e teste de conexao (alertas e login do Supabase).

`smtp` e um dict com servidor, porta, usuario, senha, remetente e nome. Porta 465 usa SSL direto;
as outras usam STARTTLS quando o servidor oferece (servidores de teste, como o Mailpit, nao oferecem).
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid


class ErroCorreio(Exception):
    """Mensagem pronta para a tela."""


def _conectar(smtp: dict) -> smtplib.SMTP:
    porta = int(smtp.get("porta") or 587)
    servidor = smtp["servidor"]
    try:
        ctx = ssl.create_default_context()
        if porta == 465:
            con: smtplib.SMTP = smtplib.SMTP_SSL(servidor, porta, timeout=15, context=ctx)
        else:
            con = smtplib.SMTP(servidor, porta, timeout=15)
            con.ehlo()
            if con.has_extn("starttls"):
                con.starttls(context=ctx)
                con.ehlo()
        if smtp.get("usuario"):
            con.login(smtp["usuario"], smtp.get("senha") or "")
        return con
    except smtplib.SMTPAuthenticationError:
        raise ErroCorreio("O servidor de e-mail recusou o usuário ou a senha.") from None
    except (OSError, smtplib.SMTPException) as exc:
        raise ErroCorreio(f"Não foi possível conectar em {servidor}:{porta}: {exc}") from None


def testar(smtp: dict) -> str:
    """Conecta e confere usuario e senha, sem enviar nada. Devolve a mensagem de sucesso."""
    con = _conectar(smtp)
    with con:
        pass
    porta = int(smtp.get("porta") or 587)
    return f"Conexão com {smtp['servidor']}:{porta} OK" + (" e usuário/senha aceitos." if smtp.get("usuario") else " (sem usuário).")


def enviar(smtp: dict, para: list[str], assunto: str, texto: str) -> None:
    if not para:
        raise ErroCorreio("Nenhum destinatário informado.")
    if not smtp.get("remetente"):
        raise ErroCorreio("O e-mail do remetente não está configurado.")
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = formataddr((smtp.get("nome") or "Portal LOG20", smtp["remetente"]))
    msg["To"] = ", ".join(para)
    msg["Message-ID"] = make_msgid(domain=smtp["remetente"].rsplit("@", 1)[-1])
    msg.set_content(texto)
    con = _conectar(smtp)
    try:
        with con:
            con.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise ErroCorreio(f"O servidor de e-mail não aceitou a mensagem: {exc}") from None
