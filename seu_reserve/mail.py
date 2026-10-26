"""Optional SMTP notifications and aggregate progress, without credentials or lecture details."""

import math
import re
import smtplib
import socket
import ssl
from collections.abc import Mapping
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from .model import SHANGHAI


MESSAGES = {
    'test_notification': ('测试邮件', '邮件配置可用。请确认你能在收件箱中看到这封邮件。'),
    'reservation_succeeded': ('预约成功', '学校记录已确认一场预约成功。程序会继续检查剩余目标。'),
    'goal_completed': ('选听目标已完成', '已选听场次和必需类别均已达标，本次任务已结束。'),
    'authentication_required': ('需要人工认证', '登录尚未完成，业务尚未开始或已暂停。请在前台终端按提示处理认证；后台实例退出时会保留任务。'),
    'authentication_rejected': ('认证被拒绝', '账号、密码或账号状态被学校拒绝，程序已停止自动尝试。请检查配置及账号状态。'),
    'sms_send_failed': ('短信发送失败', '学校未确认短信已发送，请在前台终端检查并按提示处理。'),
    'reservation_unconfirmed': ('预约结果待确认', '提交结果尚未确认。程序只查询原活动，暂停其他提交；请勿删除状态文件或另开实例。'),
    'query_failure': ('持续查询失败', '连续五分钟未完成有效查询，程序仍在重试。请检查网络和本地运行状态；同一未恢复问题只通知一次。'),
    'query_recovered': ('查询已恢复', '此前连续失败的查询或处理已恢复，程序会继续核对目标和预约状态。'),
    'reservation_step_failure': ('预约关键步骤持续失败', '连续五分钟无法完成验证码或提交请求，列表查询可能仍正常。请检查网络、学校接口和本地运行状态；同一未恢复问题只通知一次。'),
    'reservation_step_recovered': ('预约关键步骤已恢复', '此前连续失败的验证码或提交请求已恢复，程序会继续尝试预约。'),
    'task_failed': ('任务异常退出', '程序已停止。请查看本地状态和日志，处理问题后用原命令重启。'),
    'unexpected_failure': ('程序异常退出', '程序遇到未预期错误并停止。请查看本地状态和日志。'),
    # Legacy subject kept so mail history saved by older versions still validates.
    'daily_report': ('每日进度汇报', '以下为程序最近取得的进度和运行情况，所有时间均为北京时间。'),
}


MAIL_FAILURES = {
    socket.gaierror: 'dns_failed',
    ssl.SSLError: 'tls_failed',
    TimeoutError: 'timeout',
    smtplib.SMTPAuthenticationError: 'authentication_failed',
    smtplib.SMTPSenderRefused: 'sender_refused',
    smtplib.SMTPRecipientsRefused: 'recipient_refused',
    smtplib.SMTPDataError: 'message_rejected',
    smtplib.SMTPConnectError: 'connection_failed',
    smtplib.SMTPServerDisconnected: 'connection_failed',
    smtplib.SMTPException: 'smtp_failed',
    ConnectionError: 'connection_failed',
    OSError: 'network_failed',
}
MAIL_ERROR_REASONS = {'none', 'unknown', *MAIL_FAILURES.values()}


def mail_history(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = snapshot.get('last_mail_at', 0)
    status = snapshot.get('last_mail_status', snapshot.get('mail_status', 'never'))
    if 'last_mail_status' not in snapshot and status in ('disabled', 'ready'):
        status = 'never'
    error = snapshot.get('last_mail_error', 'unknown' if status == 'failed' else 'none')
    event = snapshot.get('last_mail_event', 'unknown' if timestamp else 'none')
    duration = snapshot.get('last_mail_duration_seconds')
    if (type(timestamp) not in (int, float) or not 0 <= timestamp <= 253402214400
            or status not in ('never', 'sending', 'accepted', 'failed')
            or not isinstance(error, str) or error not in MAIL_ERROR_REASONS
            or not isinstance(event, str) or event not in (*MESSAGES, 'none', 'unknown')
            or duration is not None and (type(duration) not in (int, float)
                or duration < 0 or type(duration) is float and not math.isfinite(duration))):
        raise ValueError('invalid_mail_history')
    return dict(last_mail_at=timestamp, last_mail_status=status, last_mail_error=error,
                last_mail_event=event, last_mail_duration_seconds=duration)


def mail_failure_reason(exc: Exception) -> str:
    # Exception messages and SMTP responses can contain credentials or addresses.
    return next((reason for kind, reason in MAIL_FAILURES.items() if isinstance(exc, kind)), 'unknown')


class Mailer:
    @staticmethod
    def configured(environ: Mapping[str, str]) -> bool:
        return (any(environ.get(key) for key in
                    ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAIL_FROM', 'MAIL_TO'))
                or environ.get('SMTP_PORT', '') not in ('', '465'))

    def __init__(self, environ: Mapping[str, str]):
        self.username = environ.get('SMTP_USERNAME', '')
        self.password = environ.get('SMTP_PASSWORD', '')
        self.host = environ.get('SMTP_HOST') or {
            'qq.com': 'smtp.qq.com', '163.com': 'smtp.163.com',
            '126.com': 'smtp.126.com', 'gmail.com': 'smtp.gmail.com',
        }.get(self.username.rpartition('@')[2].lower(), '')
        self.port = int(environ.get('SMTP_PORT') or '465')
        self.sender = environ.get('MAIL_FROM') or self.username
        self.recipient = environ.get('MAIL_TO') or self.sender
        if (not self.host or not 1 <= self.port <= 65535
                or any(not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+', address)
                       for address in (self.sender, self.recipient))
                or any(c in self.host + self.sender + self.recipient for c in '\r\n')
                or bool(self.username) != bool(self.password)):
            raise ValueError('mail_configuration_invalid')

    def __call__(self, event: str, details: str = '') -> None:
        message = EmailMessage()
        message['From'] = self.sender
        message['To'] = self.recipient
        title, explanation = MESSAGES.get(event, ('运行通知', '请查看本地运行状态。'))
        message['Subject'] = '东南大学讲座预约：' + title
        message.set_content(explanation + '\n\n' + (details + '\n\n' if details else '')
                            + '时间：' + datetime.now(SHANGHAI).isoformat(timespec='seconds')
                            + '\n事件：' + event + '\n本地状态命令：uv run main.py --status\n')
        with smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=ssl.create_default_context()) as smtp:
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
