"""
Extractor — Gemini structured extraction from scraped page text.

Design:
  - LLM extracts only explicitly stated fields (temperature 0.1)
  - confidence is computed in calculate_confidence() from source metadata
    (domain trust, depth, field count, version match, page length)
  - CONFIDENCE_THRESHOLD (0.55): below -> return None
  - MODEL_FALLBACK_CHAIN: tries multiple Gemini models on 429 errors
  - Requires GEMINI_API_KEY in environment / .env
"""

import os
import json
import re
from google import genai
from typing import Optional
from datetime import datetime
from urllib.parse import urlparse
from models import ProductLifecycle
from seed_data import get_oem_domains
from lifecycle_terms import get_prompt_snippet
from date_utils import merge_date_fields
from product_matching import products_match
from product_aliases import alias_hint_for_prompt
from result_quality import has_minimum_lifecycle_data
from version_utils import extract_version as extract_version_from_name

_client = None

def _get_client():
    """Lazy-init Gemini client (only when extraction is actually needed)."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY not set. Create a .env file with your key, "
                "or set it as an environment variable."
            )
        _client = genai.Client(api_key=api_key)
    return _client

# --- Configuration ---

CONFIDENCE_THRESHOLD = 0.55   # Minimum confidence to accept a result
RETRY_WAIT_SECONDS = 5        # Fixed wait time on rate limit before retrying
# Model fallback chain: try each in order if previous is rate-limited
# Best capability first, then fall back to lighter models with separate quotas
MODEL_FALLBACK_CHAIN = [
    "models/gemini-3-flash-preview",       # Primary: Gemini 3 Flash (most capable)
    "models/gemini-3.1-flash-lite",        # Fallback 1: Gemini 3.1 Flash Lite
    "models/gemini-2.5-flash",             # Fallback 2: Gemini 2.5 Flash (proven)
    "models/gemini-3.1-flash-live-preview", # Fallback 3: Gemini 3.1 Flash Live
]

# Known licensing models (controlled vocabulary)
KNOWN_LICENSING_MODELS = {
    "per-user", "per-device", "per-core", "per-processor", "per-socket",
    "per-server", "per-instance", "per-node", "per-seat",
    "named user", "concurrent user", "floating license",
    "subscription", "perpetual", "perpetual + subscription",
    "saas", "cloud", "on-premise",
    "open source", "free", "freemium",
    "enterprise license agreement", "ela",
    "volume licensing", "oem", "retail",
    "core-based", "cal-based", "processor-based",
    "client access license", "cal",
    "cpu license", "named user plus",
    "term license", "metered",
}

# Known chargeability types
KNOWN_CHARGEABILITY_TYPES = {
    "free", "paid", "freemium", "open source",
    "subscription", "perpetual", "enterprise",
    "per-use", "metered", "tiered",
}


# --- System + Extraction Prompts ---

SYSTEM_PROMPT = """You are a precise data extractor for software and hardware product 
lifecycle information in IT Asset Management (ITAM) contexts.

Your only job is to extract what is EXPLICITLY stated in the provided content.
You never invent, infer, or estimate calendar dates unless you are computing from 
an explicit release date + an explicit support duration (e.g. "released 2019-03-01" 
+ "5 years of support" = estimated end 2024-03-01 — this computation is allowed).

You understand that:
- Many OEMs do not publish lifecycle dates publicly
- Some lifecycle pages are behind authentication walls
- Some products are perpetual/evergreen with no fixed end date
- Lifecycle terminology varies widely by vendor
- Multiple support tiers (mainstream, extended, security-only) are common
- Hardware and software lifecycle models differ

You return ONLY valid JSON. No markdown, no explanation, no preamble."""

EXTRACTION_PROMPT = """Extract all available lifecycle and commercial data for the 
product below from the provided webpage content.

Product: {product_name}
OEM/Vendor: {oem}
Target version: {target_version_label}
Source URL: {source_url}

{terminology_block}

---
FIELD MAPPING RULES:

1. DATE FIELDS — ISO format YYYY-MM-DD, YYYY-MM, or YYYY only.
   - end_of_support      : mainstream/standard/general support end
   - end_of_extended_support : extended/security-only/premium support end
   - end_of_life         : full retirement, no support of any kind
   - end_of_sale         : last date to purchase (hardware/Cisco-style products)
   - release_date        : GA/general availability/release date if explicitly stated

2. COMPUTED DATES — allowed only when BOTH conditions are explicitly on the page:
   - An explicit release/GA date for this product/version
   - An explicit support duration in years/months
   Set estimated_end_of_support to the computed date and set date_precision to "estimated".
   Do NOT compute if either input is absent or ambiguous.

3. RELATIVE POLICY — if only a relative policy exists with no computable date:
   - Copy the exact policy text into support_policy_relative
   - Leave date fields null
   - Set date_precision to "relative_only"

4. COMMERCIAL FIELDS:
   - licensing_model     : extract if explicitly stated (per-user, per-core, subscription, etc.)
   - chargeability_type  : free / paid / freemium / subscription / perpetual / metered

5. PRODUCT VARIANT (CRITICAL):
   - Similar names are different products. "WebSphere AS" ≠ "WebSphere AS Network Deployment"
   - Only extract dates that explicitly apply to the EXACT product above
   - Set product_variant_found to the exact edition name the dates refer to
   - If only a sibling edition's dates are present, set product_found_in_text to false

6. VERSION MATCHING:
   - Only extract data for the target version
   - If target is "latest (auto-resolved)", use the most recent version on the page
   - Report the version you extracted data for in version_found

7. SPECIAL STATES — set lifecycle_status accordingly:
   - "dates_found"       : at least one explicit calendar date was extracted
   - "policy_only"       : support policy exists but no calendar dates and cannot be computed
   - "licensing_only"    : only commercial/licensing info found, no lifecycle dates or policy
   - "no_dates_published": page is clearly an official source but explicitly states no fixed dates
   - "perpetual"         : product is explicitly described as evergreen or continuously updated
   - "auth_required"     : page content suggests a login wall (very short content, login prompts)
   - "not_found"         : product not mentioned on this page

8. MULTI-TIER SUPPORT — use additional_support_tiers for vendors with more than two tiers:
   Capture as a list of objects: [{{"tier_name": "...", "end_date": "..."}}]
   Example vendors: Cisco (5 tiers), Oracle (Premier/Extended/Sustaining), 
   VMware (General/Technical Guidance/Self-Service), Red Hat (Full/Maintenance 1/Maintenance 2)

9. EXPLICIT ABSENCE — if the page is an official source that clearly states 
   no end date exists or the product is outside standard lifecycle policies, 
   set lifecycle_status to "no_dates_published" and capture any explanation in lifecycle_notes.

Return ONLY this JSON object:
{{
  "end_of_support": "YYYY-MM-DD or null",
  "end_of_extended_support": "YYYY-MM-DD or null", 
  "end_of_life": "YYYY-MM-DD or null",
  "end_of_sale": "YYYY-MM-DD or null",
  "release_date": "YYYY-MM-DD or null",
  "estimated_end_of_support": "YYYY-MM-DD or null",
  "support_policy_relative": "verbatim policy text or null",
  "licensing_model": "string or null",
  "chargeability_type": "string or null",
  "lifecycle_status": "dates_found | policy_only | licensing_only | no_dates_published | perpetual | auth_required | not_found",
  "product_found_in_text": true/false,
  "product_variant_found": "exact edition name or null",
  "version_found": "string or null",
  "date_precision": "exact | estimated | relative_only | null",
  "additional_support_tiers": [{{"tier_name": "string", "end_date": "YYYY-MM-DD"}}] or [],
  "extraction_notes": "what was found, which OEM terms were mapped, what was absent and why"
}}

Webpage content:
{content}"""


# --- Confidence Calculation (Source-Based) ---

def calculate_confidence(
    source_url: str,
    oem: str,
    depth: int,
    extraction: dict,
    page_text_length: int,
    product_name_for_version: str = "",
    target_version: Optional[str] = None,
    version_auto_resolved: bool = False,
) -> float:
    """
    Calculate confidence based on SOURCE METADATA, not LLM self-assessment.

    Factors:
      1. Domain trust -- is the URL on the official OEM domain?
      2. Data completeness -- how many fields were extracted?
      3. Date plausibility -- are dates in a reasonable range?
      4. Product match -- did the LLM find the product in the text?
      5. Version match -- wrong version heavily penalized
      6. Page quality -- is the page substantive enough?

    depth is accepted for API compatibility but not used (deep OEM pages are valid).
    """
    _ = depth  # provenance only; relevance comes from extraction + page content
    score = 1.0

    # 1. Domain trust (biggest factor)
    domains = get_oem_domains(oem)
    if domains:
        parsed = urlparse(source_url)
        hostname = parsed.netloc.lower()
        on_official_domain = any(
            hostname == d or hostname.endswith("." + d) for d in domains
        )
        if not on_official_domain:
            score *= 0.55   # Massive penalty for off-domain
    else:
        score *= 0.80       # Unknown OEM, can't verify domain

    # 2. Data completeness
    fields = [
        "end_of_support",
        "end_of_extended_support",
        "end_of_life",
        "end_of_sale",
        "licensing_model",
        "chargeability_type",
        "estimated_end_of_support",
    ]
    fields_found = sum(1 for f in fields if extraction.get(f) is not None)
    # At least 1 field found -> some signal; 0 fields -> big penalty
    if fields_found == 0:
        score *= 0.1
    elif fields_found == 1:
        score *= 0.75
    else:
        score *= (0.75 + 0.25 * ((fields_found - 1) / (len(fields) - 1)))

    status = extraction.get("lifecycle_status", "")
    if status == "auth_required":
        return 0.0
    if status == "not_found":
        score *= 0.05
    if status == "perpetual":
        score = max(score, 0.70)
    if status == "no_dates_published":
        score = max(score, 0.65)
    if status == "licensing_only":
        score = min(score, 0.50)
    if status == "policy_only":
        score = max(score, 0.52)

    # 3. Date plausibility
    for date_field in ["end_of_support", "end_of_extended_support", "end_of_life"]:
        date_val = extraction.get(date_field)
        if date_val:
            if not _is_plausible_date(date_val):
                score *= 0.3
                break

    # 4. Product found in text
    if not extraction.get("product_found_in_text", False):
        score *= 0.3

    # 4b. Wrong product edition (e.g. WAS ND vs WAS)
    variant_mismatch = False
    variant = extraction.get("product_variant_found")
    if variant and product_name_for_version:
        if not products_match(product_name_for_version, variant):
            score *= 0.05
            variant_mismatch = True

    # 5. Version mismatch
    version_mismatch = False
    version_found = extraction.get("version_found")
    check_ver = target_version
    if not check_ver:
        check_ver = extract_version_from_name(product_name_for_version or "")
    if check_ver and version_found:
        vf = version_found.lstrip("vV")
        cv = check_ver.lstrip("vV")
        if vf != cv:
            score *= 0.05
            version_mismatch = True

    # 6. Relative policy floor — only when edition/version are not wrong
    if (
        not variant_mismatch
        and not version_mismatch
        and extraction.get("support_policy_relative")
        and extraction.get("product_found_in_text")
    ):
        score = max(score, 0.52)
    if extraction.get("release_date"):
        score = min(1.0, score * 1.05)

    field_count = sum(
        1 for k in (
            "end_of_support", "end_of_extended_support", "end_of_life",
            "support_policy_relative", "estimated_end_of_support",
        )
        if extraction.get(k)
    )
    if field_count >= 2:
        score = min(1.0, score + 0.08)
    if field_count >= 3:
        score = min(1.0, score + 0.05)

    precision = extraction.get("date_precision")
    if precision == "estimated":
        score = min(score, 0.78)
    elif precision == "relative_only":
        score = min(score, 0.65)

    # 7. Page quality
    if page_text_length < 200:
        score *= 0.5
    elif page_text_length < 500:
        score *= 0.7

    return round(min(score, 1.0), 3)


def _is_plausible_date(date_str: str) -> bool:
    """Check if a date is in a plausible range (1990-2040)."""
    try:
        match = re.match(r'(\d{4})', date_str)
        if match:
            year = int(match.group(1))
            return 1990 <= year <= 2040
    except (ValueError, AttributeError):
        pass
    return False


def _validate_licensing(value: str) -> Optional[str]:
    """Validate licensing model against known taxonomy."""
    if not value:
        return None
    normalized = value.lower().strip()
    if normalized in KNOWN_LICENSING_MODELS:
        return value
    for known in KNOWN_LICENSING_MODELS:
        if known in normalized or normalized in known:
            return value
    return value


def _validate_chargeability(value: str) -> Optional[str]:
    """Validate chargeability against known types."""
    if not value:
        return None
    normalized = value.lower().strip()
    for known in KNOWN_CHARGEABILITY_TYPES:
        if known in normalized or normalized in known:
            return value
    return value


# Track models that are rate-limited during this session — skip them on future calls
_exhausted_models = set()


def _get_available_models():
    """Return only models that haven't been rate-limited this session."""
    available = [m for m in MODEL_FALLBACK_CHAIN if m not in _exhausted_models]
    if not available:
        # All models exhausted — reset and try again (quotas may have refreshed)
        _exhausted_models.clear()
        return list(MODEL_FALLBACK_CHAIN)
    return available


def _escape_format_literal(text: str) -> str:
    if not text:
        return ""
    return text.replace("{", "{{").replace("}", "}}")


def _build_extraction_prompt(
    product_name: str,
    oem: str,
    source_url: str,
    target_version_label: str,
    terminology_block: str,
    content: str,
) -> str:
    return EXTRACTION_PROMPT.format(
        product_name=_escape_format_literal(product_name),
        oem=_escape_format_literal(oem),
        source_url=_escape_format_literal(source_url),
        target_version_label=_escape_format_literal(target_version_label),
        terminology_block=_escape_format_literal(terminology_block),
        content=_escape_format_literal(content),
    )


# --- Main Extraction Function ---

def extract_from_content(
    content: str,
    product_name: str,
    oem: str,
    source_url: str,
    depth: int = 0,
    target_version: Optional[str] = None,
    version_auto_resolved: bool = False,
    resolved_version: Optional[str] = None,
) -> Optional[ProductLifecycle]:
    """
    Extract structured lifecycle data from raw page text using Gemini.

    Returns ProductLifecycle if confidence >= threshold, None otherwise.
    The LLM only extracts -- confidence is calculated from source metadata.
    """
    if not content or not source_url:
        return None

    try:
        if target_version:
            if version_auto_resolved:
                ver_label = f"{target_version} (latest — auto-resolved)"
            else:
                ver_label = target_version
        else:
            ver_label = "any / not specified"

        term_block = get_prompt_snippet(oem)
        alias_line = alias_hint_for_prompt(product_name, oem)
        if alias_line:
            term_block = f"{term_block}\n{alias_line}" if term_block else alias_line

        prompt = _build_extraction_prompt(
            product_name=product_name,
            oem=oem,
            source_url=source_url,
            target_version_label=ver_label,
            terminology_block=term_block,
            content=content,
        )

        import time
        raw = None
        available_models = _get_available_models()

        for i, model_name in enumerate(available_models):
            try:
                response = _get_client().models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config={
                        "system_instruction": SYSTEM_PROMPT,
                        "temperature": 0.1,
                    },
                )
                raw = response.text.strip()
                break  # Success
            except KeyboardInterrupt:
                raise
            except Exception as api_err:
                err_str = str(api_err)
                remaining = len(available_models) - i - 1
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    # Blacklist this model for future calls
                    _exhausted_models.add(model_name)
                    if remaining > 0:
                        print(f"    [Extractor] {model_name.split('/')[-1]} exhausted -> next model ({remaining} left)")
                    else:
                        print(f"    [Extractor] All models exhausted. Waiting {RETRY_WAIT_SECONDS}s...")
                        time.sleep(RETRY_WAIT_SECONDS)
                        # Reset blacklist and try the first model one more time
                        _exhausted_models.clear()
                        try:
                            response = _get_client().models.generate_content(
                                model=MODEL_FALLBACK_CHAIN[0],
                                contents=prompt,
                                config={
                                    "system_instruction": SYSTEM_PROMPT,
                                    "temperature": 0.1,
                                },
                            )
                            raw = response.text.strip()
                        except Exception:
                            print(f"    [Extractor] Final retry failed. Skipping.")
                else:
                    # Network/SSL/other error — try next model, don't crash
                    if remaining > 0:
                        print(f"    [Extractor] {model_name.split('/')[-1]} error -> next model ({remaining} left)")
                    else:
                        print(f"    [Extractor] All models failed.")

        if raw is None:
            return None

        # Strip markdown fences if model ignores instructions
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data = json.loads(raw)
        data = merge_date_fields(data)

    except json.JSONDecodeError as e:
        print(f"    [Extractor] JSON parse failed: {e}")
        return None
    except Exception as e:
        print(f"    [Extractor] Gemini error: {e}")
        return None

    # Calculate confidence from source metadata (NOT from LLM)
    confidence = calculate_confidence(
        source_url=source_url,
        oem=oem,
        depth=depth,
        extraction=data,
        page_text_length=len(content),
        product_name_for_version=product_name,
        target_version=target_version or resolved_version,
        version_auto_resolved=version_auto_resolved,
    )

    ver = resolved_version or data.get("version_found") or target_version

    candidate = ProductLifecycle(
        product_name=product_name,
        oem=oem,
        end_of_support=data.get("end_of_support"),
        end_of_extended_support=data.get("end_of_extended_support"),
        end_of_life=data.get("end_of_life"),
        licensing_model=_validate_licensing(data.get("licensing_model")),
        chargeability_type=_validate_chargeability(data.get("chargeability_type")),
        source_url=source_url,
        confidence=confidence,
        data_source="oem_crawl",
        last_verified=datetime.now().isoformat(),
        resolved_version=ver,
        version_auto_resolved=version_auto_resolved,
        release_date=data.get("release_date"),
        support_policy_relative=data.get("support_policy_relative"),
        estimated_end_of_support=data.get("estimated_end_of_support"),
        date_precision=data.get("date_precision"),
        lifecycle_notes=data.get("extraction_notes"),
    )

    if confidence < CONFIDENCE_THRESHOLD:
        if has_minimum_lifecycle_data(candidate):
            print(f"    [Extractor] Confidence {confidence:.3f} below {CONFIDENCE_THRESHOLD} "
                  f"— returning for partial review")
            return candidate
        notes = data.get("extraction_notes", "no notes")
        print(f"    [Extractor] Confidence {confidence:.3f} below threshold "
              f"({CONFIDENCE_THRESHOLD}) — rejecting. Notes: {notes}")
        return None

    return candidate
