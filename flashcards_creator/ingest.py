"""Stage 1a: EPUB -> chapters -> paragraphs.

Books nest differently. Notre-Dame de Paris, the book this was built against,
is two-level: 11 *Livres*, each holding chapters numbered I..VIII, 59 content
chapters in all. So a chapter is addressed either as ``1.6`` (Livre I, ch. VI)
or by its flat position ``6``.

Three things in that EPUB would break a naive "one spine document = one
chapter" rule, and every book seems to have its own version of them:

  * Each Livre is preceded by a ~35-character stub containing only the words
    "LIVRE PREMIER". Treated as chapters, those yield empty decks.
  * Front and back matter (cover, publisher's note, NOTES, TABLE, COLOPHON)
    sit in the same spine.
  * The first chapter of each Livre carries *both* headings, so the Livre name
    must not be mistaken for the chapter title.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

# Below this many characters of body text, a spine document is a section
# divider or a decorative page rather than a chapter.
MIN_CHAPTER_CHARS = 200

# Headings that mark front or back matter rather than a chapter.
MATTER_PATTERNS = [
    r"^table$", r"^tables?\s+des\s+mati", r"^notes?$", r"^note\s+", r"^colophon$",
    r"^couverture$", r"^sommaire$", r"^préface$", r"^avertissement$",
    r"^bibliographie$", r"^index$", r"^copyright$", r"^achev[ée]\s+d.imprimer$",
]

# "LIVRE PREMIER", "LIVRE DEUXIÈME", "PREMIÈRE PARTIE", "TOME II" ...
PART_RE = re.compile(
    r"^\s*(livre|partie|tome|première\s+partie|deuxième\s+partie)\b|"
    r"^\s*\w+\s+partie\s*$",
    re.IGNORECASE,
)

ROMAN_RE = re.compile(r"^[IVXLCDM]+$", re.IGNORECASE)

ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

ORDINAL_WORDS = {
    "premier": 1, "première": 1, "deuxième": 2, "second": 2, "seconde": 2,
    "troisième": 3, "quatrième": 4, "cinquième": 5, "sixième": 6,
    "septième": 7, "huitième": 8, "neuvième": 9, "dixième": 10,
    "onzième": 11, "douzième": 12, "treizième": 13, "quatorzième": 14,
    "quinzième": 15, "seizième": 16, "dix-septième": 17, "dix-huitième": 18,
    "dix-neuvième": 19, "vingtième": 20,
}


class IngestError(Exception):
    """Raised when an EPUB cannot be read or contains no usable chapters."""


@dataclass
class Chapter:
    """One chapter of the book, addressable two ways."""

    index: int                    # flat position, 1-based
    part: int | None              # Livre number, if the book has parts
    number: int | None            # chapter number within its part
    title: str
    part_title: str
    href: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def chapter_id(self) -> str:
        """Stable id used for directory names, e.g. '01.06' or '07'."""
        if self.part is not None and self.number is not None:
            return f"{self.part:02d}.{self.number:02d}"
        return f"{self.index:02d}"

    @property
    def label(self) -> str:
        """Human-readable, e.g. 'Livre I, ch. VI — LA ESMERALDA'."""
        bits = []
        if self.part_title:
            bits.append(self.part_title)
        if self.number is not None:
            bits.append(f"ch. {to_roman(self.number)}")
        head = ", ".join(bits)
        if self.title:
            return f"{head} — {self.title}" if head else self.title
        return head or f"Chapter {self.index}"

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)

    def incipit(self, limit: int = 200) -> str:
        """The chapter's opening, for telling one deck from another.

        Truncated on a word boundary so it reads as a sentence rather than
        stopping mid-word.
        """
        if not self.paragraphs:
            return ""
        text = " ".join(self.paragraphs)[: limit * 2].strip()
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def to_roman(n: int) -> str:
    pairs = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
             (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
             (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for value, sym in pairs:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def from_roman(s: str) -> int | None:
    s = s.upper().strip()
    if not s or not ROMAN_RE.match(s):
        return None
    total = prev = 0
    for ch in reversed(s):
        value = ROMAN_VALUES.get(ch)
        if value is None:
            return None
        total = total - value if value < prev else total + value
        prev = max(prev, value)
    return total or None


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def part_number(heading: str) -> int | None:
    """'LIVRE DEUXIÈME' -> 2. Handles roman numerals and ordinal words."""
    words = _strip_accents(heading.lower()).replace("’", "'").split()
    for word in words[1:]:
        word = word.strip(".,;:")
        if (value := from_roman(word)) is not None:
            return value
        if word.isdigit():
            return int(word)
        for name, value in ORDINAL_WORDS.items():
            if _strip_accents(name) == word:
                return value
    return None


def _chapter_number(heading: str) -> int | None:
    """A heading that is *only* a chapter numeral -> its value.

    Deliberately strict: 'IV' and '12' are chapter numbers, but a title that
    merely starts with one is a title.
    """
    h = heading.strip().rstrip(".")
    if not h:
        return None
    if h.isdigit():
        return int(h)
    if (value := from_roman(h)) is not None:
        return value
    m = re.match(r"^(?:chapitre|chapter)\s+([IVXLCDM]+|\d+)\.?$", h, re.IGNORECASE)
    if m:
        token = m.group(1)
        return int(token) if token.isdigit() else from_roman(token)
    return None


def is_matter(heading: str) -> bool:
    h = heading.strip().lower()
    return any(re.search(p, h, re.IGNORECASE) for p in MATTER_PATTERNS)


# ---------------------------------------------------------------------------
# EPUB reading
# ---------------------------------------------------------------------------

def _spine_documents(zf: zipfile.ZipFile) -> list[str]:
    """Spine hrefs in reading order.

    Read straight from the OPF rather than via a library: we only need the
    ordered document list, and doing it here keeps the namespace handling and
    path resolution explicit.
    """
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError) as exc:
        raise IngestError(f"not a readable EPUB: {exc}") from exc

    rootfile = next(
        (el.get("full-path") for el in container.iter()
         if el.tag.endswith("rootfile") and el.get("full-path")), None)
    if not rootfile:
        raise IngestError("EPUB container.xml names no OPF file")

    try:
        opf = ET.fromstring(zf.read(rootfile))
    except (KeyError, ET.ParseError) as exc:
        raise IngestError(f"cannot read {rootfile}: {exc}") from exc

    base = posixpath.dirname(rootfile)
    manifest = {el.get("id"): el.get("href")
                for el in opf.iter() if el.tag.endswith("}item")}
    hrefs = []
    for el in opf.iter():
        if not el.tag.endswith("}itemref"):
            continue
        href = manifest.get(el.get("idref"))
        if href:
            hrefs.append(posixpath.normpath(posixpath.join(base, href)))
    return hrefs


def _book_title(zf: zipfile.ZipFile) -> str:
    """The book's title from OPF metadata, or '' if it has none."""
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = next(el.get("full-path") for el in container.iter()
                        if el.tag.endswith("rootfile") and el.get("full-path"))
        opf = ET.fromstring(zf.read(rootfile))
    except (KeyError, ET.ParseError, StopIteration):
        return ""
    for el in opf.iter():
        if el.tag.endswith("}title") and (el.text or "").strip():
            return el.text.strip()
    return ""


def _parse_document(raw: bytes) -> tuple[list[str], list[str]]:
    """One spine document -> (headings, paragraphs)."""
    soup = BeautifulSoup(raw, "lxml-xml")
    if soup.find() is None:
        soup = BeautifulSoup(raw, "html.parser")

    headings = []
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if text:
            headings.append(text)
        tag.decompose()  # keep headings out of the body text

    paragraphs = []
    for tag in soup.find_all("p"):
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if len(text) > 1:
            paragraphs.append(text)

    if not paragraphs:
        # Some books use <div> instead of <p>.
        for tag in soup.find_all("div"):
            text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
            if len(text) > 40:
                paragraphs.append(text)

    return headings, paragraphs


def read_chapters(epub_path: Path) -> tuple[str, list[Chapter]]:
    """Return (book title, chapters) from an EPUB.

    Divider stubs and front/back matter are dropped here, so callers only ever
    see real chapters.
    """
    epub_path = Path(epub_path)
    if not epub_path.exists():
        raise IngestError(f"no such file: {epub_path}")

    try:
        zf = zipfile.ZipFile(epub_path)
    except zipfile.BadZipFile as exc:
        raise IngestError(f"{epub_path} is not a valid EPUB (bad zip): {exc}") from exc

    with zf:
        hrefs = _spine_documents(zf)
        book_title = _book_title(zf) or epub_path.stem

        # Parse everything once, then decide what counts as a chapter. Deciding
        # up front lets us ask "does this book number its chapters?" before
        # classifying any single document, which a streaming pass cannot do.
        docs = []
        for href in hrefs:
            try:
                raw = zf.read(href)
            except KeyError:
                continue
            headings, paragraphs = _parse_document(raw)
            docs.append({
                "href": href,
                "headings": headings,
                "paragraphs": paragraphs,
                "chars": sum(len(p) for p in paragraphs),
            })

        # Does the book label chapters with a numeral of its own ("I", "IV",
        # "12")? Notre-Dame does. When it does, a substantial document *without*
        # such a heading is front matter (a dedication, a publisher's note),
        # not an untitled chapter -- which is what makes it safe to skip rather
        # than guess.
        numbered = sum(
            1 for d in docs if d["chars"] >= MIN_CHAPTER_CHARS
            and any(_chapter_number(h) is not None for h in d["headings"])
        )
        numbered_mode = numbered >= 3

        chapters: list[Chapter] = []
        current_part: int | None = None
        current_part_title = ""
        number_in_part = 0

        for doc in docs:
            headings = doc["headings"]

            part_heading = next((h for h in headings if PART_RE.match(h)), None)
            if part_heading:
                value = part_number(part_heading)
                if value is not None and value != current_part:
                    current_part = value
                    current_part_title = re.sub(r"\s+", " ", part_heading).strip()
                    number_in_part = 0

            # Divider stubs ("LIVRE PREMIER" alone) and decorative pages.
            if doc["chars"] < MIN_CHAPTER_CHARS:
                continue

            # Front and back matter is always skipped, never used to stop the
            # scan: a publisher's note sits *before* chapter 1, so breaking on
            # the first match would truncate the book to nothing.
            heads_wo_part = [h for h in headings if h != part_heading]
            if any(is_matter(h) for h in heads_wo_part):
                continue

            number = None
            title = ""
            for h in heads_wo_part:
                value = _chapter_number(h)
                if value is not None and number is None:
                    number = value
                elif not title:
                    title = h

            if number is None:
                if numbered_mode:
                    continue  # unnumbered in a numbered book => front matter
                number_in_part += 1
                number = number_in_part
            else:
                number_in_part = number

            chapters.append(Chapter(
                index=len(chapters) + 1,
                part=current_part,
                number=number,
                title=title,
                part_title=current_part_title,
                href=doc["href"],
                paragraphs=doc["paragraphs"],
            ))

    if not chapters:
        raise IngestError(
            f"no chapters found in {epub_path.name}. The book may use an "
            f"unusual structure; inspect its spine and adjust MIN_CHAPTER_CHARS."
        )
    return book_title, chapters


def select_chapter(chapters: list[Chapter], spec: str) -> Chapter:
    """Resolve '1.6' (Livre I, ch. VI) or a flat '6' to one chapter."""
    spec = str(spec).strip()

    if "." in spec:
        part_s, num_s = spec.split(".", 1)
        try:
            part, number = int(part_s), int(num_s)
        except ValueError:
            raise IngestError(f"cannot parse chapter {spec!r}; try '1.6' or '6'")
        for ch in chapters:
            if ch.part == part and ch.number == number:
                return ch
        raise IngestError(
            f"no chapter {spec} in this book "
            f"(it has {len(chapters)} chapters; run `fcc chapters` to list them)")

    try:
        index = int(spec)
    except ValueError:
        raise IngestError(f"cannot parse chapter {spec!r}; try '1.6' or '6'")
    if not 1 <= index <= len(chapters):
        raise IngestError(
            f"chapter {index} is out of range; this book has {len(chapters)} "
            f"chapters. Run `fcc chapters` to list them.")
    return chapters[index - 1]


def split_paragraphs(chapter: Chapter, split_at: int | None) -> list[list[str]]:
    """Break an oversized chapter into review-sized batches on paragraph bounds.

    Chapter lengths in a single book vary enormously -- Notre-Dame ranges from
    1.2k to 68k characters -- and a 68k-character chapter is not a sane review
    unit.
    """
    if not split_at or split_at <= 0:
        return [chapter.paragraphs]

    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for para in chapter.paragraphs:
        if current and size + len(para) > split_at:
            batches.append(current)
            current, size = [], 0
        current.append(para)
        size += len(para)
    if current:
        batches.append(current)
    return batches or [chapter.paragraphs]
