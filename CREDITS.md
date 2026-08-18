# Credits and licences

This tool builds nothing from scratch: the CEFR levels, the frequency data,
the definitions and the audio all come from other people's work. None of it is
committed to this repository — `scripts/fetch_data.py` downloads it at setup —
but the licences apply to whatever you build with it.

## FLELex (CEFR levels)

The `FleLex_TT_Beacco` lexicon, from CENTAL at UCLouvain. This is the source of
every A1–C2 level the tool reports for a word it recognises.

> François, T., Gala, N., Watrin, P. & Fairon, C. (2014). *FLELex: a graded
> lexical resource for French foreign learners.* LREC 2014, Reykjavik.

> Pintard, A. & François, T. (2020). *Combining expert knowledge with frequency
> information to infer CEFR levels for words.* Proceedings of the 1st Workshop
> on Tools and Resources to Empower People with REAding DIfficulties (READI),
> 85–92.

**Licence: [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)**
— attribution, share-alike, and **non-commercial**. Personal study is fine;
selling decks built with it is not.

<https://cental.uclouvain.be/cefrlex/flelex/>

## Lexique 3.83 (lemmas and modern frequency)

New, B., Pallier, C., Ferrand, L. & Matos, R. (2001). *Une base de données
lexicales du français contemporain sur internet: LEXIQUE.* L'Année
Psychologique, 101, 447–462.

Used for lemmatisation and for the `modern_freq` signal — occurrences per
million in a corpus of books — which is what separates advanced-but-useful
vocabulary from merely archaic vocabulary.

**Licence: CC BY-SA 4.0.** <http://www.lexique.org>

## Wiktionary (definitions, gender, IPA)

English Wiktionary, via the [Kaikki](https://kaikki.org) machine-readable
extraction by Tatu Ylonen.

> Ylonen, T. (2022). *Wiktextract: Wiktionary as Machine-Readable Structured
> Data.* LREC 2022, 1317–1325.

**Licence: CC BY-SA 4.0** (Wiktionary content).

## Pronunciation audio

Recordings from [Wikimedia Commons](https://commons.wikimedia.org), the
majority contributed by native speakers through
[Lingua Libre](https://lingualibre.org).

**Licence: CC BY-SA** (individual files vary; a few are CC0 or public domain).

Each card carries its recording's filename in a `Credits` field, which names
the contributing speaker — that is the attribution these licences require. If
you share a deck, keep that field.

## Synthesised audio

Where no human recording exists and `--tts-fallback` is used, audio is
generated with [gTTS](https://github.com/pndurette/gTTS). Those cards say so
explicitly, so a synthetic voice is never mistaken for a native speaker.

## spaCy

Tokenisation, lemmatisation, part-of-speech tagging and named-entity
recognition use `fr_core_news_md` from [spaCy](https://spacy.io) (MIT; the
model itself is CC BY-SA 4.0, trained on UD French Sequoia and WikiNER).

## The book

Whatever EPUB you point this at is yours to account for. *Notre-Dame de Paris*
was published in 1831 and is in the public domain.

---

## If you share a deck

Cards contain Wiktionary definitions (CC BY-SA) and Commons audio (CC BY-SA),
and the word list derives from FLELex (CC BY-NC-SA). Sharing therefore means:
attribute, share alike, and **do not sell**. Keeping the `Credits` field on the
cards covers most of it.
