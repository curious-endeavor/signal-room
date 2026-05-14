# Build the v1 Curious Endeavor Signal Room MVP

Create a local Python tool that helps Curious Endeavor find mechanism-rich signals for content, client thinking, and pitch references. The MVP should generate a shareable static HTML daily digest with the top 10 signals, scored through the CE lens.

## Objective

Create a local tool that helps CE find mechanism-rich signals we can use for content, client thinking, and pitch references. The MVP should generate a shareable HTML daily digest with the top 10 signals, scored through the CE lens.

## Core Job

Every day, the tool should find relevant signals from seeded sources plus search discovery, explain why CE should care, and help us decide what is worth turning into content.

Use Python unless there is a strong reason not to.

## Output

- A shareable static HTML digest that a non-technical team can open and review.
- Store raw items, enriched/scored items, source candidates, and feedback locally as JSON/SQLite.
- Include a simple way to mark each surfaced item as useful / not useful / wrong pillar / too generic / source worth following / turned into content.
- Feedback should influence future scoring, even if the first version uses a simple local weighting file.

## CE Lens

### Pillars

- P1. Creating content with AI
- P2. Designing with AI
- P3. Turning a marketing team into AI-first
- P4. Selecting which workflows are AI-ready and which are not
- P5. Assessing good vs. bad in human-in-the-loop AI work

### Surf

- S1. AI-native brand-building in the wild: examples, case studies, agencies doing this, new visual/voice/brand patterns
- S2. Excellent classic branding: especially useful as "we did this, so we can do that" reference material

### Filter Out

- Generic "AI will change everything" takes
- Raw model/benchmark news with no agency, brand, workflow, or marketing angle
- Vendor announcements without case study, mechanism, or workflow detail

## Scoring

Each item needs:

- Pillar fit: P1-P5
- Surf fit: S1-S2 when relevant
- mechanism_present: yes/no
- score
- reason for score
- why CE should care
- suggested CE angle
- possible CE take, as a sharp angle not a polished final post
- follow-up search query
- source URL and date

Mechanism-rich items should rank above generic announcements. Especially reward examples with workflows, methods, implementation detail, failure modes, human-in-the-loop judgment, or "don't use AI for this yet" lessons.

## Source Discovery

Use seed + search.

Start with a curated seed list around AI content, AI design, AI-native teams, workflow transformation, HITL AI, branding, and agency case studies.

Also propose new candidate sources daily, but do not treat them as trusted until marked useful.

Seed examples include:

- Lenny's Newsletter
- Every
- The Generalist
- Ben's Bites
- Latent Space
- Maggie Appleton
- Are.na
- Brand New
- Eye on Design
- Fonts In Use
- Reforge
- First Round Review
- Lenny podcast
- Decoder
- Ethan Mollick
- Gary Marcus
- HN threads
- Relevant Reddit threads
- Cases
- Agency case study pages
- Cursor/Linear-style AI-native company case studies
- FT/NYT business design coverage
- Identity-design Substacks

## Test Exemplars

The MVP should be able to recognize why these are valuable CE signals:

### 1. Owner/Grader "AI CMO for restaurants"

Why we care: AI marketing for SMBs, CMO-as-software, brand/marketing work being productized.

Likely fit: P1, P3, S1.

### 2. Ramp Glass "every employee gets an AI coworker"

Why we care: AI becoming multiplayer inside companies; skills, memory, shared workflows, internal AI infrastructure as moat.

Likely fit: P3, P4, P5.

### 3. Anthropic/OpenAI verticalized service-company / consulting-arm news

Why we care: CE is also a verticalized AI service company; labs validating implementation, workflow redesign, and forward-deployed AI work.

Likely fit: P3, P4, S1.

## Voice Grounding

Use this as one voice/context artifact:

https://www.curiousendeavor.com/figma-story/

## Out of Scope for v1

- Polished app UI
- Post editor
- Image generation
- Scheduling/publishing
- Multi-client support
- Full automation before the manual run works
- Overbuilding source management

## Success Criteria

The MVP is successful if CE actually uses it as a source for content we create. After one week, it should produce at least a few signals or angles that the team would plausibly turn into posts, client references, or pitch material.
