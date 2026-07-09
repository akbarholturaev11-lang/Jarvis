# NEXT_STEPS.md

Current next steps for MARK XLVIII - AkbarCustom.

## Immediate Next Steps

1. Long-run test Gemini Live reconnect / `APIError 1006` recovery on Mac.
2. Test Mac permissions:
   - Microphone
   - Accessibility
   - Screen Recording
   - Camera
3. Test core commands:
   - Open Safari
   - Look at my screen
   - Open Telegram
   - Set reminder
   - Web search
   - File processor
4. Review any remaining English-only action/tool result wording that should be promoted into the shared English/Russian UI dictionary.
5. Add Akbar-specific assistant personality/rules.
6. Manually verify UI language switching in the full Mac app with restart: Russian -> English -> Russian.
7. Manually verify v0.3.1 runtime routing in the Mac app:
   - YouTube/media play then `to'xtat` resolves to media pause, not close/settings close.
   - `hali ham o'ynayapti` attaches correction and uses safe fallback without claiming success.
   - message `yubor` follow-ups ask confirmation unless target/chat/delivery are verified.
   - terminal is no longer flooded by repeated sounddevice NumPy deprecation warnings.
8. Later add custom features.

## Context System Follow-Up

- Keep the markdown knowledge graph in `PROJECT_MAP.md` current when architecture changes.
- Keep `AI_RESOURCES.md` current when important files or risk levels change.
- Keep `PROJECT_MEMORY.md` focused on durable context only.
- Log meaningful implementation changes in `CHANGELOG_AKBAR.md`.

## Not Planned Right Now

- Do not install Graphiti/Gravity-style external dependencies unless Akbar explicitly asks and the benefit is clear.
- Do not refactor runtime logic during context-foundation work.
- Do not change API key config.
- Do not modify `.venv/`.
