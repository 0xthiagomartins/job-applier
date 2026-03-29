import asyncio

from playwright.async_api import async_playwright

from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAgentSnapshot,
    BrowserDomSnapshotter,
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


def test_parse_browser_action_accepts_surface_scroll_payload() -> None:
    action = parse_browser_action(
        {
            "action_type": "scroll",
            "element_id": None,
            "value_source": None,
            "value": None,
            "action_intent": "reveal_primary_cta",
            "scroll_target": "active_surface",
            "scroll_direction": "down",
            "scroll_amount": 640,
            "wait_seconds": 0,
            "reasoning": "The modal can scroll down and the primary next action is likely below.",
        }
    )

    assert action.action_type == "scroll"
    assert action.scroll_target == "active_surface"
    assert action.scroll_direction == "down"
    assert action.scroll_amount == 640


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


def test_browser_dom_snapshotter_focus_locator_prioritizes_visible_modal_controls() -> None:
    async def scenario() -> None:
        snapshotter = BrowserDomSnapshotter(max_elements=12, max_visible_text=600)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <style>
                      body { margin: 0; font-family: sans-serif; }
                      .background { height: 1600px; padding: 24px; }
                      .dialog {
                        position: fixed;
                        inset: 80px auto auto 120px;
                        width: 520px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 20;
                      }
                      .modal-body {
                        height: 220px;
                        overflow-y: auto;
                        padding: 16px;
                      }
                    </style>
                    <div class="background">
                      <button aria-label="Background action">Background action</button>
                    </div>
                    <div class="dialog" role="dialog" aria-label="Apply to Example Corp">
                      <div class="modal-body">
                        <label for="phone">Phone number</label>
                        <input id="phone" aria-label="Phone number" value="" />
                        <div style="height: 520px;"></div>
                        <button aria-label="Continue to next step">Next</button>
                      </div>
                    </div>
                    """
                )
                root = page.locator('[role="dialog"]')
                before = await snapshotter.capture(page, focus_locator=root)
                assert before.active_surface == "Apply to Example Corp"
                assert before.active_surface_scrollable is True
                assert before.active_surface_can_scroll_down is True
                assert all(element.label != "Background action" for element in before.elements)

                await page.locator(
                    '[data-job-applier-active-surface-scroll-target="true"]'
                ).evaluate(
                    "(node) => node.scrollTo({ top: node.scrollHeight, behavior: 'instant' })"
                )

                after = await snapshotter.capture(page, focus_locator=root)
                assert any(
                    element.label == "Continue to next step" or element.text == "Next"
                    for element in after.elements
                )
                assert "Continue to next step" in after.visible_text or "Next" in after.visible_text
            finally:
                await browser.close()

    asyncio.run(scenario())
