from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_applier.application.agent_execution import build_user_agent_settings
from job_applier.application.panel import (
    PanelSettingsDocument,
    PrivateMetadataFormInput,
    StoredAISection,
    StoredPreferencesSection,
    StoredPrivateMetadataSection,
    StoredProfileSection,
    StoredScheduleSection,
    build_missing_private_metadata_feedback,
    parse_private_metadata_lines,
)
from job_applier.domain.enums import ResumeMode, SupportedLanguage
from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore


class PrivateMetadataPanelTests(unittest.TestCase):
    def test_parse_private_metadata_lines_normalizes_common_aliases(self) -> None:
        parsed = parse_private_metadata_lines(
            "\n".join(
                (
                    "CPF: 507.329.848-90",
                    "MÃE: Maria Example",
                    "Current Employer: Example Corp",
                )
            )
        )

        self.assertEqual(
            parsed,
            {
                "cpf": "507.329.848-90",
                "mother_name": "Maria Example",
                "current_employer": "Example Corp",
            },
        )

    def test_local_panel_store_persists_private_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalPanelSettingsStore(root_dir=Path(temp_dir))
            document = store.save_private_metadata(
                PrivateMetadataFormInput(
                    raw_text="CPF: 507.329.848-90\nRG: 12.345.678-9",
                    consent_to_ai_usage=True,
                )
            )

            loaded = store.load()

        self.assertEqual(document.private_metadata.raw_text, loaded.private_metadata.raw_text)
        self.assertTrue(loaded.private_metadata.consent_to_ai_usage)

    def test_build_user_agent_settings_wires_private_metadata_and_redacts_snapshot(self) -> None:
        document = PanelSettingsDocument(
            profile=StoredProfileSection(
                name="Thiago Martins",
                email="thiago@example.com",
                phone="+5511999999999",
                city="Sao Paulo",
                work_authorized=True,
                availability="Immediate",
                resume_mode=ResumeMode.STATIC,
                preferred_language=SupportedLanguage.PORTUGUESE,
            ),
            preferences=StoredPreferencesSection(
                keywords=("Desenvolvedor Backend",),
                location="Remote",
            ),
            ai=StoredAISection(model="o3-mini"),
            private_metadata=StoredPrivateMetadataSection(
                raw_text="CPF: 507.329.848-90\nMÃE: Maria Example",
                consent_to_ai_usage=True,
            ),
            schedule=StoredScheduleSection(),
        )

        settings = build_user_agent_settings(document)
        snapshot_payload = settings.to_snapshot_payload()

        self.assertEqual(settings.private_metadata.entries["cpf"], "507.329.848-90")
        self.assertTrue(settings.private_metadata.consent_to_ai_usage)
        self.assertEqual(
            snapshot_payload["private_metadata"],
            {
                "consent_to_ai_usage": True,
                "stored_keys": ["cpf", "mother_name"],
            },
        )
        self.assertNotIn("507.329.848-90", str(snapshot_payload))
        self.assertNotIn("Maria Example", str(snapshot_payload))

    def test_build_missing_private_metadata_feedback_aggregates_without_job_identity(self) -> None:
        feedback = build_missing_private_metadata_feedback(
            (
                (
                    "Required LinkedIn Easy Apply field could not be resolved safely because the "
                    "profile does not provide the factual data needed: normalized_key=cpf, "
                    "question_type=free_text_generic, control_kind=text."
                ),
                (
                    "Required LinkedIn Easy Apply field could not be resolved safely because the "
                    "profile does not provide the factual data needed: normalized_key=cpf, "
                    "question_type=free_text_generic, control_kind=text."
                ),
                (
                    "Required LinkedIn Easy Apply field could not be resolved safely because the "
                    "profile does not provide the factual data needed: "
                    "normalized_key=current_salary, question_type=free_text_generic, "
                    "control_kind=text."
                ),
                "some unrelated note",
            )
        )

        self.assertTrue(feedback["has_missing_fields"])
        self.assertEqual(feedback["skipped_submission_count"], 3)
        missing_fields = feedback["missing_fields"]
        assert isinstance(missing_fields, list)
        self.assertEqual(missing_fields[0]["key"], "cpf")
        self.assertEqual(missing_fields[0]["occurrences"], 2)
        message = feedback["message"]
        assert isinstance(message, str)
        self.assertIn("Nao consegui aplicar em 3 vaga(s)", message)


if __name__ == "__main__":
    unittest.main()
