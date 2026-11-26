"""The contract review itself: one Claude request per document, answered in a fixed JSON shape (structured outputs) so
the page can show the same five sections every time: drafting errors, key terms, terms to push back on, money and
liability, term and termination.

The document goes to Claude as a PDF (Word files are converted with LibreOffice first), so clause numbering, tables
and layout reach the model as the counterparty sees them; numbering mistakes are among the drafting errors we want.

Needs ANTHROPIC_API_KEY (Azure: Key Vault secret `anthropic-api-key`, see infra/azure/app.bicep). CONTRACTS_MODEL
overrides the model."""
import base64, json, os
from datetime import date

MODEL = os.environ.get("CONTRACTS_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CONTRACTS_EFFORT", "high")
MAX_PAGES = 500

CATEGORIES = {
    "nda": "NDA / confidentiality",
    "engagement": "Professional services engagement",
    "vendor": "Vendor agreement (data / tech)",
    "trial": "Trial / pilot",
    "other": "Other",
}


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# --------------------------------------------------------------------------------------------- what we ask for
_CATEGORY_FOCUS = {
    "nda": """NDAs (often with allocators, prospective investors, counterparties or service providers): whether it is
truly mutual or one-sided in effect; who may receive information (employees, members, advisers, affiliates) and whether
Demeisen can work with it day to day; standard of care ("best endeavours" is onerous under English law); breadth of the
definition; exclusions and whether compelled/regulatory disclosure keeps the rest protected and needs notice; term and
survival, especially for trading strategies, positions, models and code; residuals clauses and independent-development
carve-outs (dangerous for an investment manager disclosing IP); whether the other side can share Demeisen's information
across its own group or with its own investment teams; non-solicitation; restrictions that would clash with investor
MFN, side-letter or AIFMD/FCA disclosure obligations; third-party enforcement rights; governing law AND jurisdiction.""",
    "engagement": """Professional services engagement letters (lawyers, accountants, auditors, administrators, consultants,
placement agents): scope and exclusions; fee basis, rates, estimates, caps, success or tail fees, expenses and
disbursements, rate increases, payment terms and interest; caps on the ADVISER's liability (amount, per-claim or
aggregate, proportionate-liability clauses, exclusion of consequential loss, time bars) and any indemnity Demeisen gives
the adviser; who is the client (the LLP, the fund, members personally) and personal guarantees; conflicts and
exclusivity; reliance and third-party use of advice; ownership of work product; termination fees, notice and what is
payable on early termination; standard terms incorporated by reference (flag that they need reading if not attached).""",
    "vendor": """Vendor agreements for data and technology: licence scope (users, sites, entities, funds, affiliates), permitted
use (internal research, trading, investor reporting), derived data and models built from the data, display and
redistribution limits, audit rights and the cost of a failed audit, usage-based or per-user fees, price increases on
renewal, minimum commitments, auto-renewal and the notice window to stop it, termination for convenience, what happens
to data and derived works on termination (deletion and certification duties), SLAs and service credits, liability caps
and whether Demeisen's liability is uncapped (typically for licence breach), indemnities (IP infringement from the
vendor), data protection (UK GDPR) and security, sub-processors, governing law, and terms the vendor may change
unilaterally or incorporate by URL.""",
    "trial": """Trials and pilots of data or technology: whether it converts automatically to a paid subscription and when;
fees after the trial; whether signing the trial also accepts a full master agreement; usage restrictions during the
trial and whether trial data may be used in live trading or research; what must be deleted when the trial ends and
whether derived work must be destroyed; feedback and IP clauses that give the vendor rights in Demeisen's ideas;
publicity (use of Demeisen's name); liability during the trial; notice needed to end it.""",
    "other": """Read it as an experienced commercial lawyer would for an investment management firm, using the most relevant of:
confidentiality, fees and liability, termination, IP, data protection, regulatory obligations, and exclusivity.""",
}

SYSTEM = """You review contracts for Demeisen LLP, a small London investment management firm (an English LLP). Members
use your review to decide what to sign, what to push back on, and what to send to counsel. Read the whole document
before writing, and review it from Demeisen's side.

What to produce (it fills a fixed form; every field is required):
- overview: what the document is, the parties and their roles, Demeisen's position, dates, governing law and courts,
  a short plain-English summary, an overall risk rating and a one-sentence recommendation.
- drafting_errors: genuine mistakes in the drafting: wrong or broken cross-references, numbering that restarts or skips,
  defined terms used but not defined or defined inconsistently, wrong party named, obligations that point the wrong
  way, duplicated or contradictory sentences, blanks and placeholders, typos that change meaning, missing schedules or
  documents referred to. Include small ones; say why each matters (or that it is cosmetic) and the fix. Do not list
  commercial points here.
- key_terms: the terms someone needs to know before signing, in the document's order, each with its clause.
- concerns: terms that are off-market, one-sided, unusually onerous, or that could cause Demeisen real trouble. Rate
  each: high (should not sign without changing it), medium (push for a change), low (worth raising, acceptable if
  refused). For each say what it says, why it matters to Demeisen in practice, and the specific change to ask for.
  Order by severity. Do not pad: if a clause is market standard, it is not a concern.
- financial: every fee, rate, cost, payment term, price-change right and minimum commitment; then liability: caps,
  exclusions, indemnities (who gives them, for what, capped or not), insurance, and anything uncapped. Use "None
  stated" where the document is silent, and say so in the summary; silence is itself informative.
- termination: the term and any renewal, every way each party can end it and the notice needed, what survives, what
  must be returned or destroyed, and any payments due on termination. Put dates and notice deadlines Demeisen must
  diary in key_dates, computed from the document's dates where possible.
- missing: protections a firm in Demeisen's position would normally expect that are absent.

Rules: quote or cite clause numbers as the document shows them (e.g. "Cl. 4(b)", "Sch. 2 para 3"); use
"Preamble", "Recitals", "Signature block" or the heading where there is no number. Base everything on the document;
when you rely on general market practice or English law, say so. If the document is not a contract, or is
unreadable, say that in the overview and leave the lists empty. Write in plain British English for a senior finance
professional, not a lawyer: short sentences, no hedging boilerplate.

What to look for in this kind of document:
"""


def _items(*fields):
    return {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": list(fields),
                                       "properties": {f: {"type": "string"} for f in fields}}}


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["overview", "drafting_errors", "key_terms", "concerns", "financial", "termination", "missing"],
    "properties": {
        "overview": {
            "type": "object", "additionalProperties": False,
            "required": ["document_type", "parties", "demeisen_position", "dates", "governing_law", "summary",
                         "overall_risk", "recommendation"],
            "properties": {
                "document_type": {"type": "string"},
                "parties": _items("name", "role"),
                "demeisen_position": {"type": "string"},
                "dates": {"type": "string"},
                "governing_law": {"type": "string"},
                "summary": {"type": "string"},
                "overall_risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "recommendation": {"type": "string"},
            }},
        "drafting_errors": _items("clause", "issue", "fix"),
        "key_terms": _items("term", "clause", "summary"),
        "concerns": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["severity", "clause", "title", "issue", "why_it_matters", "suggested_change"],
            "properties": {
                "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                "clause": {"type": "string"}, "title": {"type": "string"}, "issue": {"type": "string"},
                "why_it_matters": {"type": "string"}, "suggested_change": {"type": "string"},
            }}},
        "financial": {
            "type": "object", "additionalProperties": False, "required": ["summary", "fees", "liability"],
            "properties": {"summary": {"type": "string"}, "fees": _items("item", "clause", "detail"),
                           "liability": _items("item", "clause", "detail")}},
        "termination": {
            "type": "object", "additionalProperties": False, "required": ["summary", "provisions", "key_dates"],
            "properties": {"summary": {"type": "string"}, "provisions": _items("item", "clause", "detail"),
                           "key_dates": _items("when", "what")}},
        "missing": _items("item", "why"),
    },
}


class ReviewError(Exception):
    pass


def _client():
    import anthropic
    return anthropic.Anthropic(timeout=900.0, max_retries=3)


def run(pdf_path, *, category, title, counterparty="", context=""):
    """Reviews one PDF. Returns (review dict, info dict with model and token use). Raises ReviewError."""
    import anthropic
    from pypdf import PdfReader
    pages = len(PdfReader(pdf_path).pages)
    if pages > MAX_PAGES:
        raise ReviewError(f"The document has {pages} pages; the limit is {MAX_PAGES}. Split it and upload the parts.")
    with open(pdf_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    brief = [f"Category: {CATEGORIES.get(category, category)}", f"Today's date: {date.today():%d %B %Y}",
             f"File name: {title}"]
    if counterparty: brief.append(f"Counterparty: {counterparty}")
    if context: brief.append(f"Context from Demeisen (their own words, for background only):\n{context}")
    brief.append("Review the attached document.")
    try:
        with _client().beta.messages.stream(
            model=MODEL, max_tokens=64000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM + _CATEGORY_FOCUS.get(category, _CATEGORY_FOCUS["other"]),
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                 "title": title},
                {"type": "text", "text": "\n".join(brief)},
            ]}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise ReviewError("The Claude API key was refused. Check the Key Vault secret anthropic-api-key.")
    except anthropic.BadRequestError as e:
        raise ReviewError(f"Claude could not take this document: {e.message}")
    except anthropic.RateLimitError:
        raise ReviewError("Claude's rate limit was reached. Try again in a few minutes.")
    except anthropic.APIStatusError as e:
        raise ReviewError(f"Claude API error {e.status_code}. Try again later.")
    except anthropic.APIConnectionError:
        raise ReviewError("Could not reach the Claude API (network).")
    if msg.stop_reason == "refusal":
        raise ReviewError("Claude declined to review this document.")
    if msg.stop_reason == "max_tokens":
        raise ReviewError("The review was cut off (too long). Try again, or split the document.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        review = json.loads(text)
    except ValueError:
        raise ReviewError("Claude's answer could not be read. Try again.")
    info = {"model": msg.model, "pages": pages, "tokens_in": msg.usage.input_tokens,
            "tokens_out": msg.usage.output_tokens}
    return review, info
