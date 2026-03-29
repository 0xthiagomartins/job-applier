from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAgentSnapshot,
    BrowserAutomationError,
    has_manual_intervention_cues,
    parse_browser_action,
)


def test_parse_browser_action_accepts_credential_fill_payload() -> None:
    action = parse_browser_action(
        {
            "action_type": "fill",
            "element_id": "agent-2",
            "value_source": "linkedin_email",
            "value": None,
            "wait_seconds": 0,
            "reasoning": "The visible email field should receive the LinkedIn login.",
        }
    )

    assert action.action_type == "fill"
    assert action.element_id == "agent-2"
    assert action.value_source == "linkedin_email"
    assert action.reasoning


def test_parse_browser_action_rejects_unknown_value_source() -> None:
    try:
        parse_browser_action(
            {
                "action_type": "fill",
                "element_id": "agent-3",
                "value_source": "secret_text",
                "value": None,
                "wait_seconds": 0,
                "reasoning": "invalid",
            }
        )
    except BrowserAutomationError:
        return

    raise AssertionError("Expected BrowserAutomationError for invalid value_source")


def test_manual_intervention_detection_flags_captcha_and_otp_pages() -> None:
    snapshot = BrowserAgentSnapshot(
        url="https://www.linkedin.com/checkpoint/challenge/",
        title="Security verification",
        visible_text="Complete the captcha and enter the code we sent to your email.",
        elements=(),
    )

    assert has_manual_intervention_cues(snapshot) is True
