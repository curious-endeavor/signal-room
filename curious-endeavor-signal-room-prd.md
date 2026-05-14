# Curious Endeavor Signal Room PRD

## Summary

Signal Room is a daily signal-finding tool for Curious Endeavor.

Its job is to help the team discover mechanism-rich stories, examples, case studies, workflows, and cultural/design shifts that are worth turning into content, client references, or pitch material.

The product should feel less like a generic feed reader and more like a research analyst with memory and a point of view.

## Problem

Curious Endeavor needs a reliable way to know what is happening around AI-native marketing, AI-assisted design, AI-first teams, and modern brand-building.

The team does not just need links. It needs help answering:

- Why does this matter to CE?
- What mechanism, workflow, behavior, or market shift does this reveal?
- Is this worth reacting to?
- Could this become a post, a client reference, or a pitch angle?
- Is this source worth following again?

Without a strong signal layer, downstream content tools become generic LLM wrappers.

## Product Goal

Create a reviewable daily workspace that surfaces the best signals for CE, explains why they matter, and lets the team accept, reject, or convert them into content opportunities.

For v1, the goal is not publishing automation. The goal is to produce a useful daily list that the team actually reviews.

## Primary User

The primary user is the CE team, especially people deciding what CE should notice, react to, write about, reference in client conversations, or use in pitches.

Assaf's design need: the interface should support fast judgment. The user should be able to scan, understand why something matters, mark feedback, and move on.

## Core Use Case

Every day, a CE team member opens Signal Room and sees a ranked list of surfaced signals.

For each signal, they can quickly understand:

- What happened
- Where it came from
- Why CE should care
- Which CE pillar it fits
- What possible angle CE could take
- Whether the source or topic should influence future discovery

## V1 Scope

### Must Have

- A daily ranked list of signals.
- Top 10 surfaced items by default.
- Clear scoring through the CE lens.
- A concise explanation for why each item matters.
- Source URL and date.
- Feedback actions:
  - Useful
  - Not useful
  - Wrong pillar
  - Too generic
  - Source worth following
  - Turned into content
- A simple way to run searches and review results.
- A way to test multiple query phrasings and compare output quality.

### Should Have

- Candidate source discovery.
- Follow-up search query per item.
- Recent run history.
- Source/source-type filters.
- Lookback window control.
- Lightweight web UI for review.

### Out of Scope for V1

- Polished publishing workflow.
- Full post editor.
- Scheduling.
- Image generation.
- Multi-client support.
- Fully automated daily operation before the manual review loop works.
- Overbuilt source management.

## CE Lens

### Pillars

- P1. Creating content with AI
- P2. Designing with AI
- P3. Turning a marketing team into AI-first
- P4. Selecting which workflows are AI-ready and which are not
- P5. Assessing good vs. bad in human-in-the-loop AI work

### Surf

- S1. AI-native brand-building in the wild
- S2. Excellent classic branding reference material

## What Good Signals Look Like

Signal Room should reward items with:

- Concrete workflows
- Methods or implementation detail
- Case studies
- Failure modes
- Human-in-the-loop judgment
- Brand, agency, marketing, or design relevance
- Evidence that a broader pattern may be forming

Signal Room should suppress:

- Generic "AI will change everything" takes
- Raw model or benchmark news without a CE angle
- Vendor announcements with no workflow, mechanism, or case-study detail
- Thin trend commentary with no example

## Example Signals

### Owner/Grader: AI CMO for Restaurants

Why CE cares: AI marketing for SMBs, CMO-as-software, and brand/marketing work becoming productized.

Likely fit: P1, P3, S1.

### Ramp Glass: Every Employee Gets an AI Coworker

Why CE cares: AI becoming multiplayer inside companies through memory, skills, shared workflows, and internal AI infrastructure.

Likely fit: P3, P4, P5.

### OpenAI/Anthropic Service-Company or Consulting-Arm News

Why CE cares: AI labs validating implementation, workflow redesign, and forward-deployed AI service work.

Likely fit: P3, P4, S1.

## UX Principles

- Prioritize signal quality over visual polish.
- Make the page useful for scanning, not reading every card in full.
- Put the "why CE should care" near the top of each item.
- Make feedback actions obvious and low-friction.
- Avoid a generic news-feed feeling.
- Show enough source context for trust.
- Make follow-up exploration easy.
- Design for a daily review habit.

## Suggested Screen Structure

### Home/Search

- Search input
- Source filters
- Lookback selector
- Suggested searches
- Recent runs

### Results

- Ranked list of items
- Each item shows:
  - Rank
  - Title
  - Source
  - Date
  - Score
  - Pillar fit
  - Why CE should care
  - Suggested CE angle
  - Possible CE take
  - Follow-up search
  - Feedback actions

### Query Lab

- Compare multiple query phrasings.
- Show which queries returned the best CE-relevant candidates.
- Help refine the discovery engine over time.

## Success Criteria

After one week of daily use, Signal Room should produce at least a few signals or angles that CE would plausibly turn into:

- A social post
- A client reference
- A pitch example
- A strategic POV
- A new source worth following

If the team opens it and says "this is a good source of things for us to react to," v1 is working.

## Current Implementation Notes

The current repo already includes:

- Python CLI
- Static HTML digest generation
- CE scoring heuristics
- Local JSON artifacts
- Feedback logging
- Source feedback weighting
- Optional `last30days` fetch adapter
- Query lab
- Lightweight FastAPI web UI
- Render deployment blueprint

The main product/design gap is turning the existing functionality into a focused review experience that makes daily judgment fast and useful.
