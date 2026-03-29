from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAgentSnapshot,
    has_manual_intervention_cues,
    parse_browser_action,
    parse_browser_task_assessment,
    summarize_browser_action_error,
)


def test_parse_browser_action_accepts_credential_fill_payload() -> None:
    action = parse_browser_action(
        {
            "action_type": "fill",
            "element_id": "agent-2",
            "value_source": "linkedin_email",
            "value": None,
            "action_intent": "fill_login_identifier",
            "wait_seconds": 0,
            "reasoning": "The visible email field should receive the LinkedIn login.",
        }
    )

    assert action.action_type == "fill"
    assert action.element_id == "agent-2"
    assert action.value_source == "linkedin_email"
    assert action.action_intent == "fill_login_identifier"
    assert action.reasoning


def test_parse_browser_action_accepts_task_specific_value_source() -> None:
    action = parse_browser_action(
        {
            "action_type": "fill",
            "element_id": "agent-3",
            "value_source": "search_keywords",
            "value": None,
            "action_intent": "fill_search_keywords",
            "wait_seconds": 0,
            "reasoning": "Fill the job search field with the configured search keywords.",
        }
    )

    assert action.action_type == "fill"
    assert action.value_source == "search_keywords"


def test_manual_intervention_detection_flags_captcha_and_otp_pages() -> None:
    snapshot = BrowserAgentSnapshot(
        url="https://www.linkedin.com/checkpoint/challenge/",
        title="Security verification",
        visible_text="Complete the captcha and enter the code we sent to your email.",
        elements=(),
    )

    assert has_manual_intervention_cues(snapshot) is True


def test_parse_browser_task_assessment_accepts_blocked_state() -> None:
    assessment = parse_browser_task_assessment(
        {
            "status": "blocked",
            "confidence": 0.94,
            "summary": "The form still shows a required field warning for phone number.",
            "evidence": ["required", "phone number", "warning"],
        }
    )

    assert assessment.status == "blocked"
    assert assessment.confidence == 0.94
    assert assessment.summary
    assert assessment.evidence == ("required", "phone number", "warning")


def test_summarize_browser_action_error_normalizes_overlay_interception() -> None:
    message = summarize_browser_action_error(
        RuntimeError("Locator.click: dialog intercepts pointer events while clicking the target")
    )

    assert message == "The chosen target is blocked by an open dialog or overlay."
