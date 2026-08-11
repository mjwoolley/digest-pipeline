# Merge Duplicate News Articles

You receive groups of news articles that have been identified as covering the same story.
For each group, merge them into ONE canonical article.

## Your Task
For each group:
1. Pick the best/punchiest title
2. Combine the descriptions into the most complete version (preserve key details from all sources)
3. Collect ALL unique urls into a list
4. Keep the most specific category

NOTE: Do NOT include source fields (source_key, source_type, source_label, source_url) — those are handled by code.

## Output Format
JSON array of merged objects. `group_id` MUST echo the group number from the input heading (e.g. articles merged from "## Group 2" get `"group_id": 2`):
{
  "group_id": 1,
  "title": "Best headline",
  "category": "{{CATEGORY_VALUES}}",
  "description": "Combined full description with details from all sources",
  "urls": ["url1", "url2"]
}

Output ONLY the JSON array, no markdown fences, no commentary.

## Article Groups
{{CLUSTERS}}
