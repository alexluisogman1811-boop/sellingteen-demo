"""Optional "Create Job Summary" feature.

The AI only ever returns text. It receives the owner's notes (and the job title for
context) and nothing else: no price, no payment state, no customer contact details.
Its output is checked by `find_invented_details` and shown to the owner as an editable
draft; it reaches the customer only after the owner saves it.

AI is OFF by default: AI_MOCK=1 (the default) uses a deterministic local formatter, so no API
key or credits are needed. The real API is used only when AI_MOCK=0 AND AI_API_KEY is set.
Default model: claude-haiku-5-5 (low cost).
"""
import logging
import re
from dataclasses import dataclass, field

from flask import current_app

log = logging.getLogger(__name__)

INSUFFICIENT = "INSUFFICIENT_INFORMATION"
MAX_NOTES_CHARS = 4000

SYSTEM_PROMPT = """You turn a tradesperson's rough job notes into a short, professional \
summary of completed work that their customer will read.

Rules you must follow:
- Use only facts stated in the notes. Do not add work, materials, parts, brands, \
quantities, prices, costs, dates, times, guarantees, warranties, certifications or \
recommendations that the notes do not state.
- Only describe work as completed if the notes say it was done.
- If something is unclear, leave it out rather than guess.
- Write 1 to 3 plain sentences in the past tense, without headings, bullet points, \
greetings or sign-offs.
- If the notes do not describe any completed work, reply with exactly: """ + INSUFFICIENT


class AIError(Exception):
    """Raised for any AI failure. The message is safe to show to the owner."""


UNAVAILABLE = ("AI summary is temporarily unavailable. "
               "You can continue by entering the job summary manually.")
NOT_SET_UP = ("AI summaries aren't set up right now. "
              "You can continue by entering the job summary manually.")
UNCLEAR = ("Your notes don't clearly say what work was completed, so no summary was made. "
           "Add what you did, for example \"replaced 3 outlets, tested all connections\", "
           "or enter the summary manually.")

# Past-tense verbs that mark a clause as completed work. Anything else is left out rather
# than presented to the customer as done.
DONE_VERBS = set("""
added adjusted assembled bled bonded built capped caulked changed checked cleaned cleared
completed configured connected cut diagnosed disassembled disconnected drained dug fed filled
finished fitted fixed flushed glued grounded hooked hung inspected installed insulated jetted
labeled labelled laid located lubricated measured mounted moved painted patched programmed
pulled pumped put ran reattached rebuilt recharged reconnected refilled relocated removed
repaired replaced rerouted reset restored rewired routed sealed secured serviced set snaked
soldered spliced stripped supplied swapped terminated tested tightened traced unblocked
unclogged updated upgraded vacuumed verified wired
""".split())
# Words that signal planned or suggested work, which must never read as completed.
PLANNING_WORDS = set("""
need needs needed should will would recommend recommended recommends suggest suggested
todo quote quoted plan planned schedule scheduled may might later next tomorrow pending
""".split()) | {"to do", "not yet", "still"}


class SummaryProvider:
    def summarize(self, notes: str, job_title: str = "") -> str:
        raise NotImplementedError


class MockProvider(SummaryProvider):
    """Rephrases notes without adding anything.

    Keeps only clauses that clearly describe completed work. Anything else (planned work,
    fragments, gibberish) is left out and reported in `omitted` so the owner can see it.
    """

    def __init__(self):
        self.omitted: list[str] = []

    @staticmethod
    def _is_completed_work(clause: str) -> bool:
        low = clause.lower()
        words = set(re.findall(r"[a-z]+", low))
        if words & PLANNING_WORDS or any(p in low for p in PLANNING_WORDS if " " in p):
            return False
        return bool(words & DONE_VERBS)

    def summarize(self, notes: str, job_title: str = "") -> str:
        parts = [p.strip(" .;-*•\t") for p in re.split(r"[\n,;]+|\.\s+|\s+and\s+(?=\w+ed\b)", notes)]
        parts = [p for p in parts if p]
        done = [p for p in parts if self._is_completed_work(p)]
        self.omitted = [p for p in parts if p not in done]
        if not done:
            raise AIError(UNCLEAR)
        # Lower-case the first letter unless the word looks like an acronym (e.g. "GFCI").
        done = [p if len(p) > 1 and p[1].isupper() else p[0].lower() + p[1:] for p in done]
        body = done[0] if len(done) == 1 else ", ".join(done[:-1]) + " and " + done[-1]
        return f"Completed work: {body}."


DEFAULT_MODEL = "claude-haiku-5-5"  # low-cost default; change with AI_MODEL
FALLBACK_MODEL_PREFIXES = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")


class AnthropicProvider(SummaryProvider):
    def __init__(self, api_key: str, model: str, timeout: float):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def summarize(self, notes: str, job_title: str = "") -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=1)
        user_content = (f"<job_title>{job_title}</job_title>\n" if job_title else "") + \
            f"<notes>\n{notes}\n</notes>"
        # No temperature / top_p / top_k / budget_tokens: current models reject them.
        params = dict(
            model=self.model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            output_config={"effort": "low"},
        )
        if self.model.startswith(FALLBACK_MODEL_PREFIXES):
            # On a safety decline, let the API retry on its default fallback model.
            # (Haiku, the default, has no server-side fallback, so it is not sent there.)
            params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            response = client.beta.messages.create(**params)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            log.error("AI provider rejected the API key")
            raise AIError(NOT_SET_UP)
        except anthropic.RateLimitError:
            log.warning("AI provider rate limited the request")
            raise AIError(UNAVAILABLE)
        except anthropic.APIStatusError as e:
            log.error("AI provider error status=%s", e.status_code)
            raise AIError(UNAVAILABLE)
        except anthropic.APIConnectionError:
            log.error("Could not reach AI provider")
            raise AIError(UNAVAILABLE)

        if response.stop_reason == "refusal":
            raise AIError(UNAVAILABLE)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise AIError(UNAVAILABLE)
        return text


def get_provider() -> SummaryProvider:
    cfg = current_app.config
    if cfg.get("AI_MOCK"):
        return MockProvider()
    if not cfg.get("AI_API_KEY"):
        raise AIError(NOT_SET_UP)
    return AnthropicProvider(cfg["AI_API_KEY"], cfg.get("AI_MODEL") or DEFAULT_MODEL,
                             cfg["AI_TIMEOUT_SECONDS"])


# --- Guard against invented details -------------------------------------------------

_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_NUMBER_WORDS.update({"dozen": "12"})
_MONTHS = ("january february march april may june july august september october "
           "november december jan feb mar apr jun jul aug sep sept oct nov dec").split()
_DAYS = "monday tuesday wednesday thursday friday saturday sunday today yesterday tomorrow".split()
_PROMISES = ("guarantee", "guaranteed", "warranty", "warranted", "warrantied", "lifetime",
             "certified", "certify", "insured", "licensed", "code-compliant", "up to code",
             "permanent", "never")
_MONEY = re.compile(r"[$£€]|\b(usd|dollars?|cents?|price|cost|charge|fee|invoice|paid|payment)\b")


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z'-]*", text.lower()))


def _numbers(text: str) -> set[str]:
    low = text.lower()
    nums = set(re.findall(r"\d+(?:\.\d+)?", low))
    for word, digit in _NUMBER_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            nums.add(digit)
    return nums


def find_invented_details(source: str, summary: str) -> list[str]:
    """Return reasons the summary adds facts that are not in the source text."""
    problems = []
    src_low, sum_low = source.lower(), summary.lower()
    src_words, sum_words = _words(source), _words(summary)

    extra_numbers = _numbers(summary) - _numbers(source)
    # "one" often just means "a" ("replaced one outlet"); only a written digit 1 counts.
    if "1" in extra_numbers and not re.search(r"(?<![\d.])1(?![\d.])", sum_low):
        extra_numbers.discard("1")
    if extra_numbers:
        problems.append("a number or quantity")
    if _MONEY.search(sum_low) and not _MONEY.search(src_low):
        problems.append("a price or payment detail")
    if (set(_MONTHS) | set(_DAYS)) & (sum_words - src_words):
        problems.append("a date")
    if any(p in sum_low and p not in src_low for p in _PROMISES):
        problems.append("a guarantee or promise")
    contacts = set(_CONTACT.findall(sum_low)) - set(_CONTACT.findall(src_low))
    if contacts:
        problems.append("contact details")
    return problems


_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://\S+|www\.\S+")


@dataclass
class SummaryResult:
    text: str
    omitted: list[str] = field(default_factory=list)  # note lines left out (mock mode)


def generate_summary(notes: str, job_title: str = "") -> SummaryResult:
    """Return a checked summary or raise AIError with a friendly message."""
    notes = (notes or "").strip()
    if not notes:
        raise AIError("Add some rough notes first, then try again.")
    if len(notes) > MAX_NOTES_CHARS:
        raise AIError(f"Notes are too long for a summary (max {MAX_NOTES_CHARS} characters).")

    provider = get_provider()
    try:
        summary = provider.summarize(notes, job_title)
    except AIError:
        raise
    except Exception:
        log.exception("AI provider failed unexpectedly")
        raise AIError(UNAVAILABLE)

    if INSUFFICIENT in summary:
        raise AIError(UNCLEAR)
    problems = find_invented_details(f"{job_title}\n{notes}", summary)
    if problems:
        log.warning("AI summary rejected; added %s", ", ".join(problems))
        raise AIError("The AI summary included details that aren't in your notes "
                      f"({', '.join(problems)}), so it was discarded. "
                      "Try again or enter the job summary manually.")
    return SummaryResult(summary.strip(), getattr(provider, "omitted", []))
