from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import SecretStr

from job_applier.application.config import (
    AgentConfig,
    AIConfig,
    PrivateMetadataConfig,
    ScheduleConfig,
    SearchConfig,
    UserAgentSettings,
    UserProfileConfig,
)
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import ExecutionEventType, Platform
from job_applier.infrastructure.linkedin.easy_apply import (
    _maybe_send_job_application_email_from_executor,
)
from job_applier.infrastructure.linkedin.job_email import (
    SmtpJobApplicationEmailSender,
    detect_job_application_email_target,
)
from job_applier.settings import RuntimeSettings


def _settings(*, auto_send_job_email: bool = True) -> UserAgentSettings:
    return UserAgentSettings(
        profile=UserProfileConfig(
            name="Thiago Martins",
            email="thiago@example.com",
            phone="+5511999999999",
            city="Sao Paulo",
            work_authorized=True,
            availability="Immediate",
        ),
        private_metadata=PrivateMetadataConfig(),
        search=SearchConfig(keywords=("Desenvolvedor Backend",), location="Remote"),
        agent=AgentConfig(
            schedule=ScheduleConfig(),
            auto_send_job_email=auto_send_job_email,
        ),
        ai=AIConfig(api_key=None, model="o3-mini"),
    )


class JobApplicationEmailTargetTests(unittest.TestCase):
    def test_detects_application_email_with_positive_context(self) -> None:
        target = detect_job_application_email_target(
            "Para se candidatar, envie seu currículo para jobs@example.com "
            "com o assunto Backend Developer."
        )

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.recipient_email, "jobs@example.com")
        self.assertGreater(target.score, 0)

    def test_ignores_support_email_without_application_context(self) -> None:
        target = detect_job_application_email_target(
            "If you need accommodation, contact support@example.com. "
            "This posting was published by Example Corp."
        )

        self.assertIsNone(target)


class SmtpJobApplicationEmailSenderTests(unittest.TestCase):
    def test_returns_skipped_when_smtp_is_not_configured(self) -> None:
        sender = SmtpJobApplicationEmailSender(RuntimeSettings())
        posting = JobPosting(
            platform=Platform.LINKEDIN,
            url="https://www.linkedin.com/jobs/view/1",
            title="Backend Developer",
            company_name="Example",
            description_raw="Send your resume to jobs@example.com",
        )

        attempt = sender.send(
            posting=posting,
            settings=_settings(),
            recipient_email="jobs@example.com",
            resume_path=Path(__file__),
        )

        self.assertEqual(attempt.status, "skipped")
        self.assertEqual(attempt.notes, "smtp_not_configured")

    def test_sends_email_with_resume_attachment(self) -> None:
        runtime_settings = RuntimeSettings(
            feature_job_email_enabled=True,
            email_smtp_host="smtp.example.com",
            email_smtp_port=587,
            email_smtp_username="mailer@example.com",
            email_smtp_password=SecretStr("secret"),
            email_smtp_from_address="mailer@example.com",
            email_smtp_from_name="Job Applier",
        )
        sender = SmtpJobApplicationEmailSender(runtime_settings)
        posting = JobPosting(
            platform=Platform.LINKEDIN,
            url="https://www.linkedin.com/jobs/view/2",
            title="Desenvolvedor Backend",
            company_name="Empresa X",
            description_raw="Envie seu currículo para jobs@example.com",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            resume_path = Path(temp_dir) / "resume.pdf"
            resume_path.write_bytes(b"%PDF-1.4 example")
            smtp_client = Mock()
            smtp_client.send_message = Mock()
            smtp_client.login = Mock()
            smtp_client.starttls = Mock()
            smtp_client.ehlo = Mock()
            smtp_client.quit = Mock()

            with patch("smtplib.SMTP", return_value=smtp_client) as smtp_ctor:
                attempt = sender.send(
                    posting=posting,
                    settings=_settings(),
                    recipient_email="jobs@example.com",
                    resume_path=resume_path,
                )

        self.assertEqual(attempt.status, "sent")
        self.assertEqual(attempt.recipient_email, "jobs@example.com")
        smtp_ctor.assert_called_once_with("smtp.example.com", 587, timeout=15.0)
        smtp_client.login.assert_called_once_with("mailer@example.com", "secret")
        smtp_client.send_message.assert_called_once()
        message = smtp_client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "jobs@example.com")
        self.assertEqual(
            message["Subject"],
            "Application for Desenvolvedor Backend - Thiago Martins",
        )
        self.assertEqual(len(message.get_payload()), 2)


class JobApplicationEmailExecutorBridgeTests(unittest.TestCase):
    def test_records_skip_when_runtime_flag_is_disabled(self) -> None:
        posting = JobPosting(
            platform=Platform.LINKEDIN,
            url="https://www.linkedin.com/jobs/view/3",
            title="Backend Developer",
            company_name="Example",
            description_raw="Send your resume to jobs@example.com",
        )
        events: list[dict[str, object]] = []

        class _FakeExecutor:
            def __init__(self) -> None:
                self._runtime_settings = RuntimeSettings(feature_job_email_enabled=False)
                self._job_email_sender = Mock()

            def _record_event(self, _events: list[dict[str, object]], **payload: object) -> None:
                _events.append(payload)

            def _record_exception_event(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("exception recording should not be called")

        settings = _settings()
        with tempfile.TemporaryDirectory() as temp_dir:
            resume_path = Path(temp_dir) / "resume.pdf"
            resume_path.write_bytes(b"%PDF-1.4 example")
            fake_executor = _FakeExecutor()

            asyncio.run(
                _maybe_send_job_application_email_from_executor(
                    fake_executor,  # type: ignore[arg-type]
                    posting=posting,
                    settings=settings,
                    submission_cv_path=resume_path,
                    execution_id=uuid4(),
                    submission_id=uuid4(),
                    execution_events=events,  # type: ignore[arg-type]
                )
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], ExecutionEventType.JOB_EMAIL_ATTEMPTED)
        payload = events[0]["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["status"], "skipped")
        self.assertEqual(payload["reason"], "feature_flag_disabled")

    def test_records_successful_send_event(self) -> None:
        posting = JobPosting(
            platform=Platform.LINKEDIN,
            url="https://www.linkedin.com/jobs/view/4",
            title="Backend Developer",
            company_name="Example",
            description_raw="Send your resume to jobs@example.com",
        )
        events: list[dict[str, object]] = []

        class _FakeExecutor:
            def __init__(self) -> None:
                self._runtime_settings = RuntimeSettings(feature_job_email_enabled=True)
                self._job_email_sender = Mock()
                self._job_email_sender.send = Mock(
                    return_value=type(
                        "_Attempt",
                        (),
                        {
                            "status": "sent",
                            "recipient_email": "jobs@example.com",
                            "subject": "Application for Backend Developer - Thiago Martins",
                            "notes": None,
                        },
                    )()
                )

            def _record_event(self, _events: list[dict[str, object]], **payload: object) -> None:
                _events.append(payload)

            def _record_exception_event(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("exception recording should not be called")

        settings = _settings()
        with tempfile.TemporaryDirectory() as temp_dir:
            resume_path = Path(temp_dir) / "resume.pdf"
            resume_path.write_bytes(b"%PDF-1.4 example")
            fake_executor = _FakeExecutor()

            asyncio.run(
                _maybe_send_job_application_email_from_executor(
                    fake_executor,  # type: ignore[arg-type]
                    posting=posting,
                    settings=settings,
                    submission_cv_path=resume_path,
                    execution_id=uuid4(),
                    submission_id=uuid4(),
                    execution_events=events,  # type: ignore[arg-type]
                )
            )

        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["recipient_email"], "jobs@example.com")
        self.assertEqual(payload["resume_filename"], "resume.pdf")


if __name__ == "__main__":
    unittest.main()
