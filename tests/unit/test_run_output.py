import asyncio
import json
from pathlib import Path

from pydantic import SecretStr

from job_applier.application.agent_execution import AgentExecutionOrchestrator
from job_applier.application.panel import (
    AIFormInput,
    PreferencesFormInput,
    ProfileFormInput,
    ScheduleFormInput,
)
from job_applier.domain import ExecutionOrigin, ScheduleFrequency
from job_applier.infrastructure import (
    InMemorySuccessfulSubmissionStore,
    LocalExecutionStore,
    LocalPanelSettingsStore,
)
from job_applier.observability import configure_logging
from job_applier.settings import RuntimeSettings


def test_run_output_keeps_only_the_latest_execution_bundle(tmp_path: Path) -> None:
    output_dir = tmp_path / "artifacts" / "last-run"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / ".gitkeep").write_text("", encoding="utf-8")
    (output_dir / "stale.log").write_text("old data", encoding="utf-8")

    configure_logging(RuntimeSettings(output_dir=output_dir, log_file_path=None))

    orchestrator = AgentExecutionOrchestrator(
        panel_store=build_ready_panel_store(tmp_path / "panel"),
        execution_store=LocalExecutionStore(root_dir=tmp_path / "executions"),
        successful_submission_store=InMemorySuccessfulSubmissionStore(),
        output_dir=output_dir,
    )

    summary = asyncio.run(orchestrator.run_execution(origin=ExecutionOrigin.MANUAL))

    assert not (output_dir / "stale.log").exists()
    assert (output_dir / ".gitkeep").exists()
    assert (output_dir / "summary.json").exists()
    assert (output_dir / "progress.json").exists()
    assert (output_dir / "settings-summary.json").exists()
    assert (output_dir / "run.log").exists()
    assert (output_dir / "timeline.jsonl").exists()
    assert not (output_dir / "run.json").exists()
    assert not (output_dir / "execution-events.jsonl").exists()

    summary_payload = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary_payload["execution_id"] == str(summary.execution_id)
    progress_payload = json.loads((output_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress_payload["execution_id"] == str(summary.execution_id)
    assert progress_payload["current_stage"] == "execution_completed"

    run_log = (output_dir / "run.log").read_text(encoding="utf-8")
    assert '"event_type": "execution_started"' in run_log
    assert '"event_type": "execution_completed"' in run_log
    timeline = (output_dir / "timeline.jsonl").read_text(encoding="utf-8")
    assert '"event_type": "execution_bundle_reset"' in timeline
    assert '"event_type": "config_loaded"' in timeline


def build_ready_panel_store(root_dir: Path) -> LocalPanelSettingsStore:
    store = LocalPanelSettingsStore(root_dir=root_dir)
    store.save_profile(
        ProfileFormInput.model_validate(
            {
                "name": "Thiago Martins",
                "email": "thiago@example.com",
                "phone": "+5511999999999",
                "city": "Sao Paulo",
                "linkedin_url": "https://www.linkedin.com/in/thiago",
                "github_url": "https://github.com/0xthiagomartins",
                "portfolio_url": "https://thiago.example.com",
                "years_experience_by_stack": {"python": 8},
                "work_authorized": True,
                "availability": "Immediate",
                "default_responses": {"work_authorization": "Yes"},
            },
        ),
    )
    store.save_preferences(
        PreferencesFormInput(
            keywords=("python", "automation"),
            location="Remote",
            posted_within_hours=24,
        ),
    )
    store.save_schedule(
        ScheduleFormInput(
            frequency=ScheduleFrequency.DAILY,
            run_at="23:00",
            timezone="UTC",
        ),
    )
    store.save_ai(AIFormInput(api_key=SecretStr("sk-test"), model="o3-mini"))
    return store
