# Summarize & Format Digest

You receive a list of deduplicated news articles with full descriptions.
Summarize each into a concise digest entry and format for delivery.

## Per-Article Task
1. Write a punchy headline (title)
2. Summarize the description to exactly 2-3 sharp sentences
3. Add 1 sentence for "why it matters"
4. Sort by significance within each category

## Format

{{DIGEST_HEADER}} — {{DATE}}

{{SECTIONS}}

## Per-Item Format

[**Title**](url1)
[2-3 sentence summary]
_Why it matters: [1 sentence]_
→ [source_label1](url1), [source_label2](url2)

## Rules
- Omit category sections with no items
- Link each title to its first/primary URL so it appears as a clickable blue link
- List all source URLs on the → line as separate markdown links
- Tone: {{TONE}}
- Output ONLY the formatted message — no JSON, no preamble

## Articles
{{ARTICLES}}
