"""Deterministic pre-filters: title allowlist + seniority + exclusion
keywords + location allowlist + JD checks (stack dealbreakers, security
clearance, required accessibility certifications, years of experience).

These run BEFORE any AI call — the whole point is to keep the AI-scored
volume small and cheap. The actual keyword/pattern lists live in
`filters.yaml` at the repo root (loaded below) so they can be edited
without touching code.

Design note (2026-08-11 rewrite): an EXCLUSION-only approach doesn't scale
to a company like Affirm that posts hundreds of non-engineering roles
(Analyst, Compliance, Marketing, Customer Success, Talent Brand, Sales
Development...) — you can't blacklist your way out of that. So title
filtering is allowlist-first: the title must look like an engineering
role AT ALL before we even consider it, then a smaller exclusion list
catches engineering-adjacent titles that aren't a fit (sales engineer,
hardware/mechanical engineer, etc).
"""
import re

import yaml

FILTERS_CONFIG_PATH = "filters.yaml"


def _load_config(path: str = FILTERS_CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


_config = _load_config()

PRIORITY_TITLE_KEYWORDS = _config["priority_title_keywords"]
TITLE_ALLOW_KEYWORDS = _config["title_allow_keywords"]
EXCLUSION_KEYWORDS = _config["exclusion_keywords"]
LOCATION_ALLOW_PATTERNS = _config["location_allow_patterns"]
STACK_DEALBREAKERS = _config["stack_dealbreakers"]
STACK_CORE = _config["stack_core"]
TRUNCATED_DESCRIPTION_MIN_CHARS = _config["truncated_description_min_chars"]
# Optional keys: a filters.yaml without them simply skips that check.
SENIORITY_EXCLUDE_KEYWORDS = _config.get("seniority_exclude_keywords") or []
CLEARANCE_PATTERNS = _config.get("clearance_patterns") or []
REQUIRED_CERT_PATTERNS = _config.get("required_cert_patterns") or []
MAX_REQUIRED_YEARS = _config.get("max_required_years_experience")
REQUIRED_WORDS = _config.get("required_words") or []
PREFERRED_WORDS = _config.get("preferred_words") or []

def _compile_keyword_alternation(keywords: list[str], config_key: str) -> re.Pattern:
    # An empty keyword list joins to "" and compiles to "()", which matches
    # the empty string at every position — .search() would then match ANY
    # title, silently turning an allowlist into "allow everything" and an
    # exclusion list into "exclude everything". Fail loudly instead.
    if not keywords:
        raise ValueError(
            f"filters.yaml's '{config_key}' is empty — this would silently "
            "match every title instead of none. Add at least one keyword."
        )
    return _compile_optional_alternation(keywords)


def _compile_optional_alternation(keywords: list[str]) -> re.Pattern | None:
    # Whole-word/phrase match: lookarounds rather than \b so keywords that
    # start or end with punctuation ("sr.", "c#") still work.
    if not keywords:
        return None
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(k) for k in keywords) + r")(?!\w)", re.IGNORECASE)


_priority_re = _compile_keyword_alternation(PRIORITY_TITLE_KEYWORDS, "priority_title_keywords")
_title_allow_re = _compile_keyword_alternation(TITLE_ALLOW_KEYWORDS, "title_allow_keywords")
_exclusion_re = _compile_keyword_alternation(EXCLUSION_KEYWORDS, "exclusion_keywords")
_location_res = [re.compile(p, re.IGNORECASE) for p in LOCATION_ALLOW_PATTERNS]
_dealbreaker_res = [re.compile(p, re.IGNORECASE) for p in STACK_DEALBREAKERS]
_core_res = [re.compile(p, re.IGNORECASE) for p in STACK_CORE]
_seniority_re = _compile_optional_alternation(SENIORITY_EXCLUDE_KEYWORDS)
_clearance_res = [re.compile(p, re.IGNORECASE) for p in CLEARANCE_PATTERNS]
_cert_res = [re.compile(p, re.IGNORECASE) for p in REQUIRED_CERT_PATTERNS]
_required_re = _compile_optional_alternation(REQUIRED_WORDS)
_preferred_re = _compile_optional_alternation(PREFERRED_WORDS)
# "5+ years", "5 or more years", "at least 5 years", "5-7 years". The
# first number is the minimum being asked for.
_years_re = re.compile(r"(?<![\w.])(\d{1,2})\s*(?:\+|plus|or more|-\s*\d{1,2}|to\s*\d{1,2})?\s*years?\b",
                       re.IGNORECASE)
_block_tag_re = re.compile(r"<\s*/?\s*(?:br|li|p|div|h\d|tr|ul|ol)\b[^>]*>", re.IGNORECASE)
_sentence_split_re = re.compile(r"(?<=[.!?;])\s+|\n+|\s*[•·▪●]\s*")

_html_tag_re = re.compile(r"<[^>]+>")


def strip_html(html_or_text: str) -> str:
    """Cheap HTML-to-text: good enough for keyword matching, not for display."""
    if not html_or_text:
        return ""
    return _html_tag_re.sub(" ", html_or_text)


def looks_truncated(description: str) -> bool:
    """True if `description` looks like a short aggregator teaser rather
    than a full job posting — either it visibly cuts off mid-sentence, or
    it's just too short to contain real requirements/responsibilities.

    Shared by aggregator_clients.py (decide whether it's worth the extra
    request to fetch a full JD) and ai_evaluate.py (fall back to telling
    the model the description is partial, for the cases a full-JD fetch
    still couldn't recover — blocked site, dead link, genuinely short
    posting)."""
    text = strip_html(description or "").strip()
    if not text:
        return True
    if text.endswith("…") or text.endswith("...") or text.rstrip().endswith(".."):
        return True
    return len(text) < TRUNCATED_DESCRIPTION_MIN_CHARS


def title_is_relevant(title: str) -> bool:
    title = title or ""
    if _seniority_re and _seniority_re.search(title):
        # Junior/entry-level search: seniority vetoes even a priority match.
        return False
    if _priority_re.search(title):
        # Unambiguous engineering title — e.g. "Software Engineer, Automated
        # Marketing" — skip the exclusion list entirely so a word like
        # "marketing" elsewhere in the title can't veto it.
        return True
    if not _title_allow_re.search(title):
        return False
    if _exclusion_re.search(title):
        return False
    return True


def location_is_allowed(location: str) -> bool:
    loc = location or ""
    return any(p.search(loc) for p in _location_res)


def jd_stack_mismatch(description: str) -> bool:
    """True if the JD looks like a dealbreaker-language role with no
    mention of your own core stack. Only meaningful when `description` is
    non-empty — callers should treat an empty description as "unknown,
    don't reject on stack alone"."""
    text = strip_html(description)
    if not text:
        return False
    has_dealbreaker = any(p.search(text) for p in _dealbreaker_res)
    has_core = any(p.search(text) for p in _core_res)
    return has_dealbreaker and not has_core


def requires_clearance(title: str, description: str) -> bool:
    """True if the title or JD mentions a security clearance at all — this
    search excludes every cleared role, not just ones needing it on day one."""
    text = (title or "") + "\n" + strip_html(description or "")
    return any(p.search(text) for p in _clearance_res)


def _sentences(description: str) -> list[str]:
    # Block-level tags become line breaks first, so each <li> bullet is its
    # own "sentence" rather than running into the next one.
    text = strip_html(_block_tag_re.sub("\n", description or ""))
    return [s for s in _sentence_split_re.split(text) if s.strip()]


def _is_softened(sentence: str) -> bool:
    return bool(_preferred_re and _preferred_re.search(sentence))


def requires_accessibility_cert(description: str) -> bool:
    """True if some sentence names an accessibility certification as
    required ("CPACC required", "must hold IAAP WAS certification") without
    softening it ("CPACC preferred" passes)."""
    for sentence in _sentences(description):
        if not any(p.search(sentence) for p in _cert_res):
            continue
        if _required_re and _required_re.search(sentence) and not _is_softened(sentence):
            return True
    return False


def requires_too_much_experience(description: str) -> bool:
    """True if some sentence asks for at least MAX_REQUIRED_YEARS years of
    experience without softening it. Only sentences that also mention
    "experience" count, so "founded 10 years ago" isn't a requirement."""
    if not MAX_REQUIRED_YEARS:
        return False
    for sentence in _sentences(description):
        if "experience" not in sentence.lower() or _is_softened(sentence):
            continue
        for m in _years_re.finditer(sentence):
            if int(m.group(1)) >= MAX_REQUIRED_YEARS:
                return True
    return False


def passes_filters(job: dict) -> bool:
    """job must have 'title' and 'location' keys (plain strings); may
    optionally have a 'description' key (HTML or plain text)."""
    title = job.get("title", "")
    description = job.get("description", "")
    if not title_is_relevant(title):
        return False
    if not location_is_allowed(job.get("location", "")):
        return False
    if requires_clearance(title, description):
        return False
    if jd_stack_mismatch(description):
        return False
    if requires_accessibility_cert(description):
        return False
    if requires_too_much_experience(description):
        return False
    return True
