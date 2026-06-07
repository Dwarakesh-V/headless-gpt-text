Reliable AI web UI automation via Playwright CDP
Supports: ChatGPT, Gemini, Claude

Clipboard handling:
  - Text  → injected via execCommand / DOM mutation
  - Image → injected via priority chain:
      1. set_input_files() on hidden <input type="file">   (no focus needed)
      2. CDP drag-and-drop onto the editor                 (no focus needed)
      3. JS Clipboard API + Ctrl+V paste                   (needs page focus)

Stability features:
  - Selector fallback chains (test-id → aria → role → heuristic)
  - Content-stability polling instead of brittle button-state checks
  - execCommand + DOM-mutation dual inject for text editors
  - Per-attempt retry with exponential backoff
  - Heuristic editor detection when all known selectors fail