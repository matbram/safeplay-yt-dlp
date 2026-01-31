"""
Alerting module - Multi-channel notifications.

Supports email, logging, and website integration.
"""

import asyncio
import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
import aiohttp

from .config import settings, runtime_config


@dataclass
class Alert:
    """An alert to be sent."""
    severity: str  # 'info', 'warning', 'error', 'critical'
    alert_type: str
    title: str
    message: str
    details: dict = None
    job_id: Optional[str] = None
    youtube_id: Optional[str] = None
    action_id: Optional[str] = None

    def to_db_record(self) -> dict:
        return {
            "severity": self.severity,
            "alert_type": self.alert_type,
            "title": self.title,
            "message": self.message,
            "details": self.details or {},
            "job_id": self.job_id,
            "youtube_id": self.youtube_id,
            "action_id": self.action_id,
        }


class AlertManager:
    """Manages sending alerts through multiple channels."""

    SEVERITY_EMOJI = {
        "info": "ℹ️",
        "warning": "⚠️",
        "error": "❌",
        "critical": "🚨"
    }

    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self._email_lock = asyncio.Lock()

    async def send(self, alert: Alert) -> str:
        """
        Send an alert through all configured channels.

        Returns the alert ID from the database.
        """
        # Always log to database first
        alert_id = await self._log_to_database(alert)

        # Send through other channels concurrently
        tasks = []

        if runtime_config.email_alerts_enabled and self._email_configured():
            tasks.append(self._send_email(alert, alert_id))

        if runtime_config.website_alerts_enabled and self._website_configured():
            tasks.append(self._send_to_website(alert, alert_id))

        # Always log to file
        tasks.append(self._log_to_file(alert))

        # Run all notification tasks
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    print(f"Alert channel failed: {result}")

        return alert_id

    async def _log_to_database(self, alert: Alert) -> str:
        """Log alert to Supabase."""
        try:
            record = alert.to_db_record()
            response = self.supabase.table("agent_alerts").insert(record).execute()

            if response.data:
                return response.data[0]["id"]
            return ""
        except Exception as e:
            print(f"Warning: Could not log alert to database: {e}")
            return ""

    async def _update_alert(self, alert_id: str, updates: dict) -> None:
        """Update alert record in database."""
        try:
            self.supabase.table("agent_alerts") \
                .update(updates) \
                .eq("id", alert_id) \
                .execute()
        except Exception as e:
            print(f"Warning: Could not update alert in database: {e}")

    def _email_configured(self) -> bool:
        """Check if email is configured."""
        return all([
            settings.SMTP_HOST,
            settings.SMTP_USER,
            settings.SMTP_PASSWORD,
            settings.ALERT_EMAIL_FROM,
            settings.ALERT_EMAIL_TO,
        ])

    def _website_configured(self) -> bool:
        """Check if website integration is configured."""
        return all([
            settings.SAFEPLAY_WEBSITE_URL,
            settings.SAFEPLAY_WEBSITE_API_KEY,
        ])

    async def _send_email(self, alert: Alert, alert_id: str) -> None:
        """Send alert via email."""
        async with self._email_lock:
            try:
                # Build email
                msg = MIMEMultipart("alternative")
                msg["Subject"] = f"{self.SEVERITY_EMOJI.get(alert.severity, '')} SafePlay Agent: {alert.title}"
                msg["From"] = settings.ALERT_EMAIL_FROM
                msg["To"] = settings.ALERT_EMAIL_TO

                # Plain text version
                text_body = f"""
SafePlay AI Agent Alert
========================

Severity: {alert.severity.upper()}
Type: {alert.alert_type}
Time: {datetime.now(timezone.utc).isoformat()}

{alert.message}

Details:
{json.dumps(alert.details or {}, indent=2)}

---
Alert ID: {alert_id}
"""

                # HTML version
                html_body = f"""
<html>
<body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2 style="color: {'#dc3545' if alert.severity in ['error', 'critical'] else '#ffc107' if alert.severity == 'warning' else '#17a2b8'};">
        {self.SEVERITY_EMOJI.get(alert.severity, '')} {alert.title}
    </h2>

    <table style="margin: 20px 0;">
        <tr><td><strong>Severity:</strong></td><td>{alert.severity.upper()}</td></tr>
        <tr><td><strong>Type:</strong></td><td>{alert.alert_type}</td></tr>
        <tr><td><strong>Time:</strong></td><td>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</td></tr>
        {f'<tr><td><strong>Job ID:</strong></td><td>{alert.job_id}</td></tr>' if alert.job_id else ''}
        {f'<tr><td><strong>YouTube ID:</strong></td><td>{alert.youtube_id}</td></tr>' if alert.youtube_id else ''}
    </table>

    <p style="background: #f8f9fa; padding: 15px; border-radius: 5px;">
        {alert.message}
    </p>

    {f'<pre style="background: #e9ecef; padding: 15px; border-radius: 5px; overflow-x: auto;">{json.dumps(alert.details, indent=2)}</pre>' if alert.details else ''}

    <hr style="margin-top: 30px;">
    <p style="color: #6c757d; font-size: 12px;">
        Alert ID: {alert_id}<br>
        Sent by SafePlay AI Agent
    </p>
</body>
</html>
"""

                msg.attach(MIMEText(text_body, "plain"))
                msg.attach(MIMEText(html_body, "html"))

                # Send email
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._send_smtp, msg)

                # Update database
                await self._update_alert(alert_id, {
                    "email_sent": True,
                    "email_sent_at": datetime.now(timezone.utc).isoformat()
                })

            except Exception as e:
                print(f"Failed to send email alert: {e}")
                raise

    def _send_smtp(self, msg: MIMEMultipart) -> None:
        """Send email via SMTP (sync, run in executor)."""
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)

    async def _send_to_website(self, alert: Alert, alert_id: str) -> None:
        """Send alert to SafePlay website."""
        try:
            url = f"{settings.SAFEPLAY_WEBSITE_URL}/api/webhooks/ytdlp-alerts"
            headers = {
                "Content-Type": "application/json",
                "X-API-Key": settings.SAFEPLAY_WEBSITE_API_KEY,
            }

            payload = {
                "alert_id": alert_id,
                "severity": alert.severity,
                "alert_type": alert.alert_type,
                "title": alert.title,
                "message": alert.message,
                "details": alert.details or {},
                "job_id": alert.job_id,
                "youtube_id": alert.youtube_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        await self._update_alert(alert_id, {
                            "website_notified": True,
                            "website_notified_at": datetime.now(timezone.utc).isoformat()
                        })
                    else:
                        error_text = await response.text()
                        print(f"Website notification failed: {response.status} - {error_text}")

        except Exception as e:
            print(f"Failed to send website alert: {e}")
            raise

    async def _log_to_file(self, alert: Alert) -> None:
        """Log alert to file."""
        import os

        log_dir = settings.AGENT_LOG_DIR
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, "alerts.jsonl")
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **alert.to_db_record()
        }

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._append_to_file(log_file, json.dumps(record))
        )

    def _append_to_file(self, path: str, line: str) -> None:
        """Append a line to a file (sync, run in executor)."""
        with open(path, "a") as f:
            f.write(line + "\n")

    # Convenience methods for common alerts

    async def download_failure(
        self,
        job_id: str,
        youtube_id: str,
        error_code: str,
        error_message: str,
        attempts: int,
    ) -> str:
        """Send a download failure alert."""
        return await self.send(Alert(
            severity="error",
            alert_type="download_failure",
            title=f"Download failed: {youtube_id}",
            message=f"Download failed after {attempts} attempts. Error: {error_message}",
            details={
                "error_code": error_code,
                "error_message": error_message,
                "attempts": attempts,
            },
            job_id=job_id,
            youtube_id=youtube_id,
        ))

    async def fix_applied(
        self,
        fix_type: str,
        description: str,
        job_id: Optional[str] = None,
        git_commit: Optional[str] = None,
    ) -> str:
        """Send a fix applied alert."""
        return await self.send(Alert(
            severity="info",
            alert_type="fix_applied",
            title=f"Fix applied: {fix_type}",
            message=description,
            details={
                "fix_type": fix_type,
                "git_commit": git_commit,
            },
            job_id=job_id,
        ))

    async def fix_failed(
        self,
        fix_type: str,
        error: str,
        job_id: Optional[str] = None,
    ) -> str:
        """Send a fix failed alert."""
        return await self.send(Alert(
            severity="warning",
            alert_type="fix_failed",
            title=f"Fix failed: {fix_type}",
            message=f"Attempted fix did not resolve the issue: {error}",
            details={
                "fix_type": fix_type,
                "error": error,
            },
            job_id=job_id,
        ))

    async def escalation(
        self,
        reason: str,
        job_id: Optional[str] = None,
        youtube_id: Optional[str] = None,
        context: Optional[dict] = None,
    ) -> str:
        """Send an escalation alert."""
        return await self.send(Alert(
            severity="critical",
            alert_type="escalation",
            title="Human intervention required",
            message=reason,
            details=context or {},
            job_id=job_id,
            youtube_id=youtube_id,
        ))

    async def ytdlp_updated(
        self,
        old_version: str,
        new_version: str,
    ) -> str:
        """Send a yt-dlp update alert."""
        return await self.send(Alert(
            severity="info",
            alert_type="ytdlp_updated",
            title=f"yt-dlp updated to {new_version}",
            message=f"yt-dlp was updated from {old_version} to {new_version}",
            details={
                "old_version": old_version,
                "new_version": new_version,
            },
        ))

    async def health_degraded(
        self,
        success_rate: float,
        threshold: float,
        details: dict,
    ) -> str:
        """Send a health degraded alert."""
        return await self.send(Alert(
            severity="warning",
            alert_type="health_degraded",
            title=f"Download success rate dropped to {success_rate:.1f}%",
            message=f"Success rate is below threshold of {threshold:.1f}%",
            details=details,
        ))

    async def get_unacknowledged(self, limit: int = 50) -> list[dict]:
        """Get unacknowledged alerts."""
        try:
            response = self.supabase.table("agent_alerts") \
                .select("*") \
                .eq("acknowledged", False) \
                .order("created_at", desc=True) \
                .limit(limit) \
                .execute()
            return response.data or []
        except Exception:
            return []

    async def acknowledge(self, alert_id: str, acknowledged_by: str = "agent") -> None:
        """Acknowledge an alert."""
        await self._update_alert(alert_id, {
            "acknowledged": True,
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
            "acknowledged_by": acknowledged_by,
        })

    async def resolve(self, alert_id: str) -> None:
        """Mark an alert as resolved."""
        await self._update_alert(alert_id, {
            "resolved": True,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        })
