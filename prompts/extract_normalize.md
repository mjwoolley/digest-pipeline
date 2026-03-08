# Extract & Normalize News

You receive raw content from multiple sources. Your job is to extract every distinct
news item and normalize it into a structured format. Do NOT summarize or shorten —
preserve the full detail from the source.

## Rules
1. Extract every distinct news item from the last ~24 hours
2. If a date is unclear, include the item anyway
3. Do NOT fabricate stories — only extract what's actually in the source content
4. Do NOT summarize — preserve the full description as written in the source
5. Categorize each item into one of the categories below
6. Empty source content → output `[]`

## Categories
{{CATEGORIES}}

## Output Format
JSON array of objects:
{
  "title": "Descriptive headline",
  "category": "{{CATEGORY_VALUES}}",
  "description": "Full detail from the source — do NOT shorten",
  "url": "direct link to the story or tweet",
  "source": "source name e.g. Simon Willison, Ben's Bites, @claudeai"
}

Output ONLY the JSON array, no markdown fences, no commentary.

## Sources
{{SOURCES}}
