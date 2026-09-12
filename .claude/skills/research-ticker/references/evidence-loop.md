# Closing a coverage gap

Coverage is `insufficient` when any planned question has no validated sourced
answer. That blocks BUY, ADD and EXIT downstream. Rerunning `research` changes
nothing on its own — the planner writes fresh questions from the same thesis
and retrieval fetches the same generic news.

The fix is the owner supplying evidence. Say that plainly; it is not the
pipeline finding it.

**The file format, matching rules and worked example are in `docs/commands.md`
under the research section.** Read it rather than restating it here, and point
the owner at it too. `main.py context` prints the directory the file belongs
in; it never goes in the repository.

## What the skill adds

**The quote has to be the published words.** The citation check requires the
analyst's `supporting_quote` to appear verbatim in `title\nexcerpt`. An excerpt
paraphrased in the owner's own words cannot be cited, and the answer comes back
`unanswered` with a validation error. Paste the passage as published.

**Each rerun replans.** Questions are regenerated from the thesis every time,
and matching is by text containment, so a rephrased question silently excludes
an excerpt tagged with the old wording. `questions: []` matches on symbol alone
and survives replanning.

**Dropped silently.** An entry outside the age window, or with a non-http(s)
URL, is discarded without a message. If an excerpt you added did not appear in
the evidence list, check the date first. `main.py process` prints the live
window.

## The loop

1. Read the uncovered questions from the `research` output — printed verbatim
   so they can be pasted straight into a `questions` field.
2. Find the primary document answering each: filing, earnings release,
   transcript, official announcement.
3. Add an entry per question with the exact passage.
4. Rerun `main.py research TICKER`.
5. Report which questions moved to `sourced`, which did not, and whether the
   thesis status changed once real citations existed.

Step 5 is the interesting one. A status that flips only after the owner supplies
the evidence says something about what the feed was contributing.
