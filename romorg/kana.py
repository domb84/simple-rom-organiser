"""Katakana read as the English it was made from: ``ドラゴンクエスト`` and ``Dragon Quest`` have the same consonants.

A Japanese release of a game often has its English title written in katakana (``リトルインフェルノ`` for ``Little Inferno``) and
nothing else of Latin letters, so no text of the title names the English one. The consonants of the two do: both are reduced to the
same rough skeleton (:func:`ja_key`, :func:`en_key`; vowels, doubled letters and the sounds Japanese cannot tell apart - l / r, b / v,
h / f - do not count). A skeleton is a lead, never a proof: it only joins titles that have more evidence besides (the same year, one
candidate, see ``alttitles``). A title with a kanji in it has no reading here (``romaji`` returns ``""``).
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["romaji", "ja_key", "en_key", "close", "gloss"]

_BASE = {
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o", "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "サ": "sa", "シ": "si", "ス": "su", "セ": "se", "ソ": "so", "タ": "ta", "チ": "ti", "ツ": "tu", "テ": "te", "ト": "to",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no", "ハ": "ha", "ヒ": "hi", "フ": "hu", "ヘ": "he", "ホ": "ho",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo", "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro", "ワ": "wa", "ヲ": "o", "ン": "n",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go", "ザ": "za", "ジ": "zi", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "ダ": "da", "ヂ": "zi", "ヅ": "zu", "デ": "de", "ド": "do", "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po", "ヴ": "bu",
}
_SMALL_Y = {"ャ": "a", "ュ": "u", "ョ": "o"}
_SMALL_V = {"ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o"}
_BREAK = frozenset(" 　-=＝·'!！:：、。./／・")
_SOUNDS = str.maketrans({"l": "r", "v": "b", "f": "h", "c": "k", "q": "k", "x": "k", "j": "z", "d": "t"})
_SMALL_WORDS = re.compile(r"\b(the|a|an|and|for|in|to)\b")


def romaji(text: str) -> str:
    """Rough Latin letters of a title of kana (hiragana and full-width forms included); ``""`` when it has a kanji or another letter."""
    s = unicodedata.normalize("NFKC", text or "")
    s = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if c in _BASE:
            r = _BASE[c]
            if nxt in _SMALL_Y:
                r = r[:-1] + "y" + _SMALL_Y[nxt]
                i += 1
            elif nxt in _SMALL_V:
                r = ("w" if c == "ウ" else r[:-1]) + _SMALL_V[nxt]
                i += 1
            out.append(r)
        elif c in "ッー":
            out.append("")                      # (a doubled consonant and a long vowel change no skeleton)
        elif c in _BREAK:
            out.append(" ")
        elif c.isdigit() or (c.isascii() and c.isalpha()):
            out.append(c.lower())
        else:
            return ""
        i += 1
    return "".join(out)


def _squeeze(r: str) -> str:
    return re.sub(r"(.)\1+", r"\1", r).translate(_SOUNDS)


def ja_key(text: str) -> str:
    """The skeleton of a title of kana (``""``: it has no reading)."""
    r = romaji(text)
    if not r.strip():
        return ""
    r = r.replace("sh", "s").replace("ch", "t").replace("ts", "t").replace("ji", "z")
    return _squeeze(re.sub(r"[aeiouy\s]", "", r))


def en_key(title: str) -> str:
    """The skeleton of an English title (the same letters as :func:`ja_key` makes of its katakana)."""
    t = unicodedata.normalize("NFKD", title or "").casefold()
    t = _SMALL_WORDS.sub(" ", t).replace(" of ", " ov ")     # (オブ)
    t = re.sub(r"[^a-z0-9 ]", "", t)
    for a, b in (("ph", "f"), ("th", "s"), ("ck", "k"), ("kn", "n"), ("wh", "w"), ("sh", "s"), ("ch", "t"), ("gh", ""), ("oo", "u"),
                 ("ee", "i"), ("tion", "n")):
        t = t.replace(a, b)
    return _squeeze(re.sub(r"[aeiouy\s]", "", t))


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def close(a: str, b: str, slack: int = 2) -> bool:
    """Whether two skeletons (at least six letters) differ by no more than ``slack`` letters."""
    return len(a) >= 6 and len(b) >= 6 and abs(len(a) - len(b)) <= slack and _distance(a, b) <= slack


def gloss(native: str, words: str) -> bool:
    """Whether ``native`` (the kana of a title) is the Japanese spelling of its Latin ``words`` (``ブレードオブダークネス`` / ``Blade of
    Darkness``): a long Japanese text next to the words is then no other game's subtitle (``北斗の拳``, with a kanji, never is)."""
    a = ja_key(native)
    return bool(a) and close(a, en_key(words))
