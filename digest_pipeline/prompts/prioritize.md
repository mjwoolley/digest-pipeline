# Prioritize Articles

You receive a list of news articles as JSON. Score each article from 1-10 on importance.

## Scoring Criteria
- **Impact & significance** (8-10): Major model releases, breaking industry news, significant policy changes
- **Novelty** (6-8): Genuinely new developments, not incremental updates or minor version bumps
- **Breadth of interest** (5-7): Relevant to a wide audience vs niche
- **Actionability** (4-6): Something practitioners can act on today

Give higher scores to articles that are significant, novel, and broadly relevant.
Give lower scores to minor updates, routine announcements, or highly niche topics.

## Categories
{{CATEGORIES}}

## Constraint
We need to select the top ~{{MAX_ARTICLES}} articles from this list. Score generously for must-know items and critically for filler.

## Output Format
JSON array in the SAME ORDER as the input:
[
  {"title": "exact title from input", "score": 8},
  {"title": "exact title from input", "score": 3},
  ...
]

Output ONLY the JSON array, no markdown fences, no commentary.

## Articles
{{ARTICLES}}