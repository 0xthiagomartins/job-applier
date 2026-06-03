"""Generate dynamic resume variants for a fixed set of mock job scenarios."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from job_applier.application.agent_execution import build_user_agent_settings
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import Platform
from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore
from job_applier.infrastructure.resume_dynamic import OhMyCvDynamicResumeBuilder
from job_applier.settings import RuntimeSettings


@dataclass(frozen=True, slots=True)
class MockJobScenario:
    slug: str
    title: str
    company_name: str
    location: str
    description_raw: str


def _mock_scenarios() -> tuple[MockJobScenario, ...]:
    return (
        MockJobScenario(
            slug="automation-langchain",
            title="Automation Engineer",
            company_name="SignalLayer AI",
            location="Remote",
            description_raw=(
                "We are hiring an Automation Engineer to build AI-assisted operational workflows "
                "with LangChain, RAG, chatbot orchestration, Python services, and system "
                "integrations. The role values practical AI delivery, automation reliability, "
                "and end-to-end ownership of business workflow tooling."
            ),
        ),
        MockJobScenario(
            slug="rpa-uipath",
            title="RPA Developer",
            company_name="FlowGrid",
            location="Remote - Brazil",
            description_raw=(
                "Looking for an RPA Developer to automate internal workflows with UiPath, process "
                "orchestration, integrations, and operational support. Experience in enterprise "
                "automation, resilient workflow execution, and reducing manual effort is highly "
                "valued."
            ),
        ),
        MockJobScenario(
            slug="backend-javascript",
            title="Backend Developer",
            company_name="OrbitOps",
            location="Remote - Brazil",
            description_raw=(
                "This Backend Developer will build Node.js and JavaScript services, APIs, "
                "microservices, observability, and internal integrations. Experience with "
                "automation-minded backend engineering and production support is highly valued."
            ),
        ),
        MockJobScenario(
            slug="full-stack-typescript",
            title="Full Stack Developer",
            company_name="Northstar Product Studio",
            location="Remote - Americas",
            description_raw=(
                "We are hiring a Full Stack Developer to build product features across TypeScript "
                "services, JavaScript frontends, REST APIs, system integrations, and operational "
                "automation. The role values ownership, observability, and reliable delivery."
            ),
        ),
        MockJobScenario(
            slug="backend-python-portuguese",
            title="Desenvolvedor(a) Backend Python",
            company_name="Atmo Sistemas",
            location="Remoto - Brasil",
            description_raw=(
                "Estamos contratando uma pessoa desenvolvedora backend com foco em Python, APIs, "
                "integrações entre sistemas, observabilidade e suporte a ambientes em produção. "
                "Valorizamos experiência com automação, serviços distribuídos, documentação clara "
                "e colaboração com times de produto."
            ),
        ),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate mock dynamic resumes for review.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        default=[],
        help="Optional mock scenario slug to generate. Repeatable.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Disable OpenAI usage and rely on deterministic adaptation only.",
    )
    parser.add_argument(
        "--output-dir",
        default="artifacts/mock-resume-review",
        help="Base output directory for generated artifacts.",
    )
    return parser.parse_args()


def _filter_scenarios(selected_slugs: list[str]) -> tuple[MockJobScenario, ...]:
    scenarios = _mock_scenarios()
    if not selected_slugs:
        return scenarios
    selected_tokens = {slug.strip().lower() for slug in selected_slugs if slug.strip()}
    return tuple(scenario for scenario in scenarios if scenario.slug in selected_tokens)


def main() -> int:
    args = _parse_args()
    scenarios = _filter_scenarios(args.scenarios)
    if not scenarios:
        print("No mock scenarios matched the requested filters.")
        return 1

    runtime_settings = RuntimeSettings().model_copy(
        update={
            "resume_dynamic_enabled": True,
        },
    )
    panel_store = LocalPanelSettingsStore(
        root_dir=runtime_settings.resolved_panel_storage_dir,
        runtime_settings=runtime_settings,
    )
    document = panel_store.load()
    settings = build_user_agent_settings(document)
    settings = settings.model_copy(
        update={
            "search": settings.search.model_copy(
                update={
                    "keywords": (
                        "Automation Engineer",
                        "RPA Developer",
                        "Backend Developer",
                        "Full Stack Developer",
                    ),
                },
            ),
        },
    )
    if args.offline:
        settings = settings.model_copy(
            update={
                "ai": settings.ai.model_copy(
                    update={"api_key": None},
                ),
            },
        )

    builder = OhMyCvDynamicResumeBuilder(runtime_settings)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    batch_dir = Path(args.output_dir) / timestamp
    batch_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, object]] = []
    for scenario in scenarios:
        posting = JobPosting(
            platform=Platform.LINKEDIN,
            url=f"https://mock.example/jobs/{scenario.slug}",
            title=scenario.title,
            company_name=scenario.company_name,
            description_raw=scenario.description_raw,
            location=scenario.location,
            easy_apply=True,
        )
        scenario_dir = batch_dir / scenario.slug
        scenario_dir.mkdir(parents=True, exist_ok=True)
        prepared = builder.build_for_job(
            settings=settings,
            posting=posting,
            run_dir=scenario_dir,
            submission_id=uuid4(),
        )
        if prepared is None:
            manifest.append(
                {
                    "scenario": scenario.slug,
                    "status": "skipped",
                    "reason": "no_cv_available",
                }
            )
            continue

        manifest.append(
            {
                "scenario": scenario.slug,
                "title": scenario.title,
                "company_name": scenario.company_name,
                "location": scenario.location,
                "used_dynamic_variant": prepared.used_dynamic_variant,
                "notes": prepared.notes,
                "target_language": prepared.target_language.value,
                "source_resume_language": prepared.source_resume_language.value,
                "source_cv_path": str(prepared.source_cv_path),
                "submission_cv_path": str(prepared.submission_cv_path),
                "markdown_path": str(prepared.markdown_path) if prepared.markdown_path else None,
                "css_path": str(prepared.css_path) if prepared.css_path else None,
            }
        )

    manifest_path = batch_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Generated {len(manifest)} mock resume scenarios in {batch_dir}")
    for item in manifest:
        print(
            f"- {item['scenario']}: dynamic={item.get('used_dynamic_variant')} "
            f"notes={item.get('notes')} cv={item.get('submission_cv_path')}",
        )
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
