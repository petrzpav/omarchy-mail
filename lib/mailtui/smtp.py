"""Send mail. Gmail SMTP by default; an identity can name its own server."""

import smtplib
import ssl
from email.message import EmailMessage

from .config import Config, Identity


class SendError(Exception):
    pass


def send(cfg: Config, ident: Identity, msg: EmailMessage):
    if ident.smtp:
        host, _, port = ident.smtp.partition(":")
        user, password = ident.email, cfg.secrets.get(ident.secret_key, "")
        if not password:
            raise SendError(f"{ident.secret_key} missing in secrets")
    else:
        host, port, user, password = cfg.smtp_host, "465", cfg.email, cfg.password
    port = int(port or 465)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls(context=ctx)
        with server:
            server.login(user, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise SendError(str(e)) from e
