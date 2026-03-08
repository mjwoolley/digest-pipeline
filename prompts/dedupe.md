# Merge Duplicate News Articles

You receive groups of news articles that have been identified as covering the same story.
For each group, merge them into ONE canonical article.

## Your Task
For each group:
1. Pick the best/punchiest title
2. Combine the descriptions into the most complete version (preserve key details from all sources)
3. Collect ALL unique urls into a list
4. Collect ALL unique source names into a list
5. Keep the most specific category

## Output Format
JSON array of merged objects:
{
  "title": "Best headline",
  "category": "{{CATEGORY_VALUES}}",
  "description": "Combined full description with details from all sources",
  "urls": ["url1", "url2"],
  "sources": ["Source A", "Source B"]
}

Output ONLY the JSON array, no markdown fences, no commentary.

## Article Groups
{{CLUSTERS}}
