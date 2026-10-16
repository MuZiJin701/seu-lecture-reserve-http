"""SMTP over TLS; event messages contain no school account or lecture data."""

import re
import smtplib
import ssl
from collections.abc import Mapping
from datetime import datetime
from email.message import EmailMessage


class Mailer:
    @staticmethod
    def configured(environ: Mapping[str, str]) -> bool:
        return (any(environ.get(key) for key in
                    ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAIL_FROM', 'MAIL_TO'))
                or environ.get('SMTP_PORT', '') not in ('', '465'))

    def __init__(self, environ: Mapping[str, str]):
        self.username = environ.get('SMTP_USERNAME', '')
        self.password = environ.get('SMTP_PASSWORD', '')
        self.host = environ.get('SMTP_HOST') or (
            'smtp.gmail.com' if self.username.rpartition('@')[2].lower() == 'gmail.com' else '')
        self.port = int(environ.get('SMTP_PORT') or '465')
        self.sender = environ.get('MAIL_FROM') or self.username
        self.recipient = environ.get('MAIL_TO') or self.sender
        if (not self.host or not 1 <= self.port <= 65535
                or any(not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+', address)
                       for address in (self.sender, self.recipient))
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
