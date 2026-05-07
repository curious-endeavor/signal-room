from datetime import date
from html import escape
from pathlib import Path
from typing import Iterable, List

from .models import FEEDBACK_ACTIONS, ScoredItem


def render_digest(items: Iterable[ScoredItem], output_path: Path) -> None:
    rows = list(items)
    cards = "\n".join(_render_card(index + 1, item) for index, item in enumerate(rows[:10]))
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Curious Endeavor Signal Room - {date.today().isoformat()}</title>
  <style>
    :root {{
      --ink: #171717;
      --muted: #5f6368;
      --line: #d8d4ca;
      --paper: #faf8f1;
      --panel: #fffdf8;
      --accent: #0f766e;
      --warn: #9a3412;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: var(--paper);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    main {{
      width: min(1120px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      padding-bottom: 20px;
      margin-bottom: 22px;
    }}
    h1 {{ font-size: clamp(28px, 4vw, 44px); margin: 0 0 8px; }}
    h2 {{ font-size: 22px; margin: 0 0 10px; }}
    h3 {{ font-size: 16px; margin: 20px 0 8px; }}
    p {{ margin: 0 0 10px; }}
    .muted {{ color: var(--muted); }}
    .digest-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 14px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      margin: 16px 0;
    }}
    .card-head {{
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 14px;
      align-items: start;
    }}
    .rank {{
      width: 36px;
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 999px;
      display: grid;
      place-items: center;
      font-weight: 700;
      background: #f1efe7;
    }}
    .score {{
      font-size: 28px;
      font-weight: 800;
      color: var(--accent);
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 8px 0 12px;
    }}
    .chip {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      background: #f7f3e8;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin-top: 14px;
    }}
    .field {{
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }}
    .field strong {{
      display: block;
      font-size: 12px;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 4px;
      letter-spacing: 0.04em;
    }}
    code {{
      display: block;
      white-space: pre-wrap;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: #f5f1e6;
      color: #242424;
      font-size: 13px;
    }}
    a {{ color: var(--accent); }}
    .candidate {{ color: var(--warn); font-weight: 700; }}
    @media (max-width: 760px) {{
      main {{ width: min(100vw - 20px, 1120px); padding-top: 20px; }}
      .card-head {{ grid-template-columns: auto 1fr; }}
      .score {{ grid-column: 2; font-size: 22px; }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Curious Endeavor Signal Room</h1>
      <p class="muted">Daily digest of mechanism-rich signals ranked through the CE lens.</p>
      <div class="digest-meta">
        <span>{date.today().isoformat()}</span>
        <span>{len(rows[:10])} surfaced signals</span>
        <span>Local MVP, fixture/search-candidate mode</span>
      </div>
    </header>
    {cards}
  </main>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def _render_card(rank: int, item: ScoredItem) -> str:
    chips = _chips(item)
    fields = [
        ("Reason for score", item.reason_for_score),
        ("Why CE should care", item.why_ce_should_care),
        ("Suggested CE angle", item.suggested_ce_angle),
        ("Possible CE take", item.possible_ce_take),
        ("Follow-up search query", item.follow_up_search_query),
        ("Feedback command", _feedback_command(item.id)),
    ]
    field_html = "\n".join(_field(label, value) for label, value in fields)
    candidate = '<span class="candidate">Candidate source</span>' if item.candidate_source else "Trusted/seeded source"
    return f"""
    <article class="card" id="{escape(item.id)}">
      <div class="card-head">
        <div class="rank">{rank}</div>
        <div>
          <h2>{escape(item.title)}</h2>
          <p class="muted">{escape(item.source)} · {escape(item.date)} · {candidate}</p>
          <p><a href="{escape(item.source_url)}">{escape(item.source_url)}</a></p>
        </div>
        <div class="score">{item.score:.0f}</div>
      </div>
      <div class="chips">{chips}</div>
      <p>{escape(item.summary)}</p>
      <div class="grid">
        {field_html}
      </div>
    </article>
"""


def _chips(item: ScoredItem) -> str:
    values: List[str] = []
    values.extend(item.pillar_fit or ["no pillar"])
    values.extend(item.surf_fit)
    values.append(f"mechanism: {'yes' if item.mechanism_present else 'no'}")
    values.append(item.discovery_method)
    return "\n".join(f'<span class="chip">{escape(value)}</span>' for value in values)


def _field(label: str, value: str) -> str:
    if label == "Feedback command":
        return f'<div class="field"><strong>{escape(label)}</strong><code>{escape(value)}</code></div>'
    return f'<div class="field"><strong>{escape(label)}</strong><p>{escape(value)}</p></div>'


def _feedback_command(item_id: str) -> str:
    actions = "|".join(sorted(FEEDBACK_ACTIONS))
    return f"python3 -m signal_room feedback --item-id {item_id} --action <{actions}> --note \"optional note\""
