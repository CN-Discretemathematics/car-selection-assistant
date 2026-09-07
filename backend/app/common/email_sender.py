"""验证码邮件发送（SMTP）。

- SMTP_* 未配置时：开发模式控制台输出（dev_echo_codes=true 才含验证码明文）；
- 已配置时：经 SMTP 发送验证码邮件，失败时抛异常由调用方决定回退。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from app.common.config import get_settings

logger = logging.getLogger("car-selection.smtp")


class EmailSender:
    def __init__(self) -> None:
        settings = get_settings()
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._user = settings.smtp_user
        self._password = settings.smtp_password
        self._from = settings.smtp_from

    @property
    def available(self) -> bool:
        return bool(self._host and self._user and self._password and self._from)

    def send_code(self, to_email: str, code: str) -> None:
        """发送验证码邮件；未配置时按开发模式输出日志（不抛异常）。"""
        if not self.available:
            settings = get_settings()
            if settings.dev_echo_codes:
                logger.info("开发模式验证码：email=%s code=%s", to_email, code)
            else:
                logger.info("验证码已生成（SMTP 未配置，生产投递待接入）：email=%s", to_email)
            return

        subject = "选车助手登录验证码"
        body = f"你的登录验证码是：{code}，5 分钟内有效。若非本人操作请忽略本邮件。"
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = Header(subject, "utf-8")
        message["From"] = formataddr(("选车助手", self._from))
        message["To"] = to_email

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self._host, self._port, timeout=15, context=context) as server:
            server.login(self._user, self._password)
            server.sendmail(self._from, [to_email], message.as_string())
        logger.info("验证码邮件已发送：email=%s", to_email)
