"""Playwright-based form autofill engine.

Reads every form control in one DOM pass (label, aria-label, placeholder,
name/id, autocomplete, required, options), classifies each one, and fills
only what it can identify with confidence.

Safety invariants — each is covered by a test:
- NEVER clicks submit, "Next", or any other button. It types into fields,
  picks options, and attaches the resume; the human submits.
- Never guesses: unknown fields are reported, not filled. Sensitive fields
  (passwords, government IDs, DOB, gender, salary history, ...) are never
  touched, even if an answer seems obvious.
- Never interacts with login forms, CAPTCHAs, MFA prompts, or bot challenges.
  A page-level blocker stops the fill; a CAPTCHA widget is reported for the
  human to complete.
- Never overwrites a value that is already present (e.g. platform-prefilled).
- Never ticks checkboxes (consents, terms, certifications).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from jobpilot.config.preferences import ApplicantProfile

logger = logging.getLogger(__name__)


class AutofillError(Exception):
    """The page couldn't be opened or processed (navigation failure, HTTP error, crash)."""


# ---------------------------------------------------------------------------
# DOM snapshot
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = (el) => el ? clean(el.innerText || el.textContent) : '';
  const labelFor = (el) => {
    const parts = [];
    if (el.labels) for (const l of el.labels) parts.push(textOf(l));
    const ids = el.getAttribute('aria-labelledby');
    if (ids) for (const id of ids.split(/\s+/)) parts.push(textOf(document.getElementById(id)));
    return clean(parts.join(' '));
  };
  const controls = (root) =>
    root.querySelectorAll('input, textarea, select, [contenteditable="true"]');
  const nearbyText = (el) => {
    // ATS markup often puts a bare <label>/<div> beside the input with no for=.
    let node = el.parentElement;
    for (let depth = 0; node && depth < 3; depth++, node = node.parentElement) {
      if (controls(node).length !== 1) return '';
      const lbl = node.querySelector('label, legend, [class*="label" i], [class*="question" i]');
      if (lbl && !lbl.contains(el)) return textOf(lbl).slice(0, 300);
    }
    return '';
  };
  const groupLabel = (el) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return textOf(lg); }
    const grp = el.closest('[role="radiogroup"], [role="group"]');
    if (grp) {
      const aria = grp.getAttribute('aria-label');
      if (aria) return clean(aria);
      const by = grp.getAttribute('aria-labelledby');
      if (by) return textOf(document.getElementById(by));
    }
    return '';
  };
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const out = [];
  let idx = 0;
  for (const el of controls(document)) {
    const tag = el.tagName.toLowerCase();
    const editable = el.getAttribute('contenteditable') === 'true';
    const type = editable ? 'contenteditable'
      : (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;
    el.setAttribute('data-jobpilot-idx', String(idx));
    let empty;
    if (type === 'radio' || type === 'checkbox') empty = !el.checked;
    else if (type === 'file') empty = !(el.files && el.files.length);
    else if (editable) empty = !clean(el.innerText);
    else if (tag === 'select') empty = !el.value;
    else empty = !el.value;
    const label = labelFor(el);
    out.push({
      idx: idx++, tag, type,
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      aria: el.getAttribute('aria-label') || '',
      label,
      nearby: label ? '' : nearbyText(el),
      group: (type === 'radio' || type === 'checkbox') ? groupLabel(el) : '',
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      // file inputs are commonly hidden behind a styled button but still usable
      visible: type === 'file' ? true : isVisible(el),
      empty,
      value: (type === 'radio' || type === 'checkbox') ? (el.value || '') : '',
      accept: (el.getAttribute('accept') || '').toLowerCase(),
      options: tag === 'select'
        ? Array.from(el.options).map((o) => ({ text: clean(o.text), value: o.value }))
        : [],
    });
  }
  return out;
}
"""

_BLOCKER_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const text = (document.body ? document.body.innerText : '').slice(0, 5000).toLowerCase();
  const frames = Array.from(document.querySelectorAll('iframe')).map((f) => ({
    src: (f.getAttribute('src') || '').toLowerCase(),
    title: (f.getAttribute('title') || '').toLowerCase(),
    visible: visible(f),
  }));
  const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
  const otp = inputs.some((i) => {
    const sig = [i.getAttribute('autocomplete'), i.name, i.id, i.getAttribute('placeholder'),
                 i.getAttribute('aria-label')].join(' ').toLowerCase();
    return /one-time-code|\botp\b|verification.?code|security.?code|2fa|two.?factor|authenticator/
      .test(sig);
  });
  const fillable = inputs.filter((i) =>
    !['hidden', 'submit', 'button', 'reset', 'image', 'checkbox', 'radio']
      .includes((i.getAttribute('type') || 'text').toLowerCase()));
  return {
    title: (document.title || '').toLowerCase(),
    text,
    frames,
    password: inputs.some((i) => (i.getAttribute('type') || '').toLowerCase() === 'password'),
    otp,
    // Challenge/MFA pages have one or two inputs; application forms have many.
    // Text-only signals are trusted only on sparse pages, so a job description
    // that mentions "two-factor" can't masquerade as an MFA wall.
    inputCount: fillable.length + document.querySelectorAll('textarea, select').length,
  };
}
"""

_CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "security check")
_CHALLENGE_TEXT = (
    "verify you are human",
    "verify that you are human",
    "checking your browser",
    "checking if the site connection is secure",
    "press & hold",
    "press and hold",
    "are you a robot",
    "unusual traffic",
    "complete the security check",
)
_MFA_TEXT = (
    "two-factor",
    "two factor",
    "2-step verification",
    "verification code",
    "enter the code",
    "one-time code",
    "authenticator app",
)


async def detect_blocker(page: Page) -> str | None:
    """Return 'challenge', 'mfa', or 'login' if the page is a wall, not a form."""
    info = await page.evaluate(_BLOCKER_JS)
    sparse = info["inputCount"] <= 2
    if any(info["title"].startswith(t) for t in _CHALLENGE_TITLES):
        return "challenge"
    if sparse and any(t in info["text"] for t in _CHALLENGE_TEXT):
        return "challenge"
    if sparse and any(
        "challenges.cloudflare.com" in f["src"] and f["visible"] for f in info["frames"]
    ):
        return "challenge"
    if info["otp"] or (sparse and any(t in info["text"] for t in _MFA_TEXT)):
        return "mfa"
    if info["password"]:
        return "login"
    return None


async def has_captcha_widget(page: Page) -> bool:
    """A visible CAPTCHA the human must complete (invisible reCAPTCHA v3 doesn't count)."""
    info = await page.evaluate(_BLOCKER_JS)
    for frame in info["frames"]:
        if not frame["visible"]:
            continue
        src = frame["src"]
        if "hcaptcha.com" in src or "challenges.cloudflare.com" in src:
            return True
        if "recaptcha" in src and "size=invisible" not in src:
            return True
    return False


# ---------------------------------------------------------------------------
# Field classification
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("_", " ")).strip()


def _split_camel(text: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


def _has(text: str, *phrases: str) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", text) for p in phrases)


_SENSITIVE_PHRASES = (
    "password",
    "passcode",
    "pin",
    "ssn",
    "social security",
    "social insurance",
    "sss",
    "tin",
    "tax id",
    "tax identification",
    "national id",
    "passport",
    "driver s license",
    "drivers license",
    "driving licence",
    "date of birth",
    "birth date",
    "birthdate",
    "birthday",
    "dob",
    "age",
    "gender",
    "sex",
    "race",
    "ethnicity",
    "ethnic",
    "veteran",
    "disability",
    "disabled",
    "religion",
    "marital",
    "bank",
    "account number",
    "routing",
    "iban",
    "swift",
    "credit card",
    "card number",
    "cvv",
    "cvc",
    "expiry",
    "current salary",
    "salary history",
    "previous salary",
    "last salary",
    "current compensation",
    "signature",
    "captcha",
    "security question",
    "maiden",
    "otp",
    "verification code",
    "one time",
)
_SENSITIVE_AUTOCOMPLETE = (
    "bday",
    "sex",
    "cc-",
    "current-password",
    "new-password",
    "one-time-code",
)

# "name" alone is ambiguous: these qualifiers mean it's someone/something else's name.
_NAME_EXCLUSIONS = (
    "company",
    "employer",
    "organization",
    "organisation",
    "business",
    "school",
    "university",
    "college",
    "institution",
    "reference",
    "referee",
    "referrer",
    "referral",
    "recruiter",
    "manager",
    "supervisor",
    "emergency",
    "contact person",
    "user",
    "username",
    "login",
    "file",
    "account",
    "project",
    "spouse",
    "parent",
    "guardian",
    "nick",
    "preferred",
    "middle",
    "job",
    "position",
    "role",
)
_OTHER_PERSON = ("reference", "referee", "referrer", "manager", "supervisor", "emergency")


@dataclass(frozen=True)
class _Field:
    idx: int
    tag: str
    type: str
    name: str
    id: str
    placeholder: str
    autocomplete: str
    aria: str
    label: str
    nearby: str
    group: str
    required: bool
    visible: bool
    empty: bool
    value: str
    accept: str
    options: tuple[tuple[str, str], ...]

    @classmethod
    def from_js(cls, raw: dict[str, Any]) -> _Field:
        return cls(
            **{k: raw[k] for k in raw if k != "options"},
            options=tuple((o["text"], o["value"]) for o in raw["options"]),
        )

    @property
    def question(self) -> str:
        """Human-readable question text, best source first."""
        for text in (self.group, self.label, self.aria, self.nearby, self.placeholder):
            if text.strip():
                return text.strip().rstrip("*").strip()
        return _split_camel(self.name or self.id).replace("_", " ").strip()

    @property
    def primary(self) -> str:
        """Normalized human-facing text (label, aria-label, placeholder)."""
        return _norm(" ".join((self.label, self.aria, self.nearby, self.placeholder)))

    @property
    def signals(self) -> str:
        """Everything, including machine names — for broad keyword checks."""
        return _norm(
            " ".join(
                (
                    self.label,
                    self.aria,
                    self.nearby,
                    self.placeholder,
                    _split_camel(self.name),
                    _split_camel(self.id),
                )
            )
        )


def _is_sensitive(f: _Field) -> bool:
    if any(f.autocomplete.startswith(token) for token in _SENSITIVE_AUTOCOMPLETE):
        return True
    if f.type == "password":
        return True
    return _has(f.signals, *_SENSITIVE_PHRASES) or _has(_norm(f.group), *_SENSITIVE_PHRASES)


def _classify_name(f: _Field) -> str | None:
    auto = f.autocomplete
    if auto == "given-name":
        return "first_name"
    if auto == "family-name":
        return "last_name"
    if auto == "name":
        return "name"
    if auto in ("organization", "username", "additional-name", "nickname"):
        return None
    text = f.primary or _norm(_split_camel(f.name or f.id))
    if _has(text, *_NAME_EXCLUSIONS):
        return None
    if _has(text, "first name", "given name", "firstname", "fname", "forename"):
        return "first_name"
    if _has(text, "last name", "surname", "family name", "lastname", "lname"):
        return "last_name"
    if _has(text, "full name", "your name", "legal name", "fullname", "applicant name"):
        return "name"
    if _has(text, "candidate name") or text in ("name", "name required"):
        return "name"
    machine = _norm(_split_camel(f.name or f.id))
    if not f.primary and machine in ("name", "full name", "fullname", "applicant name"):
        return "name"
    return None


_YEARS_GENERIC = re.compile(
    r"^(how many )?(total )?years? (of )?(total |professional |relevant |work |industry )?"
    r"experience( do you have)?( required)?$"
)


def _classify(f: _Field) -> str | None:
    """Map a field to an ApplicantProfile key, 'cover_letter', or None (unknown)."""
    text = f.primary or _norm(_split_camel(f.name or f.id))
    signals = f.signals
    if f.autocomplete == "email" or f.type == "email" or _has(signals, "email", "e mail"):
        return None if _has(text, *_OTHER_PERSON) else "email"
    if (
        f.autocomplete.startswith("tel")
        or f.type == "tel"
        or _has(signals, "phone", "mobile", "telephone", "cell", "contact number")
    ):
        if _has(text, *_OTHER_PERSON, "type", "extension", "ext"):
            return None
        return "phone"
    name_field = _classify_name(f)
    if name_field:
        return name_field
    if _has(signals, "linkedin"):
        return "linkedin_url"
    if _has(signals, "github"):
        return "github_url"
    if _has(
        text, "portfolio", "personal website", "personal site", "website", "portfolio url"
    ) and (not _has(text, "company", "employer")):
        return "portfolio_url"
    if _has(text, "cover letter", "coverletter", "motivation letter", "letter of motivation"):
        return "cover_letter"
    if not f.primary and _has(signals, "cover letter", "coverletter"):
        return "cover_letter"
    if _has(
        text,
        "authorized to work",
        "authorised to work",
        "work authorization",
        "work authorisation",
        "legally authorized",
        "legally authorised",
        "eligible to work",
        "right to work",
    ):
        return "work_authorization"
    if _has(text, "sponsorship", "sponsor", "visa"):
        return "requires_sponsorship"
    if _has(
        text,
        "expected salary",
        "salary expectation",
        "salary expectations",
        "desired salary",
        "desired compensation",
        "expected compensation",
        "salary requirement",
        "salary requirements",
        "expected pay",
        "asking salary",
        "expected rate",
    ):
        return "salary_expectation"
    if _has(
        text,
        "start date",
        "earliest start",
        "when can you start",
        "available to start",
        "availability",
        "notice period",
    ):
        return "available_start_date"
    if _YEARS_GENERIC.match(text):
        return "years_experience"
    if _has(text, "city") and not _has(text, "citizenship"):
        return "city"
    if _has(text, "country") and not _has(text, "citizenship", "citizen"):
        return "country"
    if _has(text, "current location", "location", "where are you based", "based in"):
        return "location"
    if _has(text, "street", "address line", "home address", "mailing address") or text in (
        "address",
        "address required",
    ):
        return "address"
    return None


def _profile_value(applicant: ApplicantProfile, key: str) -> str:
    return str(getattr(applicant, key, "") or "").strip()


def _numeric(value: str) -> str | None:
    digits = re.sub(r"[^\d.]", "", value.replace(",", ""))
    return digits if re.fullmatch(r"\d+(\.\d+)?", digits) else None


def _match_option(options: tuple[tuple[str, str], ...], wanted: str) -> str | None:
    """Pick the option whose text matches `wanted`; None unless unambiguous."""
    target = _norm(wanted)
    if not target:
        return None
    exact = [value for text, value in options if _norm(text) == target]
    if len(exact) == 1:
        return exact[0]
    prefix = [value for text, value in options if _norm(text).startswith(target + " ")]
    if len(prefix) == 1 and len(target) >= 2:
        return prefix[0]
    return None


def _question_matches(question: str, candidate: str) -> bool:
    q, c = _norm(question), _norm(candidate)
    if not q or not c:
        return False
    if q == c:
        return True
    shorter, longer = sorted((q, c), key=len)
    return len(shorter) >= 12 and shorter in longer


# ---------------------------------------------------------------------------
# Result + engine
# ---------------------------------------------------------------------------


@dataclass
class FilledForm:
    """What an autofill pass did. Holds values for the user's summary; never logged."""

    url: str
    fields_filled: dict[str, str] = field(default_factory=dict)
    fields_skipped: list[str] = field(default_factory=list)  # couldn't identify: ask the human
    sensitive_skipped: list[str] = field(default_factory=list)  # deliberately left alone
    required_unfilled: list[str] = field(default_factory=list)
    resume_uploaded: bool = False
    resume_problem: str | None = None
    cover_letter_filled: bool = False
    captcha_present: bool = False
    blocker: str | None = None  # page is a login/MFA/bot wall: nothing was filled
    screenshot_path: str | None = None

    def summary(self) -> dict[str, object]:
        """PII-free summary for the audit log."""
        return {
            "url": self.url,
            "filled": sorted(self.fields_filled),
            "unknown": self.fields_skipped,
            "sensitive": self.sensitive_skipped,
            "required_unfilled": self.required_unfilled,
            "resume_uploaded": self.resume_uploaded,
            "captcha_present": self.captcha_present,
            "blocker": self.blocker,
        }


class AutofillEngine:
    """Fill a job application form via Playwright.

    ``fill(url)`` launches its own browser; ``open(page, url)`` + ``fill_page(page)``
    work on a page the caller controls (e.g. a visible browser for the human).
    """

    def __init__(
        self,
        applicant: ApplicantProfile,
        screenshots_dir: Path | None = None,
        *,
        headless: bool = True,
        resume_file: str | None = None,
    ) -> None:
        self._applicant = applicant
        self._screenshots_dir = screenshots_dir
        self._headless = headless
        # applicant.resume_file wins; otherwise fall back to the active resume.
        self._resume_file = applicant.resume_file.strip() or (resume_file or "")

    async def fill(
        self,
        url: str,
        *,
        cover_letter: str = "",
        extra_answers: dict[str, str] | None = None,
        screenshot_name: str | None = None,
    ) -> FilledForm:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless)
            try:
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900}, locale="en-US"
                )
                page = await context.new_page()
                await self.open(page, url)
                return await self.fill_page(
                    page,
                    cover_letter=cover_letter,
                    extra_answers=extra_answers,
                    screenshot_name=screenshot_name,
                )
            finally:
                await browser.close()

    async def open(self, page: Page, url: str, *, timeout_ms: int = 30_000) -> None:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise AutofillError(f"Timed out after {timeout_ms // 1000}s opening {url}") from exc
        except PlaywrightError as exc:
            reason = str(exc).splitlines()[0]
            raise AutofillError(f"Could not open {url}: {reason}") from exc
        if response is not None and response.status >= 400:
            raise AutofillError(
                f"{url} returned HTTP {response.status} — the posting may have been removed"
            )
        # Busy pages (analytics, chat widgets) never go idle; the DOM is ready anyway.
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=5_000)

    async def fill_page(
        self,
        page: Page,
        *,
        cover_letter: str = "",
        extra_answers: dict[str, str] | None = None,
        screenshot_name: str | None = None,
    ) -> FilledForm:
        result = FilledForm(url=page.url)
        try:
            result.blocker = await detect_blocker(page)
            if result.blocker is None:
                result.captcha_present = await has_captcha_widget(page)
                answers = {q: a for q, a in (extra_answers or {}).items() if a.strip()}
                # Second pass: fields revealed by the first (e.g. "If yes, explain").
                for _ in range(2):
                    await self._fill_pass(page, result, cover_letter, answers)
                    await page.wait_for_timeout(300)
                await self._upload_resume(page, result)
                result.required_unfilled = await self._required_unfilled(page)
            else:
                logger.warning("Autofill stopped: %s page at %s", result.blocker, page.url)
            await self._screenshot(page, result, screenshot_name)
        except PlaywrightError as exc:
            raise AutofillError(f"Browser error while filling {page.url}: {exc}") from exc
        logger.info(
            "Autofill: %d filled, %d unknown, %d sensitive left, %d required unfilled",
            len(result.fields_filled),
            len(result.fields_skipped),
            len(result.sensitive_skipped),
            len(result.required_unfilled),
        )
        return result

    async def _snapshot(self, page: Page) -> list[_Field]:
        return [_Field.from_js(raw) for raw in await page.evaluate(_SNAPSHOT_JS)]

    def _value_for(self, key: str, cover_letter: str) -> str:
        if key == "cover_letter":
            return cover_letter.strip()
        if key == "first_name":
            return self._applicant.first_name
        if key == "last_name":
            return self._applicant.last_name
        if key == "location":
            return self._applicant.location
        return _profile_value(self._applicant, key)

    async def _fill_pass(
        self, page: Page, result: FilledForm, cover_letter: str, answers: dict[str, str]
    ) -> None:
        fields = await self._snapshot(page)
        radio_groups: dict[str, list[_Field]] = {}
        for f in fields:
            if f.type == "radio":
                radio_groups.setdefault(f.name or f"#{f.idx}", []).append(f)
                continue
            if f.type in ("checkbox", "file") or not f.visible:
                continue
            question = f.question
            if _is_sensitive(f):
                _note(result.sensitive_skipped, question)
                continue
            if not f.empty:
                continue  # never overwrite existing values
            answer = next((a for q, a in answers.items() if _question_matches(q, question)), None)
            key = None if answer is not None else _classify(f)
            value = (
                answer
                if answer is not None
                else (self._value_for(key, cover_letter) if key else "")
            )
            if not value:
                if key is None and answer is None:
                    _note(result.fields_skipped, question)
                continue
            if await self._set_value(page, f, value):
                label = f"q:{question[:40]}" if answer is not None else str(key)
                result.fields_filled[label] = value
                if key == "cover_letter":
                    result.cover_letter_filled = True
            elif key is not None or answer is not None:
                _note(result.fields_skipped, question)

        for members in radio_groups.values():
            await self._fill_radio_group(page, members, result, answers)

    async def _set_value(self, page: Page, f: _Field, value: str) -> bool:
        locator = page.locator(f'[data-jobpilot-idx="{f.idx}"]')
        try:
            if f.tag == "select":
                option = _match_option(f.options, value)
                if option is None:
                    return False
                await locator.select_option(value=option)
                return True
            if f.type == "number":
                number = _numeric(value)
                if number is None:
                    return False
                value = number
            await locator.fill(value)
            return True
        except PlaywrightError as exc:
            logger.warning("Could not fill %r: %s", f.question, str(exc).splitlines()[0])
            return False

    async def _fill_radio_group(
        self, page: Page, members: list[_Field], result: FilledForm, answers: dict[str, str]
    ) -> None:
        first = members[0]
        question = first.group or first.nearby or first.question
        probe = _Field(**{**first.__dict__, "label": question, "group": question})
        if _is_sensitive(probe):
            _note(result.sensitive_skipped, question)
            return
        if any(not m.empty for m in members):
            return  # already answered
        answer = next((a for q, a in answers.items() if _question_matches(q, question)), None)
        key = None if answer is not None else _classify(probe)
        wanted = answer if answer is not None else (self._value_for(key, "") if key else "")
        if not wanted:
            _note(result.fields_skipped, question)
            return
        options = tuple((m.label or m.value, str(m.idx)) for m in members)
        chosen = _match_option(options, wanted)
        if chosen is None:
            _note(result.fields_skipped, question)
            return
        try:
            await page.locator(f'[data-jobpilot-idx="{chosen}"]').check()
        except PlaywrightError as exc:
            logger.warning("Could not select %r: %s", question, str(exc).splitlines()[0])
            _note(result.fields_skipped, question)
            return
        result.fields_filled[f"q:{question[:40]}" if answer is not None else str(key)] = wanted

    async def _upload_resume(self, page: Page, result: FilledForm) -> None:
        files = [f for f in await self._snapshot(page) if f.type == "file"]
        if not files:
            return
        path = Path(self._resume_file) if self._resume_file else None
        if path is None or not path.is_file():
            result.resume_problem = (
                f"Resume file not found: {path}" if path else "No resume file configured"
            )
            return

        def is_cover(f: _Field) -> bool:
            return _has(f.signals, "cover letter", "coverletter", "cover")

        candidates = [
            f for f in files if _has(f.signals, "resume", "résumé", "cv", "curriculum vitae")
        ]
        candidates = [f for f in candidates if not is_cover(f)]
        if not candidates and len(files) == 1 and not is_cover(files[0]):
            candidates = files  # a lone unlabeled upload is the resume
        for f in candidates[:1]:
            if not f.empty:
                return
            accept = f.accept
            if accept and not any(t in accept for t in (".pdf", "pdf", "*")):
                result.resume_problem = f"Upload only accepts {accept}; resume is {path.suffix}"
                return
            try:
                await page.locator(f'[data-jobpilot-idx="{f.idx}"]').set_input_files(str(path))
            except PlaywrightError as exc:
                result.resume_problem = f"Upload failed: {str(exc).splitlines()[0]}"
                return
            result.resume_uploaded = True
            result.fields_filled["resume_upload"] = path.name

    async def _required_unfilled(self, page: Page) -> list[str]:
        missing: list[str] = []
        groups_done: set[str] = set()
        fields = await self._snapshot(page)
        by_group: dict[str, list[_Field]] = {}
        for f in fields:
            if f.type in ("radio", "checkbox") and f.name:
                by_group.setdefault(f.name, []).append(f)
        for f in fields:
            required = f.required or f.label.rstrip().endswith("*")
            if not required or not f.visible:
                continue
            if f.type in ("radio", "checkbox") and f.name:
                if f.name in groups_done:
                    continue
                groups_done.add(f.name)
                if all(m.empty for m in by_group[f.name]):
                    _note(missing, f.group or f.question)
            elif f.empty:
                _note(missing, f.question)
        return missing

    async def _screenshot(self, page: Page, result: FilledForm, name: str | None) -> None:
        if not self._screenshots_dir:
            return
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        if name is None:
            # Unique per page + time: URL tails like ".../apply" collide across jobs.
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            digest = hashlib.sha1(page.url.encode()).hexdigest()[:8]
            name = f"autofill_{stamp}_{digest}.png"
        path = self._screenshots_dir / name
        await page.screenshot(path=str(path), full_page=True)
        result.screenshot_path = str(path)


def _note(bucket: list[str], item: str) -> None:
    item = item.strip()
    if item and item not in bucket:
        bucket.append(item)
