# NEXT_STEPS.md

Current next steps for MARK XLVIII - AkbarCustom.

## Immediate Next Steps

1. Long-run test Gemini Live reconnect / `APIError 1006` recovery on Mac.
2. Test Mac permissions:
   - Microphone
   - Accessibility
   - Screen Recording
   - Camera
3. Manually verify Universal Device Intelligence:
   - Start with no `config/device_profile.json` and confirm it is created.
   - Ask `qaysi qurilmada ishlayapsan?`
   - Ask `asosiy browser qaysi?`
   - Ask `Telegram bormi?`
   - Ask `musiqani to'xtat`
   - Ask `qurilmani qayta tekshir`
   - Confirm unknown permissions/capabilities are reported honestly.
4. Test core commands:
   - Open Safari
   - Look at my screen
   - Open Telegram
   - Set reminder
   - Web search
   - File processor
5. Review any remaining English-only action/tool result wording that should be promoted into the shared English/Russian UI dictionary.
6. Add Akbar-specific assistant personality/rules.
7. Manually verify UI language switching in the full Mac app with restart: Russian -> English -> Russian.
8. Manually verify v0.3.1 runtime routing in the Mac app:
   - YouTube/media play then `to'xtat` resolves to media pause, not close/settings close.
   - `hali ham o'ynayapti` attaches correction and uses safe fallback without claiming success.
   - message `yubor` follow-ups ask confirmation unless target/chat/delivery are verified.
9. Manually verify Personal Operations Briefing in the full Gemini Live app:
   - Start the app and confirm startup gives Personal Operations Briefing, not generic world news.
   - Say `men uydaman` and confirm the `personal_briefing` route is used.
   - Say `dunyo yangiliklarini ayt` and confirm the existing world-news route is used.
   - Ask `Telegram kanalim statistikasi qanday?` and confirm `not_configured` with no numbers.
   - Confirm local project output contains evidence-based `foyda`, `zarar`, and `next_action`.
10. Confirm a fresh runtime terminal is not flooded by the sounddevice NumPy 2.5 shape warning; unrelated warnings must remain visible.
11. Supply an official Zerno API contract/config only before implementing a real network adapter. Telegram/Instagram/Messenger also remain `not_configured` until real supported APIs are provided.
12. Later add custom features.

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
