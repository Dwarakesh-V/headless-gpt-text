#!/usr/local/bin/.venv/bin/python3

import asyncio
import time
from playwright.async_api import async_playwright
import pyperclip
import subprocess
import os
os.environ["NODE_NO_WARNINGS"] = "1" # Silence NodeJS warnings

DEBUG_PORT = "http://localhost:9222" # Chrome devtools

async def _find_page(browser, model: str):
    """
    Locate the already-open tab for a given provider.
    """
    for context in browser.contexts:
        for candidate in context.pages:
            url = candidate.url.lower()

            if model == "chatgpt" and ("chat.openai.com" in url or "chatgpt.com" in url):
                return candidate

            if model == "gemini" and "gemini.google.com" in url:
                return candidate

            if model == "claude" and "claude.ai" in url:
                return candidate

    return None


async def _inject_prompt(page, selector, prompt):
    """
    Insert text into a contenteditable editor safely.
    Works across ProseMirror / Quill / Tiptap editors.
    """
    await page.evaluate(
        """({selector, prompt}) => {
            const editor = document.querySelector(selector);
            editor.focus();

            while (editor.firstChild) {
                editor.removeChild(editor.firstChild);
            }

            const p = document.createElement("p");
            p.textContent = prompt;
            editor.appendChild(p);

            editor.dispatchEvent(new InputEvent("input", { bubbles: true }));
        }""",
        {"selector": selector, "prompt": prompt},
    )


async def _wait_for_response(page, msg_selector):
    """
    Wait until model streaming stops by detecting text stabilization.
    """
    time.sleep(5) # Wait for processing time
    await page.wait_for_selector(msg_selector)

    return await page.evaluate(
        """(selector) => new Promise(resolve => {
            const delay = ms => new Promise(r => setTimeout(r, ms));

            async function waitStable() {
                let last = "";

                while (true) {
                    const nodes = document.querySelectorAll(selector);
                    const el = nodes[nodes.length - 1];
                    const current = el ? el.innerText : "";

                    if (current === last && current.length > 0) {
                        resolve(current);
                        return;
                    }

                    last = current;
                    await delay(900);
                }
            }

            waitStable();
        })""",
        msg_selector,
    )


async def rcv_web_int(model: str, prompt: str):
    """
    Send a prompt to an already-open AI web UI tab and return the response.

    Supported models:
    - chatgpt
    - gemini
    - claude
    """
    model = model.lower()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(DEBUG_PORT)

        page = await _find_page(browser, model)
        if not page:
            raise Exception(f"{model} tab not found")

        # CHATGPT
        if model == "chatgpt":
            editor_selector = "#prompt-textarea"
            msg_selector = '[data-message-author-role="assistant"]'

            await page.wait_for_selector(editor_selector)
            await _inject_prompt(page, editor_selector, prompt)
            await page.keyboard.press("Enter")

        # GEMINI
        elif model == "gemini":
            editor_selector = '.ql-editor[contenteditable="true"]'
            msg_selector = "model-response"

            await page.wait_for_selector(editor_selector)
            await _inject_prompt(page, editor_selector, prompt)
            await page.keyboard.press("Enter")

        # CLAUDE
        elif model == "claude":
            editor_selector = '[data-testid="chat-input"]'
            msg_selector = ".font-claude-response"

            await page.wait_for_selector(editor_selector)
            await _inject_prompt(page, editor_selector, prompt)
            await page.keyboard.press("Enter")

        else:
            raise ValueError("Unknown model")

        response = await _wait_for_response(page, msg_selector)
        return response


async def main():
    response = await rcv_web_int("claude", pyperclip.paste())
    pyperclip.copy(response)
    
    subprocess.run(
        "type_text"
    )

if __name__ == "__main__":
    asyncio.run(main())
