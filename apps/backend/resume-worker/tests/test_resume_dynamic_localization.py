from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import SecretStr

from job_applier.application.config import (
    AgentConfig,
    AIConfig,
    RulesetConfig,
    ScheduleConfig,
    SearchConfig,
    UserAgentSettings,
    UserProfileConfig,
)
from job_applier.domain.enums import ResumeMode, SupportedLanguage
from job_applier.infrastructure.resume_dynamic import (
    OhMyCvDynamicResumeBuilder,
    ResumeAdaptationPlan,
    ResumeCertificationEntry,
    ResumeEducationEntry,
    ResumeExperienceEntry,
    ResumeSourceSnapshot,
)
from job_applier.settings import RuntimeSettings


def _build_settings(cv_path: Path, *, preferred_language: SupportedLanguage) -> UserAgentSettings:
    return UserAgentSettings(
        profile=UserProfileConfig(
            name="Thiago",
            email="thiago@example.com",
            phone="+5511999999999",
            city="Sao Paulo",
            work_authorized=True,
            needs_sponsorship=False,
            availability="Immediate",
            cv_path=str(cv_path),
            cv_filename=cv_path.name,
            resume_mode=ResumeMode.DYNAMIC,
            preferred_language=preferred_language,
        ),
        search=SearchConfig(
            keywords=("Software Engineer",),
            location="Remote",
        ),
        agent=AgentConfig(schedule=ScheduleConfig()),
        ai=AIConfig(api_key=SecretStr("test-key"), model="o3-mini"),
        ruleset=RulesetConfig(),
    )


class _BatchCountingResumeBuilder(OhMyCvDynamicResumeBuilder):
    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.batch_sizes: list[int] = []

    def _translate_resume_items_batch(
        self,
        *,
        settings: UserAgentSettings,
        translation_items: tuple[tuple[str, str], ...],
        source_language: SupportedLanguage,
        target_language: SupportedLanguage,
        strict_target_language: bool = False,
    ) -> dict[str, str] | None:
        self.batch_sizes.append(len(translation_items))
        return {
            ref: f"{target_language.value}:{index}"
            for index, (ref, _text) in enumerate(translation_items)
        }


class ResumeDynamicLocalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        temp_root = Path(self._temp_dir.name)
        self.resume_path = temp_root / "resume.txt"
        self.resume_path.write_text("resume base", encoding="utf-8")
        self.runtime_settings = RuntimeSettings(
            data_dir=temp_root / "runtime",
            output_dir=temp_root / "last-run",
            resume_dynamic_enabled=True,
        )

    def test_resume_item_needs_localization_skips_factual_fields(self) -> None:
        builder = OhMyCvDynamicResumeBuilder(self.runtime_settings)

        self.assertFalse(
            builder._resume_item_needs_localization(  # noqa: SLF001
                ref="experience_company_0",
                text="Excent Capital Ltd",
                target_language=SupportedLanguage.PORTUGUESE,
            )
        )
        self.assertFalse(
            builder._resume_item_needs_localization(  # noqa: SLF001
                ref="city",
                text="Sao Paulo",
                target_language=SupportedLanguage.PORTUGUESE,
            )
        )
        self.assertFalse(
            builder._resume_item_needs_localization(  # noqa: SLF001
                ref="certification_issuer_0",
                text="Amazon Web Services",
                target_language=SupportedLanguage.PORTUGUESE,
            )
        )
        self.assertTrue(
            builder._resume_item_needs_localization(  # noqa: SLF001
                ref="summary",
                text="Backend engineer focused on platform reliability and APIs.",
                target_language=SupportedLanguage.PORTUGUESE,
            )
        )
        self.assertTrue(
            builder._resume_item_needs_localization(  # noqa: SLF001
                ref="experience_date_0",
                text="2022 - Present",
                target_language=SupportedLanguage.PORTUGUESE,
            )
        )

    def test_translate_resume_items_uses_larger_batches(self) -> None:
        builder = _BatchCountingResumeBuilder(self.runtime_settings)
        settings = _build_settings(
            self.resume_path,
            preferred_language=SupportedLanguage.ENGLISH,
        )
        translation_items = tuple(
            (f"experience_bullet_0_{index}", f"Backend delivery item {index}")
            for index in range(15)
        )

        translated = builder._translate_resume_items(  # noqa: SLF001
            settings=settings,
            translation_items=translation_items,
            source_language=SupportedLanguage.ENGLISH,
            target_language=SupportedLanguage.PORTUGUESE,
        )

        self.assertIsNotNone(translated)
        self.assertEqual([15], builder.batch_sizes)

    def test_initial_translation_filter_excludes_factual_fields_from_mixed_snapshot(
        self,
    ) -> None:
        builder = OhMyCvDynamicResumeBuilder(self.runtime_settings)
        snapshot = ResumeSourceSnapshot(
            header_role="Engenheiro de Software",
            summary="Engenheiro focado em backend e automação de processos.",
            experience_entries=(
                ResumeExperienceEntry(
                    title="Engenheiro de Software",
                    company_name="Empresa Exemplo",
                    date_range="2022 - Atual",
                    bullets=("Construiu APIs internas.",),
                ),
            ),
            certifications=(
                ResumeCertificationEntry(
                    name="AWS Certified Developer – Associate",
                    issuer="Amazon Web Services",
                ),
            ),
            education_entries=(
                ResumeEducationEntry(
                    institution="Universidade Exemplo",
                    degree="Bacharelado em Ciência da Computação",
                    location="Sao Paulo",
                ),
            ),
            skill_lines=("Competências: Python, FastAPI",),
            city="Sao Paulo",
        )
        plan = ResumeAdaptationPlan(
            headline="Engenheiro de Software",
            summary="Engenheiro focado em backend e automação de processos.",
        )

        translation_items = tuple(
            item
            for item in builder._build_resume_translation_items(  # noqa: SLF001
                resume_snapshot=snapshot,
                adaptation_plan=plan,
            )
            if builder._resume_item_needs_localization(  # noqa: SLF001
                ref=item[0],
                text=item[1],
                target_language=SupportedLanguage.PORTUGUESE,
            )
        )
        refs = {ref for ref, _text in translation_items}

        self.assertNotIn("city", refs)
        self.assertNotIn("experience_company_0", refs)
        self.assertNotIn("certification_issuer_0", refs)
        self.assertIn("experience_date_0", refs)
        self.assertIn("education_degree_0", refs)


if __name__ == "__main__":
    unittest.main()
