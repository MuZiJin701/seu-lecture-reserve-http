"""SMTP over TLS; event messages contain no school account or lecture data."""

import smtplib
import ssl
from collections.abc import Mapping
from datetime import datetime
from email.message import EmailMessage


class Mailer:
    def __init__(self, environ: Mapping[str, str]):
        self.host = environ.get('SMTP_HOST', '')
        self.port = int(environ.get('SMTP_PORT', '465'))
        self.sender = environ.get('MAIL_FROM', '')
        self.recipient = environ.get('MAIL_TO', '')
        self.username = environ.get('SMTP_USERNAME', '')
        self.password = environ.get('SMTP_PASSWORD', '')
        if (not self.host or not 1 <= self.port <= 65535
                or '@' not in self.sender or '@' not in self.recipient
                or any(c in self.host + self.sender + self.recipient for c in '\r\n')
                or bool(self.username) != bool(self.password)):
            raise ValueError('mail_configuration_invalid')

    def __call__(self, event: str) -> None:
        message = EmailMessage()
        message['From'] = self.sender
        message['To'] = self.recipient
        message['Subject'] = 'SEU reservation: ' + event
        message.set_content('Event: ' + event + '\nTime: ' + datetime.now().astimezone().isoformat()
                            + '\nCheck the server terminal for required actions.\n')
        with smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=ssl.create_default_context()) as smtp:
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
