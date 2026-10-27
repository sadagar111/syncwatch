"""Turning the 32-byte signing key into 24 words for a paper backup, and back.

Uses the BIP39 standard (the same as crypto wallets): 256 bits of key plus an
8-bit checksum, split into 24 numbers of 11 bits, each picking one word from a
fixed list of 2048 English words (tools/bip39_english.txt, MIT licence, from
the python-mnemonic project). The checksum catches almost every typo when
typing the words back. Every word is unique in its first 4 letters, so typing
just the first 4 letters of each word also works.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Sequence

WORDLIST_FILE = Path(__file__).with_name("bip39_english.txt")
WORDLIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
WORD_COUNT = 24


class WordsError(ValueError):
    pass


def _wordlist() -> List[str]:
    data = WORDLIST_FILE.read_bytes().replace(b"\r\n", b"\n")  # same check whatever Git did to line endings
    if hashlib.sha256(data).hexdigest() != WORDLIST_SHA256:
        raise WordsError("tools/bip39_english.txt has been changed; put the original back")
    words = data.decode("ascii").split()
    assert len(words) == 2048
    return words


def to_words(seed: bytes) -> List[str]:
    if len(seed) != 32:
        raise WordsError("the key must be 32 bytes")
    words = _wordlist()
    checksum = hashlib.sha256(seed).digest()[0]
    bits = (int.from_bytes(seed, "big") << 8) | checksum
    return [words[(bits >> (11 * (WORD_COUNT - 1 - i))) & 0x7FF] for i in range(WORD_COUNT)]


def from_words(typed: Sequence[str]) -> bytes:
    words = _wordlist()
    by_prefix = {w[:4]: w for w in words}
    tokens = [t.strip().lower() for t in typed if t.strip()]
    if len(tokens) != WORD_COUNT:
        raise WordsError(f"{len(tokens)} words given, 24 needed")
    bits = 0
    for position, token in enumerate(tokens, 1):
        word = token if token in words else by_prefix.get(token[:4]) if len(token) >= 4 else None
        if word is None or not word.startswith(token[:4]):
            raise WordsError(f"word {position} ({token!r}) isn't in the word list. Check the spelling.")
        bits = (bits << 11) | words.index(word)
    seed = (bits >> 8).to_bytes(32, "big")
    if hashlib.sha256(seed).digest()[0] != bits & 0xFF:
        raise WordsError("the words don't fit together: one of them is probably wrong or in the wrong place")
    return seed


def format_words(words: Sequence[str], columns: int = 4) -> str:
    rows = []
    per_column = (len(words) + columns - 1) // columns
    for row in range(per_column):
        cells = []
        for column in range(columns):
            index = column * per_column + row
            if index < len(words):
                cells.append(f"{index + 1:>2}. {words[index]:<10}")
        rows.append("   ".join(cells))
    return "\n".join(rows)
