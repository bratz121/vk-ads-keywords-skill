"""Russian Snowball stemmer (stdlib only) and Wordstat-style phrase signatures.

Wordstat broad match ignores word order, word form and stop words, so two
phrases with the same set of stems cover the same queries, and a phrase whose
stems are a subset of another's absorbs it. The stemmer follows the reference
Snowball algorithm (snowballstem.org/algorithms/russian) closely enough for
that comparison; it is not a lemmatizer and never used for output text.
"""

import re

_VOWELS = frozenset("аеиоуыэюя")

# Group 1 endings are valid only after "а" or "я" (which stays in the stem).
_PERFECTIVE_GERUND = {"в": 1, "вши": 1, "вшись": 1,
                      "ив": 2, "ивши": 2, "ившись": 2, "ыв": 2, "ывши": 2, "ывшись": 2}
_ADJECTIVE = tuple("ее ие ые ое ими ыми ей ий ый ой ем им ым ом его ого ему ому "
                   "их ых ую юю ая яя ою ею".split())
_PARTICIPLE = {"ем": 1, "нн": 1, "вш": 1, "ющ": 1, "щ": 1, "ивш": 2, "ывш": 2, "ующ": 2}
_REFLEXIVE = ("ся", "сь")
_VERB = {**{s: 1 for s in "ла на ете йте ли й л ем н ло но ет ют ны ть ешь нно".split()},
         **{s: 2 for s in ("ила ыла ена ейте уйте ите или ыли ей уй ил ыл им ым ен ило "
                           "ыло ено ят ует уют ит ыт ены ить ыть ишь ую ю").split()}}
_NOUN = tuple("а ев ов ие ье е иями ями ами еи ии и ией ей ой ий й иям ям ием ем ам "
              "ом о у ах иях ях ы ь ию ью ю ия ья я".split())
_DERIVATIONAL = ("ост", "ость")
_TIDY = ("ейш", "ейше", "н", "ь")

# Yandex ignores these in broad match unless fixed with "+"; they never make
# two phrases different.
STOP_WORDS = frozenset("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да
даже для до его ее если есть еще же за здесь и из или им их к как ко когда кто ли либо
мне может мы на надо наш не него нее нет ни них но ну о об обо однако он она они оно от
очень по под при про с со так также такой там те тем то того тоже той только том ты у
уже хотя чего чей чем что чтобы чье чья эта эти это я
""".split())

_TOKEN = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)


def _regions(word):
    """Return (rv, r2) start offsets as defined by the Snowball algorithm."""
    n = len(word)

    def gopast(pos, want_vowel):
        while pos < n and (word[pos] in _VOWELS) != want_vowel:
            pos += 1
        return pos + 1 if pos < n else None

    rv = gopast(0, True)
    if rv is None:
        return n, n
    step = gopast(rv, False)
    step = gopast(step, True) if step is not None else None
    step = gopast(step, False) if step is not None else None
    return rv, (step if step is not None else n)


def _longest(word, rv, endings):
    best = ""
    for ending in endings:
        if len(ending) > len(best) and word.endswith(ending) and len(word) - len(ending) >= rv:
            best = ending
    return best


def _strip_grouped(word, rv, table):
    """Strip the longest ending; group 1 needs а/я before it, still inside RV."""
    ending = _longest(word, rv, table)
    if not ending:
        return word, False
    cut = len(word) - len(ending)
    if table[ending] == 1 and (cut - 1 < rv or word[cut - 1] not in "ая"):
        return word, False
    return word[:cut], True


def stem(word):
    word = word.lower().replace("ё", "е")
    rv, r2 = _regions(word)

    word, done = _strip_grouped(word, rv, _PERFECTIVE_GERUND)
    if not done:
        reflexive = _longest(word, rv, _REFLEXIVE)
        if reflexive:
            word = word[:-len(reflexive)]
        adjective = _longest(word, rv, _ADJECTIVE)
        if adjective:
            word = word[:-len(adjective)]
            word, _ = _strip_grouped(word, rv, _PARTICIPLE)
        else:
            word, done = _strip_grouped(word, rv, _VERB)
            if not done:
                noun = _longest(word, rv, _NOUN)
                if noun:
                    word = word[:-len(noun)]

    if word.endswith("и") and len(word) - 1 >= rv:
        word = word[:-1]

    derivational = _longest(word, rv, _DERIVATIONAL)
    if derivational and len(word) - len(derivational) >= r2:
        word = word[:-len(derivational)]

    tidy = _longest(word, rv, _TIDY)
    if tidy in ("ейш", "ейше"):
        word = word[:-len(tidy)]
        tidy = "н"
    if tidy == "н":
        if word.endswith("нн") and len(word) - 2 >= rv:
            word = word[:-1]
    elif tidy == "ь":
        word = word[:-1]
    return word


def tokens(phrase):
    """Lower-cased word tokens; hyphens and punctuation split words like Yandex does."""
    return [token.lower().replace("ё", "е") for token in _TOKEN.findall(phrase)]


def signature(phrase):
    """Frozen set of stems of significant words: equal sets mean equal broad-match coverage."""
    return frozenset(stem(token) for token in tokens(phrase) if token not in STOP_WORDS)


def significant_words(phrase):
    return [token for token in tokens(phrase) if token not in STOP_WORDS]
