"""Email detection and outbound delivery for jobs that request resume submission by email."""

from __future__ import annotations

import mimetypes
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from job_applier.application.config import UserAgentSettings
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import SupportedLanguage
from job_applier.infrastructure.language_support import detect_job_posting_language
from job_applier.infrastructure.linkedin.question_resolution import normalize_text
from job_applier.settings import RuntimeSettings

_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])",
    re.I,
)

_POSITIVE_EMAIL_CONTEXT_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"send\s+(your\s+)?(resume|cv|curriculum vitae)\s+to",
        r"email\s+(your\s+)?(resume|cv|application)\s+to",
        r"apply\s+(via\s+)?email",
        r"applications?\s+to",
        r"envie\s+(seu\s+)?(curriculo|currículo|cv)\s+para",
        r"enviar\s+(seu\s+)?(curriculo|currículo|cv)\s+para",
        r"candidaturas?\s+para",
        r"encaminhe\s+(seu\s+)?(curriculo|currículo|cv)\s+para",
    )
)

_NEGATIVE_EMAIL_CONTEXT_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"privacy",
        r"legal",
        r"support",
        r"help",
        r"questions?",
        r"accommodation",
        r"equal opportunity",
        r"unsubscribe",
        r"do not reply",
        r"nao responda",
        r"não responda",
        r"suporte",
        r"privacidade",
        r"juridic",
        r"jurídic",
    )
)


@dataclass(frozen=True, slots=True)
class JobApplicationEmailTarget:
    """One detected application email target inside the job description."""

    recipient_email: str
    score: int
    context_excerpt: str


@dataclass(frozen=True, slots=True)
class JobApplicationEmailAttempt:
    """Result of one outbound job-application email attempt."""

    status: str
    recipient_email: str | None = None
    subject: str | None = None
    notes: str | None = None


def detect_job_application_email_target(description_raw: str) -> JobApplicationEmailTarget | None:
    """Return the most likely application email target mentioned in the job description."""

    matches = list(_EMAIL_PATTERN.finditer(description_raw))
    if not matches:
        return None

    best_target: JobApplicationEmailTarget | None = None
    text_length = len(description_raw)
    for match in matches:
        candidate = match.group(1).strip()
        start = max(0, match.start() - 140)
        end = min(text_length, match.end() + 140)
        excerpt = " ".join(description_raw[start:end].split())
        normalized_excerpt = normalize_text(excerpt)
        score = 0

        if candidate.lower().startswith(("noreply@", "no-reply@", "donotreply@", "support@")):
            score -= 5

        for pattern in _POSITIVE_EMAIL_CONTEXT_PATTERNS:
            if pattern.search(normalized_excerpt):
                score += 3

        for pattern in _NEGATIVE_EMAIL_CONTEXT_PATTERNS:
            if pattern.search(normalized_excerpt):
                score -= 2

        if "mailto:" in normalized_excerpt:
            score += 1

        target = JobApplicationEmailTarget(
            recipient_email=candidate,
            score=score,
            context_excerpt=excerpt,
        )
        if best_target is None or target.score > best_target.score:
            best_target = target

    if best_target is None or best_target.score <= 0:
        return None
    return best_target


class SmtpJobApplicationEmailSender:
    """Send one job-application email through SMTP."""

    def __init__(self, runtime_settings: RuntimeSettings) -> None:
        self._runtime_settings = runtime_settings

    def is_configured(self) -> bool:
        """Return whether the current runtime has enough SMTP config to send mail."""

        return (
            bool(self._runtime_settings.email_smtp_host)
            and self._runtime_settings.email_smtp_from_address is not None
            and bool(self._runtime_settings.email_smtp_username)
            and self._runtime_settings.email_smtp_password is not None
        )

    def send(
        self,
        *,
        posting: JobPosting,
        settings: UserAgentSettings,
        recipient_email: str,
        resume_path: Path,
    ) -> JobApplicationEmailAttempt:
        """Build and deliver the email, attaching the requested resume PDF."""

        if not self.is_configured():
            return JobApplicationEmailAttempt(
                status="skipped",
                recipient_email=recipient_email,
                notes="smtp_not_configured",
            )
        if not resume_path.exists():
            return JobApplicationEmailAttempt(
                status="skipped",
                recipient_email=recipient_email,
                notes="resume_attachment_missing",
            )

        message = self._build_message(
            posting=posting,
            settings=settings,
            recipient_email=recipient_email,
            resume_path=resume_path,
        )
        self._deliver_message(message)
        return JobApplicationEmailAttempt(
            status="sent",
            recipient_email=recipient_email,
            subject=message["Subject"],
        )

    def _build_message(
        self,
        *,
        posting: JobPosting,
        settings: UserAgentSettings,
        recipient_email: str,
        resume_path: Path,
    ) -> EmailMessage:
        language = detect_job_posting_language(posting).language
        subject = f"Application for {posting.title} - {settings.profile.name}"
        body = _build_job_application_email_body(
            posting=posting,
            settings=settings,
            language=language,
        )

        message = EmailMessage()
        from_name = (self._runtime_settings.email_smtp_from_name or "").strip()
        from_address = str(self._runtime_settings.email_smtp_from_address)
        message["From"] = f"{from_name} <{from_address}>" if from_name else from_address
        message["To"] = recipient_email
        message["Subject"] = subject
        if self._runtime_settings.email_smtp_reply_to is not None:
            message["Reply-To"] = str(self._runtime_settings.email_smtp_reply_to)
        message.set_content(body)

        content_type, _ = mimetypes.guess_type(resume_path.name)
        maintype, subtype = (content_type or "application/pdf").split("/", maxsplit=1)
        message.add_attachment(
            resume_path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=resume_path.name,
        )
        return message

    def _deliver_message(self, message: EmailMessage) -> None:
        host = self._runtime_settings.email_smtp_host
        username = self._runtime_settings.email_smtp_username
        password_secret = self._runtime_settings.email_smtp_password
        if host is None or username is None or password_secret is None:
            msg = "SMTP settings are incomplete."
            raise RuntimeError(msg)

        timeout = max(5.0, self._runtime_settings.email_smtp_timeout_seconds)
        smtp: smtplib.SMTP | smtplib.SMTP_SSL
        if self._runtime_settings.email_smtp_use_ssl:
            smtp = smtplib.SMTP_SSL(host, self._runtime_settings.email_smtp_port, timeout=timeout)
        else:
            smtp = smtplib.SMTP(host, self._runtime_settings.email_smtp_port, timeout=timeout)
        try:
            smtp.ehlo()
            if (
                not self._runtime_settings.email_smtp_use_ssl
                and self._runtime_settings.email_smtp_starttls
            ):
                smtp.starttls()
                smtp.ehlo()
            smtp.login(username, password_secret.get_secret_value())
            smtp.send_message(message)
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                smtp.close()


def _build_job_application_email_body(
    *,
    posting: JobPosting,
    settings: UserAgentSettings,
    language: SupportedLanguage,
) -> str:
    name = settings.profile.name.strip()
    phone = settings.profile.phone.strip() if settings.profile.phone else ""
    linkedin_url = str(settings.profile.linkedin_url) if settings.profile.linkedin_url else ""

    if language is SupportedLanguage.PORTUGUESE:
        lines = [
            "Olá,",
            "",
            (
                f"Acabei de me candidatar à vaga {posting.title} na {posting.company_name} "
                "pelo LinkedIn e, conforme solicitado na descrição, estou enviando meu currículo "
                "em anexo."
            ),
            "",
            "Fico à disposição para conversar.",
            "",
            "Atenciosamente,",
            name,
            str(settings.profile.email),
        ]
    else:
        lines = [
            "Hello,",
            "",
            (
                f"I just applied for the {posting.title} role at {posting.company_name} on "
                "LinkedIn and, as requested in the job description, I am sharing my resume as "
                "an attachment."
            ),
            "",
            "I would be glad to continue the conversation.",
            "",
            "Best regards,",
            name,
            str(settings.profile.email),
        ]

    if phone:
        lines.append(phone)
    if linkedin_url:
        lines.append(linkedin_url)
    return "\n".join(lines)
