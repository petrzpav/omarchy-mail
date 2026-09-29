"""Ask Jev (TypeSafe AI) which category a message belongs to, and how good a reply is."""

import re
from dataclasses import dataclass, field

import httpx

from .config import Config

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
KEEP = "Inbox"
KEEP_DESCRIPTION = ("Personal or one-off mail that needs the reader's attention and fits none of the "
                    "other categories")


@dataclass
class Verdict:
    choice: str
    confidence: float
    probabilities: dict[str, float]
    model: str = ""


class JevError(Exception):
    pass


def _post(cfg: Config, state: dict, questions: dict) -> dict:
    """Send questions about `state` to Jev; the raw response."""
    if not cfg.jev_key:
        raise JevError("TYPESAFE_API_KEY missing in ~/.config/petrzpav-mail/secrets")
    try:
        r = httpx.post(ENDPOINT, json={"model": cfg.jev_model, "state": state, "questions": questions},
                       timeout=30, headers={"Authorization": f"Bearer {cfg.jev_key}"})
    except httpx.HTTPError as e:
        raise JevError(f"Jev unreachable: {e}") from e
    if r.status_code != 200:
        raise JevError(f"Jev {r.status_code}: {r.text[:200]}")
    return r.json()


def _verdict(data: dict, q: str) -> Verdict:
    ans = data["answers"][q]
    return Verdict(ans["choice"], float(ans.get("confidence", 0)),
                   {k: float(v) for k, v in ans.get("probabilities", {}).items()}, data.get("model", ""))


def ask(cfg: Config, state: dict, instructions: str, criteria: dict[str, str]) -> Verdict:
    """One Choice question to Jev."""
    data = _post(cfg, state, {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}})
    return _verdict(data, "q")


def classify(cfg: Config, sender: str, to: str, subject: str, body: str,
             categories=None) -> Verdict:
    cats = categories if categories is not None else cfg.categories
    criteria = {c.name: c.description or c.name for c in cats}
    criteria[KEEP] = KEEP_DESCRIPTION
    data = _post(cfg, {"from": sender, "to": to, "subject": subject, "body": body[:2000]},
                 {"category": {"type": "choice", "instructions": "Which email folder should this email be filed in",
                               "criteria": criteria}})
    return _verdict(data, "category")


# -- reviewing a reply before it is sent

@dataclass
class Criterion:
    """One thing a reply is judged on, graded good / ok / poor."""
    name: str
    question: str
    good: str
    ok: str
    poor: str
    reply_only: bool = False   # needs the message being answered


CRITERIA = [
    Criterion("clear", "Is the reply clear and easy to understand",
              "Clear and easy to follow", "Understandable but somewhat vague or confusing", "Unclear or confusing"),
    Criterion("tone", "Is the tone right for this conversation",
              "Polite and fits the conversation", "Acceptable but a bit off: too curt, too formal or too casual",
              "Rude, cold or inappropriate"),
    Criterion("length", "Is the reply a good length for what it needs to say",
              "The right length", "Somewhat too long or too short", "Far too long or too short"),
    Criterion("next step", "Is it clear what happens next",
              "The next step or outcome is clear, or nothing further is needed", "Only somewhat clear",
              "Unclear what happens next or who should act"),
    Criterion("language", "Is the spelling and grammar correct",
              "No mistakes", "A few small mistakes", "Many mistakes"),
]


@dataclass
class Grade:
    choice: str
    score: float        # 0..1: P(good) + P(ok) / 2


@dataclass
class Review:
    grades: dict[str, Grade]
    asked: int = 0                                        # questions and requests in the original
    missed: list[str] = field(default_factory=list)       # ... that the reply leaves unanswered


SENTENCE_CHECK = {"answered": "It asks or requests something and the reply answers or handles it",
                  "missed": "It asks or requests something and the reply ignores it",
                  "none": "It is not a question or request (greeting, thanks, information, sign-off)"}
MAX_SENTENCES = 25


def sentences(s: str) -> list[str]:
    """The original split into sentences, each one a candidate question for SENTENCE_CHECK."""
    parts = re.split(r"(?<=[.?!])\s+|\n\s*\n|\n(?=\s*[-*•\d])", s)
    out = [" ".join(p.split()) for p in parts]
    out = [p for p in out if len(p) >= 8]
    if len(out) > MAX_SENTENCES:          # a long email: questions first
        out = sorted(out, key=lambda p: "?" not in p)[:MAX_SENTENCES]
    return out


def review(cfg: Config, reply: str, subject: str = "", original: str = "") -> Review:
    """Grade a reply (or a new message, without `original`) on every criterion, and check each
    sentence of the original for a question or request the reply forgot. One request."""
    crits = [c for c in cfg.review or CRITERIA if original or not c.reply_only]
    state = {"subject": subject, "reply": reply[:4000]}
    questions = {c.name: {"type": "choice", "instructions": c.question,
                          "criteria": {"good": c.good, "ok": c.ok, "poor": c.poor}} for c in crits}
    sents = sentences(original) if original else []
    if original:
        state["original email"] = original[:3000]
    for i, sent in enumerate(sents):
        questions[f"sentence {i}"] = {
            "type": "choice", "criteria": SENTENCE_CHECK,
            "instructions": f'This sentence is from the original email: "{sent}". Does the reply answer it'}
    data = _post(cfg, state, questions)
    out = Review({})
    for c in crits:
        v = _verdict(data, c.name)
        out.grades[c.name] = Grade(v.choice, v.probabilities.get("good", 0) + v.probabilities.get("ok", 0) / 2)
    for i, sent in enumerate(sents):
        v = _verdict(data, f"sentence {i}")
        if v.choice != "none":
            out.asked += 1
            if v.choice == "missed":
                out.missed.append(sent)
    return out
