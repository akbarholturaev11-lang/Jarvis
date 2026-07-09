# NEXT_STEPS.md

Current next steps for MARK XLVIII - AkbarCustom.

## Immediate Next Steps

1. Stabilize Gemini Live reconnect / `APIError 1006` if needed.
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
4. Translate UI to Russian if Akbar wants.
5. Add Akbar-specific assistant personality/rules.
6. Add stronger project memory/context system.
7. Later add custom features.

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
