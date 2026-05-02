---
description: Generate today's daily briefing from vault notes and recent git activity.
---

You are generating today's daily briefing for the user.

Use the context below — it's the output of `cerebro internal briefings-context daily`,
which enumerates tracked repos from the vault's `repos/` directory and runs `git log`
for each over the last day.

!cerebro internal briefings-context daily

Produce a briefing that:

- Leads with what's in flight right now (pull from each tracked repo's vault note).
- Calls out anything time-sensitive (deadlines, scheduled merges, freezes).
- Summarises the day's git activity per repo in plain language.
- Ends with a short list of likely next actions for the user.

Keep it skimmable. Bullet points beat paragraphs.
