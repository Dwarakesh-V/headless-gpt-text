Reliable AI web UI automation via Playwright CDP
Supports: ChatGPT, Gemini, Claude

Stability features:
  - Selector fallback chains (test-id → aria → role → heuristic)
  - Content-stability polling instead of brittle button-state checks
  - execCommand + DOM-mutation dual inject for all editor types
  - Per-attempt retry with exponential backoff
  - Heuristic editor detection when all known selectors fail