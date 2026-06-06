#!/usr/local/bin/.venv/bin/python3

# This script is mapped to special keys for execution from anywhere
import asyncio
import os
import subprocess
import time

# Custom
import pyperclip
from playwright.async_api import async_playwright, Page

os.environ["NODE_NO_WARNINGS"] = "1"

DEBUG_PORT = "http://localhost:9222"

STABLE_SECONDS: float = 2.5 # How long the response text must be unchanged before is is done
POLL_INTERVAL: float = 0.8 # How often response element is polled
MAX_WAIT_SECONDS: int = 300 # Maximum time to wait for generation to complete (seconds)
MAX_RETRIES: int = 3 # How many times to retry the full send→receive flow on failure

# Each entry is tried in order
EDITOR_SELECTORS: dict[str, list[str]] = {
    "claude": [
        '[data-testid="chat-input"]',
        'div[contenteditable="true"][data-placeholder]',
        'div.ProseMirror[contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"]',
    ],
    "chatgpt": [
        "#prompt-textarea",
        'div[contenteditable="true"][id*="prompt"]',
        'textarea[placeholder*="message" i]',
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"]',
    ],
    "gemini": [
        '.ql-editor[contenteditable="true"]',
        'rich-textarea div[contenteditable="true"]',
        'div[contenteditable="true"][aria-label*="message" i]',
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"]',
    ],
}

RESPONSE_SELECTORS: dict[str, list[str]] = {
    "claude": [
        ".font-claude-response",
        '[data-is-streaming="false"] .prose',
        'div[data-testid*="response"]',
        ".prose",
    ],
    "chatgpt": [
        '[data-message-author-role="assistant"]',
        ".assistant-message",
        '[class*="assistant"]',
    ],
    "gemini": [
        "model-response",
        ".model-response-text",
        ".response-content",
        '[class*="model-response"]',
    ],
}

STOP_BUTTON_SELECTORS: dict[str, list[str]] = {
    "claude": [
        'button[aria-label="Stop response"]',
        'button[aria-label*="stop" i]',
        'button[aria-label*="cancel" i]',
    ],
    "chatgpt": [
        'button[aria-label="Stop generating"]',
        'button[aria-label*="stop" i]',
        '[data-testid="stop-button"]',
    ],
    "gemini": [
        'button[aria-label*="stop" i]',
        'button[aria-label*="cancel" i]',
    ],
}

SEND_BUTTON_SELECTORS: dict[str, list[str]] = {
    "claude": [
        'button[aria-label="Send message"]',
        'button[aria-label*="send" i]',
        '[data-testid="send-button"]',
    ],
    "chatgpt": [
        '[data-testid="send-button"]',
        'button[aria-label*="send" i]',
    ],
    "gemini": [
        'button[aria-label*="send" i]',
        'button.send-button',
        'mat-icon[aria-label*="send" i]',
    ],
}

async def _first_matching_selector(
    page: Page, selectors: list[str], timeout_ms: int = 3000, state: str = "visible"
) -> tuple[str, object] | tuple[None, None]:
    """
    Try each selector in order; return (selector_str, element) for the first hit.
    Returns (None, None) if none match within timeout_ms each.
    """
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout_ms, state=state)
            if el:
                return sel, el
        except Exception:
            continue
    return None, None


async def _detect_editor_heuristic(page: Page) -> str | None:
    """
    Score all contenteditable/textarea elements by position and size.
    Returns a unique CSS selector for the best candidate, or None.
    Last-resort fallback when all known selectors fail.
    """
    return await page.evaluate("""() => {
        const candidates = [
            ...document.querySelectorAll('div[contenteditable="true"], textarea')
        ];
        if (!candidates.length) return null;

        const scored = candidates.map(el => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return { el, score: -1 };
            let score = 0;
            if (r.bottom > window.innerHeight * 0.55) score += 4;
            if (r.width > window.innerWidth * 0.35) score += 3;
            if (r.height < 250) score += 2;
            if (el.getAttribute('aria-label') || el.getAttribute('placeholder')) score += 1;
            if (el.closest('form')) score += 1;
            return { el, score };
        });

        scored.sort((a, b) => b.score - a.score);
        const best = scored[0]?.el;
        if (!best || scored[0].score < 0) return null;

        // Build a unique selector
        if (best.id) return '#' + CSS.escape(best.id);
        // nth-of-type path
        const tag = best.tagName.toLowerCase();
        const allOfTag = [...document.querySelectorAll(tag)];
        const idx = allOfTag.indexOf(best) + 1;
        return `${tag}:nth-of-type(${idx})`;
    }""")


async def _find_editor(page: Page, model: str) -> tuple[str, object]:
    """
    Return (selector, element) for the chat input editor.
    Falls back to heuristic detection if known selectors fail.
    """
    sel, el = await _first_matching_selector(page, EDITOR_SELECTORS[model], timeout_ms=4000)
    if sel:
        return sel, el

    # Heuristic fallback
    heuristic_sel = await _detect_editor_heuristic(page)
    if heuristic_sel:
        try:
            el = await page.wait_for_selector(heuristic_sel, timeout=3000, state="visible")
            if el:
                print(f"[warn] Using heuristic editor selector: {heuristic_sel}")
                return heuristic_sel, el
        except Exception:
            pass

    raise RuntimeError(f"Could not locate chat editor for model '{model}'")


async def _inject_prompt(page: Page, selector: str, prompt: str) -> None:
    """
    Insert text into a contenteditable / textarea editor.
    Uses execCommand (works in ProseMirror / Quill / plain textarea),
    then verifies the content was set and falls back to DOM mutation if not.
    """
    await page.evaluate(
        """({ selector, prompt }) => {
            const el = document.querySelector(selector);
            if (!el) throw new Error('Editor not found: ' + selector);
            el.focus();

            const isInput = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT';
            if (isInput) {
                // Native input/textarea value setter (bypasses React's synthetic events)
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                )?.set;
                if (nativeInputValueSetter) {
                    nativeInputValueSetter.call(el, prompt);
                } else {
                    el.value = prompt;
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return;
            }

            // ContentEditable path
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
            const inserted = document.execCommand('insertText', false, prompt);

            // DOM mutation fallback
            if (!inserted || !el.textContent.trim()) {
                while (el.firstChild) el.removeChild(el.firstChild);
                const p = document.createElement('p');
                p.textContent = prompt;
                el.appendChild(p);
                el.dispatchEvent(new InputEvent('input', { bubbles: true, data: prompt }));
            }
        }""",
        {"selector": selector, "prompt": prompt},
    )


async def _submit(page: Page, model: str) -> None:
    """
    Click the send button if found, otherwise press Enter.
    """
    _, send_el = await _first_matching_selector(
        page, SEND_BUTTON_SELECTORS[model], timeout_ms=2000
    )
    if send_el:
        try:
            await send_el.click()
            return
        except Exception:
            pass
    await page.keyboard.press("Enter")


async def _get_latest_response(page: Page, model: str) -> str:
    """Return the inner text of the last response element, or ''."""
    for sel in RESPONSE_SELECTORS[model]:
        try:
            elements = await page.query_selector_all(sel)
            if elements:
                return await elements[-1].inner_text()
        except Exception:
            continue
    return ""


async def _wait_for_response_stable(page: Page, model: str) -> str:
    """
    Poll the response area until its text stops changing for STABLE_SECONDS.
    Also watches the stop button: if it disappears, generation has ended.

    This is far more robust than waiting on button disabled-states.
    """
    # Brief pause so the UI can transition into the "generating" state
    await page.wait_for_timeout(1200)

    last_text: str = ""
    stable_since: float | None = None
    deadline = time.monotonic() + MAX_WAIT_SECONDS

    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        current_text = await _get_latest_response(page, model)

        if current_text and current_text == last_text:
            # Text hasn't changed — start (or continue) the stability timer
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= STABLE_SECONDS:
                # Double-check: confirm the stop button is gone (generation done)
                stop_sel, _ = await _first_matching_selector(
                    page, STOP_BUTTON_SELECTORS[model], timeout_ms=500, state="visible"
                )
                if stop_sel is None:
                    return current_text  # Stable and stop button gone
                # Stop button still visible — keep waiting
                stable_since = None
        else:
            # Text changed — reset stability timer
            last_text = current_text
            stable_since = None

    print(f"[warn] Timed out after {MAX_WAIT_SECONDS}s waiting for {model} response")
    return last_text

async def _find_page(browser, model: str) -> Page | None:
    """Locate the already-open browser tab for a given provider."""
    matchers = {
        "chatgpt": lambda u: "chat.openai.com" in u or "chatgpt.com" in u,
        "gemini": lambda u: "gemini.google.com" in u,
        "claude": lambda u: "claude.ai" in u,
    }
    match = matchers.get(model)
    if not match:
        return None

    for context in browser.contexts:
        for candidate in context.pages:
            if match(candidate.url.lower()):
                return candidate
    return None

async def rcv_web_int(model: str, prompt: str) -> str:
    """
    Send *prompt* to an already-open AI web UI tab and return the response text.
    Supported models: "chatgpt", "gemini", "claude"
    """
    model = model.lower()
    if model not in EDITOR_SELECTORS:
        raise ValueError(f"Unsupported model '{model}'. Choose from: {list(EDITOR_SELECTORS)}")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(DEBUG_PORT)

        page = await _find_page(browser, model)
        if not page:
            raise RuntimeError(
                f"No open tab found for '{model}'. Make sure the browser is running with --remote-debugging-port=9222 and the AI tab is open."
            )

        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Find the editor
                selector, _ = await _find_editor(page, model)

                # Inject the prompt
                await _inject_prompt(page, selector, prompt)
                await page.wait_for_timeout(300)

                # Submit
                await _submit(page, model)

                # Wait for a stable response
                response = await _wait_for_response_stable(page, model)

                if response.strip():
                    return response

                raise RuntimeError("Received empty response")

            except Exception as exc:
                last_exc = exc
                print(f"[attempt {attempt}/{MAX_RETRIES}] Error: {exc}")
                if attempt < MAX_RETRIES:
                    backoff = 2 ** attempt  # 2 s, 4 s, …
                    print(f"  Retrying in {backoff}s…")
                    await asyncio.sleep(backoff)

        raise RuntimeError(
            f"All {MAX_RETRIES} attempts failed for '{model}'"
        ) from last_exc

async def main() -> None:
    prompt = pyperclip.paste() # TODO: Add image support
    if not prompt.strip():
        print("[error] Clipboard is empty — nothing to send.")
        return

    response = await rcv_web_int("chatgpt", prompt)
    pyperclip.copy(response)
    subprocess.run("type_text", shell=True)


if __name__ == "__main__":
    asyncio.run(main())