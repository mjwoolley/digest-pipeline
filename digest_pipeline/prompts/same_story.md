# Same-Story Adjudication

You are deduplicating a daily news digest. Each pair below has a NEW candidate article (title + description excerpt) and the title of an article ALREADY PUBLISHED in a recent digest.

For each pair, decide whether the new article covers the SAME underlying story or event as the previously published one — the same announcement, launch, release, paper, funding round, or incident. Coverage from a different outlet with different framing still counts as the same story. Merely sharing a topic area, company, or product family does NOT count.

## Output Format
JSON array, one object per pair, echoing each pair's id exactly:
[
  {"id": 0, "same_story": true},
  {"id": 3, "same_story": false}
]

Output ONLY the JSON array, no markdown fences, no commentary.

## Pairs
{{PAIRS}}
