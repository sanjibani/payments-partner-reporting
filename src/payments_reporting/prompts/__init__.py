"""Prompt templates for the LLM nodes.

Voice rules (apply to every prompt and every generated artefact):
- No em-dashes anywhere. ASCII "--" or rephrase.
- Short sentences.
- Name the document / metric before the partner.
- Grounded in the metrics JSON; no inventing numbers.
"""

from __future__ import annotations

from textwrap import dedent


ANALYSIS_SYSTEM = (
    "You are a payments SRE analyst writing a weekly health note for a "
    "partner integration owner. You are concise, evidence-driven, and you "
    "never invent numbers. If the metrics do not support a claim, you say "
    '"no notable signal" instead of speculating.'
)


def analysis_user_prompt(summary_json: str, partner_name: str) -> str:
    return dedent(
        f"""
        Partner: {partner_name}

        Below is the JSON summary of last week's payment traffic for this
        partner. Produce a structured JSON response with exactly these keys:

        - "overview": one paragraph, two or three sentences, the headline.
        - "key_issues": list of strings, up to four. Each must reference a
          specific number from the JSON (gateway, region, error code).
        - "likely_causes": list of strings, up to three. Hypotheses only,
          grounded in the failure buckets and trend deltas.
        - "recommended_actions": list of strings, up to four. Concrete and
          testable (e.g. "replay 12 stuck Braintree charges from EU on 2026-06-29").

        Rules:
        - Do not invent numbers. If you cannot ground a claim, omit it.
        - No em-dashes. Use ASCII punctuation only.
        - Keep the whole response under 400 words.

        Metrics:
        ```json
        {summary_json}
        ```
        """
    ).strip()


EMAIL_SYSTEM = (
    "You are a payments platform team lead writing a weekly status email "
    "to a partner integration owner. You are professional, specific, and "
    "you never oversell. You cite numbers from the supplied context; you "
    "do not invent them."
)


def email_user_prompt(
    partner_name: str,
    tone: str,
    analysis_json: str,
    chart_titles: list[str],
) -> str:
    titles_block = "\n".join(f"- {t}" for t in chart_titles) or "- (no charts)"
    tone_line = {
        "formal": "Use a formal, executive tone.",
        "neutral": "Use a clear, neutral tone.",
        "friendly": "Use a friendly, partnership tone.",
    }.get(tone, "Use a clear, neutral tone.")

    return dedent(
        f"""
        Recipient: {partner_name}
        Tone: {tone_line}

        Produce a JSON response with exactly these keys:
        - "subject": a short email subject line, under 80 characters.
        - "html_body": a complete HTML email body.

        Body structure:
        1. Greeting.
        2. Short headline (one or two sentences).
        3. "What we saw this week" section, three to five bullets, each
           referencing a specific number or chart.
        4. "What we recommend" section, two to three bullets.
        5. Closing line with how to reach the payments team.

        Constraints:
        - Embed the charts by referencing them as plain-text lines like
          "(see chart: Success rate by gateway)". Do not invent URLs.
        - Inline CSS only. No external assets. Body must render in Gmail
          and Outlook desktop.
        - No em-dashes. Use ASCII punctuation only.
        - Total body length: under 350 words.

        Available charts:
        {titles_block}

        Analysis context:
        ```json
        {analysis_json}
        ```
        """
    ).strip()