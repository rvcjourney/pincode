#!/usr/bin/env python3
"""
Spelling-tolerant matching for Indian place names.

The problem this solves: the master holds 745,231 place names transliterated
from Indian scripts by many different hands. A candidate types the spelling they
know, which is routinely not the one the government recorded.

    typed            stored
    Mohammadwadi     Mohamadwadi, Mahamadwadi
    Wadgaon          Vadgaon
    Kondwa           Kondhwa
    Bagalpur         Bhagalpur

Deliberately not Postgres pg_trgm: the pipeline has to run on SQLite locally and
Postgres on the VPS, so the keys are computed once at load time and stored in
indexed columns. That works identically on both and needs no extension.

Two keys, because one is not enough:
  fold_key  - conservative. Collapses the spelling variations that represent the
              same sound, and nothing else. A match here is almost certainly the
              same place.
  skel_key  - consonant skeleton. Vowels are the least stable part of an Indian
              transliteration, so dropping them catches Mahamadwadi/Mohamadwadi
              and Bangalore/Bengaluru. It over-matches (Mohan/Mahan collide), so
              it is only ever a fallback tier and results are ranked by actual
              similarity to what was typed.
"""
import difflib
import re

VOWELS = "aeiou"

# English spellings of the same Indian sound.
_PAIRS = (("ph", "f"), ("v", "w"), ("z", "j"), ("x", "ks"), ("q", "k"),
          ("ee", "i"), ("oo", "u"), ("aa", "a"), ("ii", "i"), ("uu", "u"))


def fold(s):
    """Conservative key: same sound, different spelling."""
    s = re.sub(r"[^a-z]", "", (s or "").lower())
    if not s:
        return ""
    # 'h' after a plosive marks aspiration and is routinely dropped:
    # Kondhwa/Kondwa, Thiruvananthapuram/Tiruvanantapuram, Bhagalpur/Bagalpur.
    # 'ch' and 'sh' are left alone - those are distinct sounds, not aspiration.
    s = re.sub(r"(?<=[bdgkptj])h", "", s)
    for a, b in _PAIRS:
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)      # mohammad -> mohamad


def skeleton(s):
    """Fallback key: consonants only, first letter kept for anchoring."""
    s = fold(s)
    return (s[0] + re.sub(f"[{VOWELS}]", "", s[1:])) if s else ""


def similarity(query, candidate):
    """0..1 similarity, used to rank results once a tier has matched."""
    a, b = (query or "").lower().strip(), (candidate or "").lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    base = difflib.SequenceMatcher(None, a, b).ratio()
    if b.startswith(a):                      # typing a prefix is a strong signal
        base = min(1.0, base + 0.15)
    return base


def rank(query, rows, key="locality_name", limit=25):
    """Order matches by how close they are to what was actually typed.

    Exact and prefix hits outrank fold-key hits, which outrank skeleton hits -
    so the loose fallback tier can stay loose without burying the obvious answer.
    """
    fq, sq = fold(query), skeleton(query)
    scored = []
    for r in rows:
        name = r.get(key) or ""
        if name.lower() == (query or "").lower():
            tier = 0
        elif name.lower().startswith((query or "").lower()):
            tier = 1
        elif fold(name) == fq:
            tier = 2
        elif fold(name).startswith(fq):
            tier = 3
        elif skeleton(name) == sq:
            tier = 4
        else:
            tier = 5
        scored.append((tier, -similarity(query, name), name, r))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    out = []
    for tier, negsim, _, r in scored[:limit]:
        r = dict(r)
        r["match_tier"] = tier
        r["score"] = round(-negsim, 3)
        out.append(r)
    return out
