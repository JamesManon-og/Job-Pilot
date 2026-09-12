"""Job identity across runs and platforms.

Three layers:
- canonical_url: the posting URL with tracking noise removed, so the same
  LinkedIn job reached via two search pages is one row.
- identity key: (platform, external_id) when the board gives us an id,
  otherwise the canonical URL. This is what dedup_hash hashes.
- fingerprint: normalized company + title. Two rows with the same
  fingerprint are the same job listed on different boards (or reposted);
  only the first is processed.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never identify a posting.
_TRACKING_PARAMS = frozenset(
    {
        "ref",
        "refid",
        "trackingid",
        "trk",
        "trkinfo",
        "lipi",
        "eborigin",
        "originalsubdomain",
        "src",
        "source",
        "from",
        "sid",
        "sessionid",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "pagenum",
        "searchrequestid",
        "tk",
    }
)


def canonicalize_url(url: str) -> str:
    """Stable form of a posting URL: lowercase host, no fragment, no tracking params."""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), "")
    )


_COMPANY_SUFFIXES = re.compile(
    r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|pte|pty|plc|"
    r"gmbh|s\.a|sa|bv|ag|opc|philippines|ph)\b\.?",
)
# Words that decorate a title without changing the job.
_TITLE_NOISE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|wfh|work from home|urgent(ly)?|hiring|now hiring|"
    r"immediate(ly)?|start asap|asap|full[- ]?time|part[- ]?time|contract|freelance|"
    r"permanent|temporary|we'?re hiring)\b"
)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", text)).strip()


def normalize_company(company: str) -> str:
    text = company.casefold()
    text = _COMPANY_SUFFIXES.sub(" ", text)
    return _squash(text)


_TITLE_SYNONYMS = {"sr": "senior", "snr": "senior", "jr": "junior", "jnr": "junior"}


def normalize_title(title: str) -> str:
    """Drop decoration ("(Remote)", "URGENT", "Full-time"), keep what distinguishes roles.

    Separators and brackets are removed but their words kept: "Engineer -
    Backend" and "Engineer (Frontend)" must stay different jobs.
    """
    text = _TITLE_NOISE.sub(" ", title.casefold())
    words = _squash(text).split()
    return " ".join(_TITLE_SYNONYMS.get(word, word) for word in words)


def job_fingerprint(company: str, title: str) -> str:
    """Same company + same normalized title = the same job, whichever board lists it."""
    key = f"{normalize_company(company)}|{normalize_title(title)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def identity_key(source: str, external_id: str | None, canonical_url: str) -> str:
    if external_id:
        return f"{source}:id:{external_id.strip()}"
    return f"url:{canonical_url}"


def identity_hash(source: str, external_id: str | None, canonical_url: str) -> str:
    key = identity_key(source, external_id, canonical_url)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
