"""ntfy notifications with email and console fallbacks."""
import os
import smtplib
from email.message import EmailMessage

import requests


class Notifier:
    def __init__(self, db):
        self.db = db
        self.topic = os.environ.get("NTFY_TOPIC", "").strip()
        self.base_url = os.environ.get("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")
        self.token = os.environ.get("NTFY_TOKEN", "").strip()

    def enqueue(self, text, request_id=None):
        self.db.x('INSERT INTO outbox(request_id,text) VALUES(?,?)', (request_id, text))
        from .telegram import Telegram
        telegram = Telegram(self.db)
        if telegram.configured:
            telegram.flush()
        else:
            print('[notify:pending] Telegram is not configured; request saved in the local inbox')

    def send(self, text: str, subject: str = "jobhunt") -> str:
        from .telegram import Telegram
        telegram = Telegram(self.db)
        if telegram.configured:
            self.enqueue(subject + '\n' + text)
            return 'telegram_queued'
        if self.topic:
            try:
                headers = {"Title": subject[:100], "Content-Type": "text/plain; charset=utf-8"}
                if self.token:
                    headers["Authorization"] = f"Bearer {self.token}"
                payload = text.encode("utf-8")[:4000].decode("utf-8", "ignore").encode("utf-8")
                r = requests.post(f"{self.base_url}/{self.topic}", data=payload,
                                  headers=headers, timeout=20)
                if r.ok:
                    return "ntfy"
            except requests.RequestException:
                pass
        if os.environ.get("SMTP_HOST") and os.environ.get("NOTIFY_EMAIL_TO"):
            try:
                m = EmailMessage()
                m["Subject"], m["From"], m["To"] = subject, os.environ.get("SMTP_USER", ""), os.environ["NOTIFY_EMAIL_TO"]
                m.set_content(text)
                with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
                    s.starttls()
                    s.login(os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", ""))
                    s.send_message(m)
                return "email"
            except Exception:
                pass
        print(f"[notify:console] {text}")
        return "console"
