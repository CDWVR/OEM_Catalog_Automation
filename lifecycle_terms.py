"""
OEM lifecycle terminology — injected into extraction prompts.

Different vendors use different labels for the same concepts. The extractor
is instructed to map these to our canonical fields without inventing dates.
"""

# Canonical field -> phrases commonly used on OEM sites
LIFECYCLE_SYNONYMS = {
    "end_of_support": [
        "end of support", "eos", "end of standard support", "end of mainstream support",
        "end of maintenance", "end of technical support", "ends support",
        "support ends", "no longer supported", "support retirement",
        "end of premier support", "end of full support", "general availability end",
    ],
    "end_of_extended_support": [
        "end of extended support", "end of extended maintenance",
        "extended support ends", "end of security updates", "end of bug fixes",
        "sustaining support", "extended support period",
    ],
    "end_of_life": [
        "end of life", "eol", "end of sale", "end of availability",
        "product discontinuation", "retired", "obsolete", "end of service life",
        "eosl", "end-of-life", "no longer available",
    ],
    "release_date": [
        "release date", "general availability", "ga date", "launched",
        "initial release", "first shipped", "availability date", "rtm",
        "release availability",
    ],
    "support_policy_relative": [
        "years of support", "years from release", "months of support",
        "support period", "support lifecycle", "supported for",
        "maintenance period", "warranty period", "years mainstream support",
    ],
}

OEM_HINTS = {
    "microsoft": "Uses 'Mainstream Support' / 'Extended Support' end dates on lifecycle pages.",
    "oracle": "Uses Premier / Extended / Sustaining support phases; often policy PDFs.",
    "ibm": "May use EOS, EOSL, or 'withdrawal from marketing'.",
    "red hat": "Uses full support / maintenance support / EOL for RHEL minor releases.",
    "bmc": "Product-specific support matrices; Control-M uses announcement pages.",
    "cisco": "EOS and EOL dates on the EOS/EOL bulletin listing.",
    "vmware": "General Support End, Technical Guidance End on lifecycle.vmware.com.",
    "autodesk": "Previous versions list with support eligibility windows.",
}


def get_prompt_snippet(oem: str) -> str:
    """Build OEM-aware terminology block for the extraction prompt."""
    lines = [
        "OEM TERMINOLOGY (map explicit statements to our JSON fields):",
        "- end_of_support: " + ", ".join(LIFECYCLE_SYNONYMS["end_of_support"][:8]) + ", ...",
        "- end_of_extended_support: " + ", ".join(LIFECYCLE_SYNONYMS["end_of_extended_support"][:6]) + ", ...",
        "- end_of_life: " + ", ".join(LIFECYCLE_SYNONYMS["end_of_life"][:8]) + ", ...",
        "- release_date: " + ", ".join(LIFECYCLE_SYNONYMS["release_date"][:6]) + ", ...",
        "- support_policy_relative: e.g. '5 years from release', '10 years mainstream support'",
    ]
    normalized = oem.lower().strip()
    for key, hint in OEM_HINTS.items():
        if key in normalized or normalized in key:
            lines.append(f"- {oem} note: {hint}")
            break
    return "\n".join(lines)
