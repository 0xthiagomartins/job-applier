import asyncio
from typing import cast

from playwright.async_api import Page, async_playwright
from pydantic import SecretStr

from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAgentAction,
    BrowserAgentElement,
    BrowserAgentSnapshot,
    BrowserAutomationError,
    BrowserDomSnapshotter,
    OpenAIResponsesBrowserAgent,
    estimate_openai_retry_delay_seconds,
    has_manual_intervention_cues,
    parse_browser_action,
    parse_browser_stall_diagnosis,
    parse_browser_task_assessment,
    serialize_snapshot,
    snapshot_signature,
    summarize_browser_action_error,
    summarize_openai_responses_error,
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


def test_parse_browser_action_accepts_press_payload() -> None:
    action = parse_browser_action(
        {
            "action_type": "press",
            "element_id": None,
            "value_source": None,
            "value": None,
            "action_intent": "confirm_autocomplete_choice",
            "key_name": "Enter",
            "wait_seconds": 0,
            "reasoning": "The current combobox likely needs keyboard confirmation.",
        }
    )

    assert action.action_type == "press"
    assert action.key_name == "Enter"
    assert action.action_intent == "confirm_autocomplete_choice"


def test_browser_agent_single_action_respects_attempt_limit() -> None:
    async def scenario() -> None:
        agent = OpenAIResponsesBrowserAgent(
            api_key=SecretStr("sk-test"),
            model="o3-mini",
            single_action_max_attempts=1,
        )
        snapshot = BrowserAgentSnapshot(
            url="https://www.linkedin.com/jobs/view/123",
            title="LinkedIn",
            visible_text="Easy Apply",
            elements=(),
        )
        plan_calls = 0
        execute_calls = 0

        async def fake_capture(*args, **kwargs):
            del args, kwargs
            return snapshot

        async def fake_plan_action(**kwargs):
            nonlocal plan_calls
            plan_calls += 1
            return BrowserAgentAction(
                action_type="click",
                element_id="agent-1",
                value_source=None,
                value=None,
                action_intent="open_easy_apply",
                key_name=None,
                scroll_target=None,
                scroll_direction=None,
                scroll_amount=550,
                wait_seconds=0,
                reasoning="Click the only visible apply button.",
            )

        async def fake_execute_action(**kwargs):
            nonlocal execute_calls
            execute_calls += 1
            del kwargs
            raise BrowserAutomationError("synthetic failure")

        agent._snapshotter.capture = fake_capture  # type: ignore[method-assign]
        agent._plan_action = fake_plan_action  # type: ignore[method-assign]
        agent._execute_action = fake_execute_action  # type: ignore[method-assign]

        try:
            await agent.perform_single_task_action(
                page=cast(Page, object()),
                available_values={},
                goal="Open Easy Apply",
                task_name="linkedin_open_easy_apply",
            )
        except BrowserAutomationError as exc:
            assert str(exc) == "synthetic failure"
        else:
            raise AssertionError("Expected BrowserAutomationError to be raised.")

        assert plan_calls == 1
        assert execute_calls == 1

    asyncio.run(scenario())


def test_browser_agent_single_action_aligns_priority_field_into_view_before_planning() -> None:
    async def scenario() -> None:
        agent = OpenAIResponsesBrowserAgent(
            api_key=SecretStr("sk-test"),
            model="o3-mini",
            single_action_max_attempts=1,
        )
        observed_snapshot: BrowserAgentSnapshot | None = None

        async def fake_plan_action(**kwargs):
            nonlocal observed_snapshot
            observed_snapshot = kwargs["snapshot"]
            return BrowserAgentAction(
                action_type="done",
                element_id=None,
                value_source=None,
                value=None,
                action_intent="field_visible_for_follow_up",
                key_name=None,
                scroll_target=None,
                scroll_direction=None,
                scroll_amount=250,
                wait_seconds=0,
                reasoning="The priority field is visible and ready for the next step.",
            )

        agent._plan_action = fake_plan_action  # type: ignore[method-assign]

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <style>
                      body { margin: 0; font-family: sans-serif; }
                      .dialog {
                        position: fixed;
                        inset: 48px auto auto 120px;
                        width: 520px;
                        max-height: 240px;
                        overflow-y: auto;
                        background: white;
                        border: 1px solid #ddd;
                        padding: 16px;
                      }
                      .spacer {
                        height: 520px;
                      }
                    </style>
                    <div class="dialog" role="dialog" aria-label="Apply to Example Corp">
                      <div class="spacer"></div>
                      <label for="city">Location (city)</label>
                      <input
                        id="city"
                        aria-label="Location (city)"
                        role="combobox"
                        aria-invalid="true"
                        value=""
                      />
                    </div>
                    """
                )

                action = await agent.perform_single_task_action(
                    page=page,
                    available_values={"intended_field_value": "Sao Paulo"},
                    goal="Finish the interaction for the invalid city field.",
                    task_name="linkedin_easy_apply_finalize_field_interaction",
                    focus_locator=page.locator('[role="dialog"]'),
                    priority_locator=page.locator("#city"),
                    allowed_action_types=("done",),
                )

                assert action.action_type == "done"
                assert observed_snapshot is not None
                city = next(
                    element
                    for element in observed_snapshot.elements
                    if element.label == "Location (city)"
                )
                assert city.is_priority_target is True
                assert city.invalid is True
                dialog_scroll_top = await page.locator(".dialog").evaluate("node => node.scrollTop")
                assert isinstance(dialog_scroll_top, (int, float))
                assert dialog_scroll_top > 0
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_manual_intervention_detection_flags_captcha_and_otp_pages() -> None:
    snapshot = BrowserAgentSnapshot(
        url="https://www.linkedin.com/checkpoint/challenge/",
        title="Security verification",
        visible_text="Complete the captcha and enter the code we sent to your email.",
        elements=(),
    )

    assert has_manual_intervention_cues(snapshot) is True


def test_snapshot_signature_changes_when_visible_surface_changes() -> None:
    base = BrowserAgentSnapshot(
        url="https://www.linkedin.com/jobs/search/",
        title="Jobs",
        visible_text="Apply to Example Corp",
        active_surface="Apply dialog",
        elements=(),
    )
    changed = BrowserAgentSnapshot(
        url="https://www.linkedin.com/jobs/search/",
        title="Jobs",
        visible_text="Review your application",
        active_surface="Apply dialog",
        elements=(),
    )

    assert snapshot_signature(base) != snapshot_signature(changed)


def test_serialize_snapshot_keeps_machine_readable_surface_metadata() -> None:
    snapshot = BrowserAgentSnapshot(
        url="https://www.linkedin.com/jobs/search/",
        title="Jobs",
        visible_text="Continue applying",
        active_surface="Apply dialog",
        active_surface_scrollable=True,
        active_surface_can_scroll_down=True,
        elements=(),
    )

    payload = serialize_snapshot(snapshot)

    assert payload["active_surface"] == "Apply dialog"
    assert payload["active_surface_scrollable"] is True
    assert payload["active_surface_can_scroll_down"] is True


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


def test_parse_browser_stall_diagnosis_accepts_recoverable_plan() -> None:
    diagnosis = parse_browser_stall_diagnosis(
        {
            "status": "recoverable",
            "summary": "The same autocomplete field stayed invalid across repeated snapshots.",
            "blocker_category": "autocomplete_confirmation",
            "next_plan": [
                "Focus the invalid combobox",
                "Use ArrowDown to inspect suggestions",
                "Confirm the closest valid option with Enter",
            ],
            "evidence": ["aria-invalid", "combobox", "Please enter a valid answer"],
        }
    )

    assert diagnosis.status == "recoverable"
    assert diagnosis.blocker_category == "autocomplete_confirmation"
    assert diagnosis.next_plan[0] == "Focus the invalid combobox"


def test_summarize_browser_action_error_normalizes_overlay_interception() -> None:
    message = summarize_browser_action_error(
        RuntimeError("Locator.click: dialog intercepts pointer events while clicking the target")
    )

    assert message == "The chosen target is blocked by an open dialog or overlay."


def test_summarize_browser_action_error_includes_blocker_summary_when_available() -> None:
    message = summarize_browser_action_error(
        RuntimeError("Locator.click: dialog intercepts pointer events while clicking the target"),
        blocker_summary="action 'Close', surface 'Link copied to clipboard.'",
    )

    assert "Observed blocker" in message
    assert "Close" in message


def test_summarize_openai_responses_error_marks_openai_rate_limit_clearly() -> None:
    message = summarize_openai_responses_error(
        status=429,
        body='{"error":"rate_limited"}',
        task_name="linkedin_job_search_state",
        mode="assessment",
    )

    assert "OpenAI Responses API rate limit" in message
    assert "not a LinkedIn page-rate-limit signal" in message


def test_estimate_openai_retry_delay_seconds_prefers_api_hint() -> None:
    delay = estimate_openai_retry_delay_seconds(
        status=429,
        body='{"error":{"message":"Rate limited. Please try again in 12.5s."}}',
        retry_after_header="3",
        max_delay_seconds=20.0,
    )

    assert 12.5 <= delay <= 13.75


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


def test_browser_dom_snapshotter_prioritizes_marked_blocking_surface() -> None:
    async def scenario() -> None:
        snapshotter = BrowserDomSnapshotter(max_elements=12, max_visible_text=600)
        agent = OpenAIResponsesBrowserAgent(
            api_key=SecretStr("sk-test"),
            model="o3-mini",
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <style>
                      body { margin: 0; font-family: sans-serif; }
                      .page {
                        padding: 120px;
                      }
                      #easy-apply {
                        width: 220px;
                        height: 48px;
                      }
                      .toast {
                        position: fixed;
                        top: 118px;
                        left: 118px;
                        width: 260px;
                        min-height: 90px;
                        padding: 12px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 80;
                      }
                    </style>
                    <div class="page">
                      <button id="easy-apply" aria-label="Easy Apply">Easy Apply</button>
                    </div>
                    <div class="toast" role="status" aria-label="Link copied to clipboard.">
                      <button id="toast-close" aria-label="Close">Close</button>
                      <p>Link copied to clipboard.</p>
                    </div>
                    """
                )

                summary = await agent._mark_intercepting_blocker(page, page.locator("#easy-apply"))
                snapshot = await snapshotter.capture(page)
                labels = [element.label for element in snapshot.elements]

                assert summary is not None
                assert "Close" in summary or "Link copied" in summary
                assert snapshot.active_surface == "Link copied to clipboard."
                assert labels[0] in {"Close", "Link copied to clipboard."}
                assert "Close" in labels
                assert all(label != "Easy Apply" for label in labels)
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_dom_snapshotter_focus_locator_includes_popup_options_for_active_field() -> None:
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
                      .dialog {
                        position: fixed;
                        inset: 80px auto auto 120px;
                        width: 520px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 20;
                        padding: 16px;
                      }
                      .portal {
                        position: fixed;
                        inset: 220px auto auto 160px;
                        width: 360px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 30;
                      }
                      .option { padding: 12px 16px; }
                    </style>
                    <div class="dialog" role="dialog" aria-label="Apply to Example Corp">
                      <label for="city">City</label>
                      <input
                        id="city"
                        aria-label="City"
                        role="combobox"
                        aria-autocomplete="list"
                        aria-controls="city-options"
                        aria-expanded="true"
                        aria-invalid="true"
                        aria-describedby="city-error"
                        value="Sao"
                      />
                      <div id="city-error" role="alert">Please enter a valid answer</div>
                    </div>
                    <div class="portal" id="city-options" role="listbox">
                      <div class="option" role="option">Sao Paulo, Sao Paulo, Brazil</div>
                      <div class="option" role="option">Sao Jose dos Campos, Sao Paulo, Brazil</div>
                    </div>
                    """
                )
                await page.locator("#city").focus()

                snapshot = await snapshotter.capture(
                    page,
                    focus_locator=page.locator('[role="dialog"]'),
                    priority_locator=page.locator("#city"),
                )
                labels = {element.label for element in snapshot.elements}
                texts = {element.text for element in snapshot.elements}
                city = next(element for element in snapshot.elements if element.label == "City")

                assert any("Sao Paulo" in (text or "") for text in texts)
                assert "City" in labels
                assert snapshot.elements[0].label == "City"
                assert city.focused is True
                assert city.invalid is True
                assert city.expanded is True
                assert city.validation_text == "Please enter a valid answer"
                assert city.is_priority_target is True
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_dom_snapshotter_focus_locator_includes_priority_field_when_scope_is_narrow() -> (
    None
):
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
                      .dialog {
                        position: fixed;
                        inset: 80px auto auto 120px;
                        width: 520px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 20;
                        padding: 16px;
                      }
                      .field-shell {
                        display: inline-block;
                      }
                    </style>
                    <div class="dialog" role="dialog" aria-label="Apply to Example Corp">
                      <label for="city">Location (city)</label>
                      <div class="field-shell">
                        <input
                          id="city"
                          aria-label="Location (city)"
                          role="combobox"
                          aria-invalid="true"
                          value="Sao Paulo"
                        />
                      </div>
                    </div>
                    """
                )
                await page.locator("#city").focus()

                snapshot = await snapshotter.capture(
                    page,
                    focus_locator=page.locator(".field-shell"),
                    priority_locator=page.locator("#city"),
                )

                city = next(
                    element for element in snapshot.elements if element.label == "Location (city)"
                )
                assert city.focused is True
                assert city.invalid is True
                assert city.is_priority_target is True
                assert "Location (city)" in snapshot.visible_text
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_dom_snapshotter_retries_with_page_scope_when_focus_snapshot_has_no_elements() -> (
    None
):
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
                      .dialog {
                        position: fixed;
                        inset: 80px auto auto 120px;
                        width: 520px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 20;
                        padding: 16px;
                      }
                    </style>
                    <div class="dialog" role="dialog" aria-label="Apply to Example Corp">
                      <div class="label-shell">Location (city)</div>
                      <input
                        id="city"
                        aria-label="Location (city)"
                        role="combobox"
                        aria-invalid="true"
                        value=""
                      />
                    </div>
                    """
                )
                await page.locator("#city").focus()

                snapshot = await snapshotter.capture(
                    page,
                    focus_locator=page.locator(".label-shell"),
                    priority_locator=page.locator("#city"),
                )

                city = next(
                    element for element in snapshot.elements if element.label == "Location (city)"
                )
                assert city.is_priority_target is True
                assert city.focused is True
                assert snapshot.visible_text
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_dom_snapshotter_page_scope_keeps_priority_context() -> None:
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
                      .dialog {
                        position: fixed;
                        inset: 80px auto auto 120px;
                        width: 520px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 20;
                        padding: 16px;
                      }
                      .portal {
                        position: fixed;
                        inset: 280px auto auto 160px;
                        width: 360px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 30;
                      }
                    </style>
                    <div class="dialog" role="dialog" aria-label="Apply to Example Corp">
                      <label for="city">Location (city)</label>
                      <input
                        id="city"
                        aria-label="Location (city)"
                        role="combobox"
                        aria-autocomplete="list"
                        aria-controls="city-options"
                        aria-expanded="true"
                        aria-invalid="true"
                        aria-describedby="city-error"
                        value="Sao"
                      />
                      <div id="city-error" role="alert">Please fill out this field.</div>
                    </div>
                    <div class="portal" id="city-options" role="listbox">
                      <div class="option" role="option">Sao Paulo, SP, Brazil</div>
                      <div class="option" role="option">Sao Jose dos Campos, SP, Brazil</div>
                    </div>
                    """
                )
                await page.locator("#city").focus()

                snapshot = await snapshotter.capture(
                    page,
                    priority_locator=page.locator("#city"),
                )

                city = next(
                    element for element in snapshot.elements if element.label == "Location (city)"
                )
                option_texts = {element.text for element in snapshot.elements if element.text}

                assert snapshot.active_surface == "Apply to Example Corp"
                assert city.focused is True
                assert city.invalid is True
                assert city.validation_text == "Please fill out this field."
                assert city.is_priority_target is True
                assert any("Sao Paulo, SP, Brazil" in text for text in option_texts)
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_dom_snapshotter_focus_locator_ignores_unrelated_describedby_noise() -> None:
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
                      .dialog {
                        position: fixed;
                        inset: 80px auto auto 120px;
                        width: 520px;
                        background: white;
                        border: 1px solid #ddd;
                        z-index: 20;
                        padding: 16px;
                      }
                      .sidebar {
                        position: fixed;
                        inset: 60px 40px auto auto;
                        width: 280px;
                      }
                    </style>
                    <div class="dialog" role="dialog" aria-label="Apply to Example Corp">
                      <label for="city">What is your current location?</label>
                      <input
                        id="city"
                        aria-label="What is your current location?"
                        aria-invalid="true"
                        aria-describedby="sidebar-copy"
                        aria-errormessage="city-error"
                        value=""
                      />
                      <div id="city-error" role="alert">Please fill out this field.</div>
                    </div>
                    <aside class="sidebar">
                      <p id="sidebar-copy">No response insights available yet</p>
                    </aside>
                    """
                )

                snapshot = await snapshotter.capture(
                    page,
                    focus_locator=page.locator('[role="dialog"]'),
                    priority_locator=page.locator("#city"),
                )
                city = next(element for element in snapshot.elements if element.label)

                assert city.validation_text == "Please fill out this field."
                assert city.invalid is True
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_agent_fill_reconciles_to_editable_descendant() -> None:
    async def scenario() -> None:
        agent = OpenAIResponsesBrowserAgent(
            api_key=SecretStr("sk-test"),
            model="o3-mini",
        )
        snapshot = BrowserAgentSnapshot(
            url="https://www.linkedin.com/jobs/view/123",
            title="LinkedIn",
            visible_text="What is your current location?",
            active_surface="What is your current location?",
            elements=(
                BrowserAgentElement(
                    element_id="agent-1",
                    tag="input",
                    label="What is your current location?",
                    input_type="text",
                    invalid=True,
                    is_priority_target=True,
                    candidate_label="What is your current location?",
                ),
            ),
        )
        action = BrowserAgentAction(
            action_type="fill",
            element_id="agent-1",
            value_source="intended_field_value",
            value=None,
            action_intent="fill_field_with_intended_value",
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=250,
            wait_seconds=0,
            reasoning="Fill the focused invalid location field.",
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <div
                      role="dialog"
                      aria-label="What is your current location?"
                      data-job-applier-active-surface="true"
                    >
                      <div data-job-applier-agent-id="agent-1">
                        <label for="city">What is your current location?</label>
                        <input id="city" type="text" />
                      </div>
                    </div>
                    """
                )

                await agent._execute_action(
                    page=page,
                    action=action,
                    values={"intended_field_value": "Sao Paulo"},
                    snapshot=snapshot,
                )

                assert await page.locator("#city").input_value() == "Sao Paulo"
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_agent_fill_selects_option_for_select_controls() -> None:
    async def scenario() -> None:
        agent = OpenAIResponsesBrowserAgent(
            api_key=SecretStr("sk-test"),
            model="o3-mini",
        )
        snapshot = BrowserAgentSnapshot(
            url="https://www.linkedin.com/jobs/view/123",
            title="LinkedIn",
            visible_text="Do you have experience with BI tools?",
            active_surface="Apply to Example Corp",
            elements=(
                BrowserAgentElement(
                    element_id="agent-3",
                    tag="select",
                    label="Do you have experience with BI tools?",
                    text="Select an option Yes No",
                    current_value="Select an option",
                    candidate_label="Do you have experience with BI tools? Select an option",
                ),
            ),
        )
        action = BrowserAgentAction(
            action_type="fill",
            element_id="agent-3",
            value_source="literal",
            value="Yes",
            action_intent="fill_select_field",
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=250,
            wait_seconds=0,
            reasoning="Select the valid affirmative option.",
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <div
                      role="dialog"
                      aria-label="Apply to Example Corp"
                      data-job-applier-active-surface="true"
                    >
                      <label for="bi-tools">Do you have experience with BI tools?</label>
                      <select id="bi-tools" data-job-applier-agent-id="agent-3">
                        <option value="">Select an option</option>
                        <option value="yes">Yes</option>
                        <option value="no">No</option>
                      </select>
                    </div>
                    """
                )

                await agent._execute_action(
                    page=page,
                    action=action,
                    values={},
                    snapshot=snapshot,
                )

                assert await page.locator("#bi-tools").input_value() == "yes"
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_agent_fill_types_sequentially_when_plain_fill_is_rejected() -> None:
    async def scenario() -> None:
        agent = OpenAIResponsesBrowserAgent(
            api_key=SecretStr("sk-test"),
            model="o3-mini",
        )
        snapshot = BrowserAgentSnapshot(
            url="https://www.linkedin.com/jobs/view/123",
            title="LinkedIn",
            visible_text="What is your current location?",
            active_surface="What is your current location?",
            elements=(
                BrowserAgentElement(
                    element_id="agent-1",
                    tag="input",
                    label="What is your current location?",
                    input_type="text",
                    current_value="",
                    invalid=True,
                    is_priority_target=True,
                    candidate_label="What is your current location?",
                ),
            ),
        )
        action = BrowserAgentAction(
            action_type="fill",
            element_id="agent-1",
            value_source="intended_field_value",
            value=None,
            action_intent="finalize_field_interaction",
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=250,
            wait_seconds=0,
            reasoning="Type the intended location into the focused field.",
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <div
                      role="dialog"
                      aria-label="What is your current location?"
                      data-job-applier-active-surface="true"
                    >
                      <label for="city">What is your current location?</label>
                      <input id="city" data-job-applier-agent-id="agent-1" type="text" />
                    </div>
                    <script>
                      const input = document.getElementById("city");
                      let sawKeydown = false;
                      input.addEventListener("keydown", () => {
                        sawKeydown = true;
                      });
                      input.addEventListener("input", () => {
                        if (!sawKeydown) {
                          input.value = "";
                        }
                      });
                      input.addEventListener("keyup", () => {
                        sawKeydown = false;
                      });
                    </script>
                    """
                )

                await agent._execute_action(
                    page=page,
                    action=action,
                    values={"intended_field_value": "Sao Paulo"},
                    snapshot=snapshot,
                )

                assert await page.locator("#city").input_value() == "Sao Paulo"
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_agent_fill_without_element_id_uses_priority_target() -> None:
    async def scenario() -> None:
        agent = OpenAIResponsesBrowserAgent(
            api_key=SecretStr("sk-test"),
            model="o3-mini",
        )
        snapshot = BrowserAgentSnapshot(
            url="https://www.linkedin.com/jobs/view/123",
            title="LinkedIn",
            visible_text="Location (city)",
            active_surface="Apply dialog",
            elements=(
                BrowserAgentElement(
                    element_id="agent-9",
                    tag="input",
                    label="Location (city)",
                    input_type="text",
                    current_value="Sao",
                    focused=True,
                    invalid=True,
                    is_priority_target=True,
                    candidate_label="Location (city) Sao",
                ),
            ),
        )
        action = BrowserAgentAction(
            action_type="fill",
            element_id=None,
            value_source="intended_field_value",
            value=None,
            action_intent="finalize_field_interaction",
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=250,
            wait_seconds=0,
            reasoning="Fill the currently focused priority field.",
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <div
                      role="dialog"
                      aria-label="Apply to Example Corp"
                      data-job-applier-active-surface="true"
                    >
                      <label for="city">Location (city)</label>
                      <input
                        id="city"
                        data-job-applier-agent-id="agent-9"
                        type="text"
                        value="Sao"
                      />
                    </div>
                    """
                )

                agent._validate_action_against_snapshot(  # noqa: SLF001
                    action,
                    snapshot=snapshot,
                    available_values={"intended_field_value": "Sao Paulo, SP, Brazil"},
                    allowed_action_types=("fill",),
                )
                await agent._execute_action(
                    page=page,
                    action=action,
                    values={"intended_field_value": "Sao Paulo, SP, Brazil"},
                    snapshot=snapshot,
                )

                assert await page.locator("#city").input_value() == "Sao Paulo, SP, Brazil"
            finally:
                await browser.close()

    asyncio.run(scenario())
