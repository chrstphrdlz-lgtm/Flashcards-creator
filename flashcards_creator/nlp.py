"""Stage 1b: paragraphs -> candidate words.

spaCy is doing real work here, not decoration. French elision and clitics
(``d'aborder``, ``c'est``, ``avez-vous``) defeat naive tokenizers, and context
POS is what makes the CEFR lookup in stage 2 accurate -- FLELex is keyed by
(lemma, part of speech), so ``livre`` the noun and ``livrer`` the verb are
different entries.

Filtering here is strictly mechanical. Anything requiring judgement is left for
stage 3, where you can steer it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from . import lexicon

MODEL = "fr_core_news_md"

# Content words only. Everything else is grammar, not vocabulary to learn.
CONTENT_POS = {"NOUN", "VERB", "ADJ", "ADV", "INTJ"}

# Named-entity labels whose tokens are names, not vocabulary.
ENTITY_LABELS = {"PER", "LOC", "ORG", "MISC"}

# Auxiliaries and light verbs a B2 reader knows cold; they survive the POS
# filter but carry no learning value.
STOP_LEMMAS = {
    "être", "avoir", "faire", "aller", "pouvoir", "vouloir", "devoir", "falloir",
    "dire", "voir", "savoir", "venir", "prendre", "donner", "mettre", "passer",
    "plus", "ne", "pas", "y", "en", "là", "ici", "oui", "non", "très", "aussi",
}

MIN_LENGTH = 2

_WORD_RE = re.compile(r"^[a-zà-öø-ÿ][a-zà-öø-ÿ'’\-]*$", re.IGNORECASE)


class NlpError(Exception):
    """Raised when the spaCy model is unavailable."""


@dataclass
class Occurrence:
    paragraph: int
    sentence: str
    surface: str


@dataclass
class Candidate:
    lemma: str
    pos: str
    occurrences: list[Occurrence] = field(default_factory=list)
    surfaces: set[str] = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.occurrences)

    def to_dict(self) -> dict[str, Any]:
        first = self.occurrences[0]
        return {
            "lemma": self.lemma,
            "pos": self.pos,
            "count": self.count,
            "surfaces": sorted(self.surfaces),
            "first_paragraph": first.paragraph,
            "sentence": first.sentence,
            "paragraphs": sorted({o.paragraph for o in self.occurrences}),
        }


@lru_cache(maxsize=1)
def load_model(name: str = MODEL):
    """Load spaCy once per process; it is expensive to construct."""
    try:
        import spacy
    except ImportError as exc:  # pragma: no cover
        raise NlpError("spaCy is not installed; run `pip install -e .`") from exc

    try:
        return spacy.load(name)
    except OSError as exc:
        raise NlpError(
            f"the French model {name!r} is not installed. Install it with:\n"
            f"  pip install https://github.com/explosion/spacy-models/releases/"
            f"download/{name}-3.8.0/{name}-3.8.0-py3-none-any.whl"
        ) from exc


def _normalise(lemma: str) -> str:
    return lemma.strip().lower().replace("’", "'")


def is_wordlike(text: str) -> bool:
    return bool(_WORD_RE.match(text.replace("’", "'")))


def extract_candidates(
    paragraphs: Iterable[str],
    known_forms: set[str] | None = None,
    model: str = MODEL,
) -> list[Candidate]:
    """Turn paragraphs into deduplicated (lemma, POS) candidates.

    ``known_forms`` is the set of lemmas present in the reference lexicons. A
    token in none of them is almost always a proper noun the tagger missed, a
    foreign word, or OCR damage -- Hugo's text produced ``gargantua``,
    ``venise`` and ``civita-vecchia`` this way.
    """
    nlp = load_model(model)
    paragraphs = list(paragraphs)
    candidates: dict[tuple[str, str], Candidate] = {}

    for para_index, doc in enumerate(nlp.pipe(paragraphs, batch_size=32), start=1):
        # Token indices covered by a named entity, so we can drop names even
        # when the tagger labelled the individual token as a common noun.
        entity_tokens = {
            i for ent in doc.ents if ent.label_ in ENTITY_LABELS
            for i in range(ent.start, ent.end)
        }

        for token in doc:
            if token.i in entity_tokens:
                continue
            if token.pos_ not in CONTENT_POS:
                continue
            if token.pos_ == "PROPN" or token.is_stop or token.like_num:
                continue
            if not token.is_alpha and not is_wordlike(token.text):
                continue

            lemma, pos, resolved = lexicon.resolve_lemma(
                token.text, token.lemma_, token.pos_)
            lemma = _normalise(lemma)
            if len(lemma) < MIN_LENGTH or lemma in STOP_LEMMAS:
                continue
            if not is_wordlike(lemma):
                continue
            # Unresolvable means neither lexicon knows the word: a proper noun
            # the tagger missed, a foreign word, or OCR damage.
            if not resolved:
                continue
            if pos not in CONTENT_POS:
                continue
            if known_forms is not None and lemma not in known_forms:
                continue

            key = (lemma, pos)
            entry = candidates.get(key)
            if entry is None:
                entry = candidates[key] = Candidate(lemma=lemma, pos=pos)
            entry.surfaces.add(token.text)
            entry.occurrences.append(Occurrence(
                paragraph=para_index,
                sentence=re.sub(r"\s+", " ", token.sent.text).strip(),
                surface=token.text,
            ))

    # Frequent words first: what recurs in the chapter matters most to read it.
    return sorted(candidates.values(),
                  key=lambda c: (-c.count, c.lemma, c.pos))
