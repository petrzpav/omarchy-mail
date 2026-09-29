"""Ask Jev (TypeSafe AI) which category a message belongs to."""

from dataclasses import dataclass

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


def ask(cfg: Config, state: dict, instructions: str, criteria: dict[str, str]) -> Verdict:
    """One Choice question to Jev."""
    if not cfg.jev_key:
        raise JevError("TYPESAFE_API_KEY missing in ~/.config/petrzpav-mail/secrets")
    payload = {"model": cfg.jev_model, "state": state,
               "questions": {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}}}
    try:
        r = httpx.post(ENDPOINT, json=payload, timeout=30,
                       headers={"Authorization": f"Bearer {cfg.jev_key}"})
    except httpx.HTTPError as e:
        raise JevError(f"Jev unreachable: {e}") from e
    if r.status_code != 200:
        raise JevError(f"Jev {r.status_code}: {r.text[:200]}")
    data = r.json()
    ans = data["answers"]["q"]
    return Verdict(ans["choice"], float(ans.get("confidence", 0)),
                   {k: float(v) for k, v in ans.get("probabilities", {}).items()}, data.get("model", ""))


def classify(cfg: Config, sender: str, to: str, subject: str, body: str,
             categories=None) -> Verdict:
    if not cfg.jev_key:
        raise JevError("TYPESAFE_API_KEY missing in ~/.config/petrzpav-mail/secrets")
    cats = categories if categories is not None else cfg.categories
    criteria = {c.name: c.description or c.name for c in cats}
    criteria[KEEP] = KEEP_DESCRIPTION
    payload = {
        "model": cfg.jev_model,
        "state": {"from": sender, "to": to, "subject": subject, "body": body[:2000]},
        "questions": {
            "category": {
                "type": "choice",
                "instructions": "Which email folder should this email be filed in",
                "criteria": criteria,
            }
        },
    }
    try:
        r = httpx.post(ENDPOINT, json=payload, timeout=30,
                       headers={"Authorization": f"Bearer {cfg.jev_key}"})
    except httpx.HTTPError as e:
        raise JevError(f"Jev unreachable: {e}") from e
    if r.status_code != 200:
        raise JevError(f"Jev {r.status_code}: {r.text[:200]}")
    data = r.json()
    ans = data["answers"]["category"]
    return Verdict(ans["choice"], float(ans.get("confidence", 0)),
                   {k: float(v) for k, v in ans.get("probabilities", {}).items()},
                   data.get("model", ""))
