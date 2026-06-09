from __future__ import annotations

import unittest

from job_applier.tools.audit_dynamic_resume import _collect_findings


class AuditDynamicResumeTests(unittest.TestCase):
    def test_bilingual_anchor_overlap_does_not_trigger_unanchored_warning(self) -> None:
        base_cv_text = """
        Full Stack Software Engineer with experience in intelligent automation,
        system integrations, backend architecture, database modeling, internal tools,
        automated testing, observability, production support, process orchestration,
        applied AI, and RAG-powered chatbots.
        """
        generated_resume = """
        ## Resumo
        Engenheiro de Software Full Stack com experiência em automação inteligente,
        integrações de sistemas, arquitetura de backend, modelagem de banco de dados,
        ferramentas internas, testes automatizados, observabilidade, suporte à produção,
        orquestração de processos, IA aplicada e chatbots com RAG.
        """

        findings = _collect_findings(
            headline="Full Stack Software Engineer",
            summary=(
                "Engenheiro de Software Full Stack com experiência em automação inteligente e "
                "integrações de sistemas."
            ),
            markdown_body=generated_resume,
            pdf_text="",
            base_cv_text=base_cv_text,
            target_job_title=None,
        )

        self.assertNotIn(
            "high_unanchored_term_count",
            {finding.code for finding in findings},
        )

    def test_real_new_anchor_terms_still_trigger_unanchored_warning(self) -> None:
        base_cv_text = """
        Full Stack Software Engineer focused on Python, backend delivery, and automation.
        """
        generated_resume = """
        ## Resumo
        Engenheiro de Software com experiência em AWS, GCP, Azure, React, React Native,
        FastAPI, UiPath, LangChain, SQL, Linux e Java.
        """

        findings = _collect_findings(
            headline="Full Stack Software Engineer",
            summary=(
                "Engenheiro de Software com experiência em AWS, GCP, Azure, React, React Native, "
                "FastAPI, UiPath, LangChain, SQL, Linux e Java."
            ),
            markdown_body=generated_resume,
            pdf_text="",
            base_cv_text=base_cv_text,
            target_job_title=None,
        )

        self.assertIn(
            "high_unanchored_term_count",
            {finding.code for finding in findings},
        )


if __name__ == "__main__":
    unittest.main()
