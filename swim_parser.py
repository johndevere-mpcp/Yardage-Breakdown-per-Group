#!/usr/bin/env python3
"""swim_parser.py — manual-calc swim workout parser.

Reads a plain-text file containing one or more swim workouts. Splits on
date lines (Rule 0), then parses each workout block independently using a
recursive descent of multipliers, "rounds of" patterns, inline NxDIST
sets, plain distance lines, and tail-format total markers like
"100 easy - 3200".

Classification is by group attribution from the Coach: line:
    Sprint    : Dave's group
    Mid       : Josh's group
    Distance  : Noah's group
    All       : no group specified (whole-team / unattributed)

Internal section headers ("Sprint:", "Mid:", "Distance:") override the
default for the sets that follow. For each sub-session:
    sprint + mid + distance + all = total

Outputs each workout's breakdown then a weekly total.

Usage:
    python swim_parser.py workouts.txt
"""

import argparse
import re
import sys
from hashlib import md5
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Compiled regex patterns (one per spec rule).
# ---------------------------------------------------------------------------

# Rule 0: A line that BEGINS with a weekday and includes EITHER a 4-digit
# year OR a "(week N)" annotation marks a workout date header. Anchoring at
# the line start avoids matching preamble references like
# "As of Monday, September 8, 2025" — those start with "As of" not a weekday.
# The "(week N)" alternative lets us catch the Week 2 Tuesday header in the
# doc, which is written as "Tuesday, september 2 (week 2)" with no year.
DATE_RE = re.compile(
    r"(?i)^\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)[,\s]"
    r".*?(?:\d{4}|\(\s*week\s*\d+\s*\))"
)

# A "Week N" header line (case-insensitive). Treated as a workout boundary
# so any leftover content from the previous week can't leak forward.
WEEK_HEADER_RE = re.compile(r"^\s*week\s+(\d+)\s*$", re.IGNORECASE)

# Day-of-week + AM/PM marker. The Week 3+ section of the doc uses these
# instead of full dates ("Monday AM", "Tuesday PM 1", etc.); we treat each
# as a workout boundary so those weeks aren't reported as empty. Anchored
# at line start so an inline mention can't false-positive.
#
# Starting around Week 11 the doc also uses ABBREVIATED day names — 'MON
# AM 15', 'TU AM 11', 'WED PM 12', 'THU AM 13'. Without matching these,
# late-season workouts would inherit the previous matched header (often
# a 'Saturday PM' overview line at the top of the week), which both
# duplicates the prior week's content and loses Weeks 16/17 entirely
# because their MON/WED/etc headers never match.
DAY_TIME_RE = re.compile(
    r"(?i)^\s*(?:"
    r"mon(?:day)?|tu(?:e|es|esday)?|wed(?:nesday)?|"
    r"thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
    r")\s+(?:AM|PM)\b"
)

# A Coach: / Coach - line ALSO splits the file: each parallel sub-workout in
# the doc starts with its own Coach line under (or near) the date header.
COACH_LINE_SPLIT_RE = re.compile(r"^\s*Coach\s*[:\-]", re.IGNORECASE)

# A row of 16+ underscores is a visual separator the doc uses between parallel
# workouts that don't both have explicit Coach lines.
UNDERSCORE_SPLIT_RE = re.compile(r"^\s*_{16,}\s*$")

# Workout uses meters iff its body contains '[####m / ####m]' or '[####m]'
# bracket notation. The 'LCM' / 'AM-LCM-LEGENDS' tokens that sometimes
# appear in date headers are NOT reliable unit markers — the user's own
# spec example has "AM-LCM-LEGENDS" but is reported as yards.
METER_BODY_RE = re.compile(
    r"\[\s*\d[\d,]*\s*m\s*(?:/|\])",
    re.IGNORECASE,
)

# Capture-only patterns that pull the cumulative (right-hand) number out of
# checkpoints anywhere in the body. Used to find the doc's own running total
# so it can override manual-calc when the doc records more than the explicit
# set lines add up to (e.g. an implicit warmup that's not in NxDIST format).
YARD_CHECKPOINT_CAP_RE = re.compile(r"\b\d{1,5}\s*/\s*(\d{1,5})\b")
METER_CHECKPOINT_CAP_RE = re.compile(
    r"\[\s*\d[\d,]*\s*m\s*(?:/\s*(\d[\d,]*)\s*m\s*)?\]",
    re.IGNORECASE,
)
# Trailing ' - NNNN' running total at the end of a line (Rule 7).
TAIL_TOTAL_CAP_RE = re.compile(r"\s+-\s+(\d{2,5})\s*$")

# Rule 1: a standalone checkpoint line. Matches both yard format ("600/600")
# AND meter bracket format ("[1,500m]" or "[2,100m / 3,600m]"). Treating
# meter brackets as checkpoints is critical: they mark section boundaries
# in meter workouts the same way ####/#### does in yard workouts, so a
# multiplier block must end at them.
CHECKPOINT_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,5}\s*/\s*\d{1,5}"
    r"|\[\s*\d[\d,]*\s*m\s*(?:/\s*\d[\d,]*\s*m\s*)?\]"
    r")\s*$"
)

# Rule 1: a labeled sub-item line starting with "(N)" — child of a parent set.
SUBITEM_RE = re.compile(r"^\s*\(\s*\d+\s*[\.\)]?\s*\)")

# Rule 7: a trailing " - NNNN" running-total marker at the end of a line.
TAIL_MARKER_RE = re.compile(r"\s+-\s+\d{1,5}\s*$")

# Rule 2: a line that is ONLY a multiplier, e.g. "2x", "4x".
STANDALONE_MULT_RE = re.compile(r"^\s*(\d+)\s*x\s*$", re.IGNORECASE)

# Rule 3: "N rounds of" anywhere on the line (followed optionally by an inline set).
ROUNDS_OF_RE = re.compile(r"(?i)(\d+)\s+rounds?\s+of\b")

# Rule 4: an inline NxDIST pattern — e.g. "3x100", "6x50". Used with finditer to
# pick up multiple sets on one line ("1x400 + 1x200").
INLINE_SET_RE = re.compile(r"(\d+)\s*[xX]\s*(\d+)")

# Rule 6: a plain distance line — a number followed by a stroke / drill / pace word.
PLAIN_DIST_RE = re.compile(
    r"(?i)^\s*(\d{2,4})\s+("
    r"kick|swim|easy|free|fly|back|breast|stroke|pull|drill|im|build|"
    r"choice|smooth|sprint|recovery|cool|flop|soc|warmup|warm)\b"
)

# Rule 1: section-header keywords whose lines carry no distance.
HEADER_KEYWORDS = (
    "warm up", "warm-up", "warmup", "warm down", "warm-down", "warmdown",
    "pre set", "pre-set", "preset", "main set", "main work", "stroke work",
    "kick set", "cool down", "cooldown", "drill set", "pull set",
    "coach:", "coach -", "athletes:", "athletes -", "pool:", "pool -",
    "group:", "group -", "lose the fins", "add the fins", "lose fins",
    "add fins", "lose fin", "add fin",
)

# Coach name → training group. A 'Coach: <name>' line in the sub-session
# header sets the default group attribution for every set in that block.
# Multi-coach lines (e.g. 'Coach: Dave + Josh') and unrecognized names
# fall through to 'all' (unattributed). Internal section headers can
# still override per-block (see GROUP_HEADER_RE below).
COACH_NAME_RE = re.compile(r"^\s*Coach\s*[:\-]\s*(.+?)\s*$", re.IGNORECASE)
COACH_GROUP_MAP = {
    "dave": "sprint",
    "josh": "mid",
    "noah": "distance",
}

# A line that names a training group as a SECTION HEADER. Recognized forms:
#   'SPRINT: Abby (3rd effort breaststroke), Arielle, ...'  (inline, with
#       athlete/lane list trailing — typical for whole-team workouts where
#       one Main Set is split into three lane blocks)
#   'DISTANCE: Kathryn, Camille, ...'
#   'MID-DISTANCE: Bert, Lilou, ...'   (hyphen or space between mid+distance)
#   'Sprint:'  'Mid Set:'  'DISTANCE'   (bare forms)
# Anchors at line start and accepts EITHER (a) keyword + colon/hyphen,
# (b) keyword + 'Set'/'Group'/'Work'/'Swimmers' word, or (c) keyword alone
# at end of line. A set line like 'Sprint 50s' won't false-positive because
# none of those terminator branches follow the bare keyword.
# Two important regex details:
#   1. `mid(?![\s\-]+distance)` prevents the engine from backtracking to plain
#      `mid` when the `mid[\s\-]*distance` form fails its terminator check.
#      Without the lookahead, a line like 'Mid-distance + Distance Stay
#      Together' falls back to matching just 'Mid' with the '-' acting as
#      the [:\-] terminator, turning the rest of the line into bogus names.
#   2. The 'Set/Group/Work/Swimmers' branch consumes an optional trailing
#      colon (`\s*[:\-]?`), so 'Distance Group: Kathryn, ...' lands m.end()
#      after the colon and the first parsed name is 'kathryn' (not ': kathryn').
GROUP_HEADER_RE = re.compile(
    r"(?i)^\s*(sprint|mid[\s\-]*distance|distance|mid(?![\s\-]+distance))\b"
    r"(?:\s*[:\-]|\s+(?:set|sets|group|work|swimmers?)\s*[:\-]?|\s*$)"
)

# An 'Athletes: name, name, ...' header line. Used by coach_group_from_body
# as a fallback signal: if every listed athlete belongs to one group's
# roster (the rosters are built from doc-wide DISTANCE:/MID-DISTANCE:/
# SPRINT: section headers), the sub-session inherits that group.
ATHLETES_LINE_RE = re.compile(r"^\s*Athletes?\s*[:\-]\s*(.+?)\s*$", re.IGNORECASE)

# Day-of-week extractor for per-day rollups. Matches both full
# ('Wednesday, August 27, 2025') and abbreviated ('MON AM 15') headers
# at the start of a sub-session label. Returns the canonical full-name
# lowercase form ('monday'..'sunday') for grouping multiple sub-sessions
# on the same training day (e.g. 'Wednesday AM Coach: Noah' + 'Wednesday
# PM Coach: Dave' both → 'wednesday').
DAY_NAME_RE = re.compile(
    r"^(?:\w+,\s+)?(?P<day>"
    r"mon(?:day)?|tu(?:e|es|esday)?|wed(?:nesday)?|"
    r"thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
    r")\b",
    re.IGNORECASE,
)
# Sort order so the per-day rollup displays Mon → Sun.
DAY_ORDER = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 7, "unknown": 8,
}
_DAY_CANONICAL = {
    "mon": "monday", "monday": "monday",
    "tu": "tuesday", "tue": "tuesday", "tues": "tuesday", "tuesday": "tuesday",
    "wed": "wednesday", "wednesday": "wednesday",
    "thu": "thursday", "thur": "thursday", "thurs": "thursday", "thursday": "thursday",
    "fri": "friday", "friday": "friday",
    "sat": "saturday", "saturday": "saturday",
    "sun": "sunday", "sunday": "sunday",
}


def extract_day_name(header: str) -> str:
    """Return canonical day-of-week ('monday'..'sunday') for a sub-session.

    Returns 'unknown' when the header doesn't begin with a recognized day
    name (rare — usually only for select-coach placeholder rows that have
    no real header). Used to roll multiple sub-sessions on the same
    training day (AM + PM, different coaches) into a single day-level
    bucket for the per-day breakdown.
    """
    h = header.lstrip()
    m = DAY_NAME_RE.match(h)
    if not m:
        return "unknown"
    return _DAY_CANONICAL.get(m.group("day").lower(), "unknown")

# An empty per-category total dict (used as a clean accumulator). Buckets
# track sprint/mid/distance/all for yards AND meters separately so a
# workout written in meters (e.g. an LCM session with [2,100m / 3,800m]
# markers) isn't lumped into the yard totals. The 'all' bucket catches
# yardage from sets with no specified group; the invariant for every
# sub-session is sprint + mid + distance + all == total.
def empty_totals() -> Dict[str, int]:
    """Return fresh totals with sprint/mid/distance/all for both yards and meters."""
    return {
        "sprint_y": 0, "mid_y": 0, "distance_y": 0, "all_y": 0,
        "sprint_m": 0, "mid_m": 0, "distance_m": 0, "all_m": 0,
    }


def is_meter_workout(body: str) -> bool:
    """Heuristic: does this workout body indicate meters (LCM) instead of yards?"""
    return METER_BODY_RE.search(body) is not None


def extract_checkpoint_max(body: str) -> int:
    """Return the largest cumulative checkpoint value found in body.

    Treats yard-format checkpoints, meter-bracket checkpoints, and trailing
    " - NNNN" totals as the same kind of signal: the doc's own running
    total. The unit (yards vs meters) is decided once by the caller via
    is_meter_workout(), so this function just returns a magnitude.
    """
    max_v = 0
    for line in body.splitlines():
        for m in YARD_CHECKPOINT_CAP_RE.finditer(line):
            v = int(m.group(1))
            if 25 <= v <= 60_000:
                max_v = max(max_v, v)
        m = TAIL_TOTAL_CAP_RE.search(line)
        if m:
            v = int(m.group(1))
            if 25 <= v <= 60_000:
                max_v = max(max_v, v)
        for m in METER_CHECKPOINT_CAP_RE.finditer(line):
            if m.group(1):
                v = int(m.group(1).replace(",", ""))
            else:
                inner = re.search(r"\d[\d,]*", m.group(0))
                v = int(inner.group(0).replace(",", "")) if inner else 0
            if 25 <= v <= 60_000:
                max_v = max(max_v, v)
    return max_v


# ---------------------------------------------------------------------------
# Small classification helpers.
# ---------------------------------------------------------------------------

def coach_group_from_body(body: str, header: str = "",
                          rosters: Dict[str, str] = None) -> str:
    """Resolve a sub-session's default group attribution.

    Resolution order (first to produce an unambiguous single-group answer
    wins; ambiguous / multi-group signals fall through):
      1. 'Coach: NAME' line in body  (Dave→sprint, Josh→mid, Noah→distance).
      2. 'Athletes:' line in body matched against rosters (athlete names
         looked up in the season-wide roster from build_athlete_rosters).
         If every matched athlete belongs to one group, that's the group.
      3. Coach name in the header label (e.g. '...- Josh',
         'AM-LCM-Legends-Dave').
      4. 'all' (unattributed or genuinely multi-group).

    rosters=None skips step 2 (used when called without doc context).
    Word-boundary matching everywhere — 'Davenport' won't match Dave,
    'Joshua' won't match Josh, 'Noahide' won't match Noah.
    """
    # Step 1: strict body scan for 'Coach: NAME' lines.
    body_found = set()
    for line in body.splitlines()[:25]:
        m = COACH_NAME_RE.match(line)
        if not m:
            continue
        names_lower = m.group(1).lower()
        for name, group in COACH_GROUP_MAP.items():
            if re.search(rf"\b{name}\b", names_lower):
                body_found.add(group)
    if len(body_found) == 1:
        return next(iter(body_found))
    # Step 2: 'Athletes:' line lookup against rosters.
    if rosters:
        for line in body.splitlines()[:25]:
            am = ATHLETES_LINE_RE.match(line)
            if not am:
                continue
            names = parse_name_list(am.group(1))
            groups = set()
            for n in names:
                if n in rosters:
                    groups.add(rosters[n])
            if len(groups) == 1:
                return next(iter(groups))
            # Mixed-group athletes list → try next Athletes line (rare)
            # then fall through to header / 'all'.
    # Step 3: header label fallback (coach name appears as a suffix).
    if header:
        header_lower = header.lower()
        header_found = set()
        for name, group in COACH_GROUP_MAP.items():
            if re.search(rf"\b{name}\b", header_lower):
                header_found.add(group)
        if len(header_found) == 1:
            return next(iter(header_found))
    return "all"


def detect_group_header(line: str):
    """Return 'sprint'/'mid'/'distance' if line is a group section header.

    Recognizes inline athlete-list headers from whole-team workouts:
      'DISTANCE: Kathryn, Camille, ...'
      'MID-DISTANCE: Bert, Lilou, ...'
      'SPRINT: Abby (3rd effort breaststroke), ...'
    As well as bare forms ('Sprint:', 'Mid Set:', 'DISTANCE'). Per the
    user's spec, MID-DISTANCE (and MID alone) both normalize to the
    'mid' bucket — Josh's group covers both nominal categories. Returns
    None for lines that don't match the header pattern.
    """
    m = GROUP_HEADER_RE.match(line)
    if not m:
        return None
    kw = re.sub(r"[\s\-]+", " ", m.group(1).lower()).strip()
    if kw == "sprint":
        return "sprint"
    if kw == "distance":
        return "distance"
    # 'mid' alone, 'mid distance', or 'mid-distance' all map to mid.
    return "mid"


def parse_name_list(text: str) -> List[str]:
    """Split a comma-separated athlete-name list into lowercased names.

    Used for both group section headers ('DISTANCE: Kathryn, Camille...
    (10) - 5 LANES - LANES 1-5') and 'Athletes:' lines ('Athletes: Dar,
    Nik, Andrew, Lucca'). Cleanup pipeline (applied in order):
      1. Strip trailing lane/count info that runs from a dash to a LANES
         keyword to end-of-line ('- (33) 9 Lanes - Lanes 12-20').
      2. Strip trailing athlete count '(NN)'.
      3. Strip balanced '(...)' annotations — done BEFORE splitting on
         commas so that '...Carter (11, 10 once Mia is out)' doesn't get
         broken into 'Carter (11' and '10 once Mia is out)'.
      4. Split on comma; per name, strip trailing asterisks ('Andrew*'
         and 'Andrew' should resolve to the same roster key).
    """
    text = text.strip()
    # Trailing lane info — generic: 'dash, anything, LANES, anything, EOL'.
    # Catches '- 5 LANES - LANES 1-5', '- 11 LANES', '- (33) 9 Lanes - Lanes 12-20'.
    text = re.sub(r"\s*[-–—].*?\bLANES?\b.*$", "", text, flags=re.IGNORECASE)
    # Trailing athlete count '(NN)' at end (after lane strip).
    text = re.sub(r"\s*\(\d+\)\s*$", "", text)
    # Strip ALL balanced parens BEFORE comma-split, looping to handle the
    # common case of multiple annotations on one line. [^()] in the body
    # keeps the match from running past a closing paren into the next one.
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\s*\([^()]*\)", "", text)
    names = []
    for piece in text.split(","):
        piece = piece.strip()
        # Strip trailing asterisks (the doc uses * / ** as athlete annotation
        # markers — they aren't part of the name).
        piece = re.sub(r"\*+\s*$", "", piece).strip()
        if piece:
            names.append(piece.lower())
    return names


def is_likely_name(piece: str) -> bool:
    """Heuristic: does this piece look like a swimmer's name?

    Filters out non-name fragments that survive parse_name_list when a
    non-roster line happens to match GROUP_HEADER_RE — e.g.
    'Distance - Additional 1,500 Set' parses to ['additional 1',
    '500 set'], and 'SPRINT GROUP WILL GO IN HEATS on #3!!' parses to
    ['will go in heats on #...']. Real names are short, alphabetic, and
    free of digits or punctuation beyond hyphens / apostrophes / periods.
    """
    if not piece or len(piece) > 25:
        return False
    if any(c.isdigit() for c in piece):
        return False
    return bool(re.match(r"^[a-z][a-z\s\-'\.]*$", piece))


def extract_athletes_from_group_header(line: str):
    """Return (group, [athlete_names]) for a group section header line.

    Returns (None, []) if the line isn't a group section header.
    Names are lowercased; group is one of sprint/mid/distance
    (mid-distance is already normalized to 'mid' by detect_group_header).
    """
    m = GROUP_HEADER_RE.match(line)
    if not m:
        return None, []
    group = detect_group_header(line)
    rest = line[m.end():].strip()
    if not rest:
        return group, []
    return group, parse_name_list(rest)


def build_athlete_rosters(text: str) -> Dict[str, str]:
    """Scan the whole doc and return a season-wide {athlete: group} dict.

    Collects every 'DISTANCE: ...', 'MID-DISTANCE: ...', and 'SPRINT: ...'
    line in the document and assigns each named athlete to the indicated
    group. Later occurrences overwrite earlier ones — in practice
    swimmers stay in the same group across the season so this is fine.
    The is_likely_name filter discards fragments from lines that match
    GROUP_HEADER_RE but aren't actually athlete listings (e.g. section
    headers like 'Distance - Additional 1,500 Set' or 'Sprint - choice
    equipment on the 2nd round'). The returned dict drives the
    'Athletes:' line lookup in coach_group_from_body.
    """
    rosters: Dict[str, str] = {}
    for line in text.splitlines():
        group, names = extract_athletes_from_group_header(line)
        if not group:
            continue
        for name in names:
            if is_likely_name(name):
                rosters[name] = group
    return rosters


def accumulate(target: Dict[str, int], src: Dict[str, int]) -> None:
    """Add src's bucket totals into target in-place."""
    for k, v in src.items():
        target[k] = target.get(k, 0) + v


def multiply(totals: Dict[str, int], factor: int) -> None:
    """Multiply every bucket in totals by factor, in-place."""
    for k in totals:
        totals[k] *= factor


# ---------------------------------------------------------------------------
# Line-level classification.
# ---------------------------------------------------------------------------

def is_header_line(line: str) -> bool:
    """Return True if line is a section header that contributes no distance.

    Catches the explicit HEADER_KEYWORDS plus two structural patterns the
    doc uses for inline section markers without an obvious keyword:
      - short titles ending with a colon (e.g. 'Main Flow:', 'Together:')
      - short titles wrapped in hyphens (e.g. '-BODY LINE-', '-CATCH & PULL-')
    Anything with a digit is excluded so we don't false-positive set lines.
    """
    low = line.strip().lower()
    if not low:
        return True
    for kw in HEADER_KEYWORDS:
        if low.startswith(kw):
            return True
    stripped = line.strip()
    if any(c.isdigit() for c in stripped):
        return False
    if len(stripped) <= 50 and stripped.endswith(":"):
        return True
    if (len(stripped) <= 50 and stripped.startswith("-")
            and stripped.endswith("-")):
        return True
    return False


def strip_tail_marker(line: str) -> str:
    """Remove a trailing ' - NNNN' running-total marker (Rule 7)."""
    return TAIL_MARKER_RE.sub("", line)


def parse_line_distance(line: str, group: str, unit: str = "y") -> Dict[str, int]:
    """Parse one line for inline NxDIST sets or a plain distance.

    All extracted yardage on this line is attributed to a single bucket:
    f'{group}_{unit}', where group is one of sprint/mid/distance/all and
    unit is 'y' (yards) or 'm' (meters). The caller decides both: 'group'
    from the surrounding section context (coach-default with section-
    header overrides) and 'unit' once per workout from is_meter_workout().
    """
    totals = empty_totals()
    bucket = f"{group}_{unit}"
    matches = list(INLINE_SET_RE.finditer(line))
    if matches:
        for m in matches:
            reps = int(m.group(1))
            dist = int(m.group(2))
            if dist < 25:  # drill ratios like 2/2/1
                continue
            totals[bucket] += reps * dist
        return totals
    plain = PLAIN_DIST_RE.match(line)
    if plain:
        dist = int(plain.group(1))
        if dist >= 25:
            totals[bucket] += dist
    return totals


# ---------------------------------------------------------------------------
# Block / workout parsing (recursive descent).
# ---------------------------------------------------------------------------

def find_block_end(lines: List[str], start: int, hard_end: int) -> int:
    """Return the index where a multiplier/rounds-of block ends.

    A block ends at the first of: (a) a blank line, (b) a checkpoint line
    (####/####), (c) a header line. The returned index is one past the
    terminator so the caller can resume from it.
    """
    i = start
    while i < hard_end:
        line = lines[i]
        if not line.strip():
            return i + 1
        if CHECKPOINT_RE.match(line):
            return i + 1
        if is_header_line(line):
            return i
        i += 1
    return hard_end


def parse_block(lines: List[str], unit: str = "y",
                default_group: str = "all",
                start: int = 0, hard_end: int = None
                ) -> Tuple[Dict[str, int], int]:
    """Parse a slice of lines as a workout block in the given unit.

    Returns (totals, next_index).

    'unit' is 'y' (yards) or 'm' (meters); the caller picks once for the
    whole workout via is_meter_workout().

    'default_group' is the starting attribution for this block — one of
    sprint/mid/distance/all. Inside the block, a line that matches
    detect_group_header() switches the current group for the sets that
    follow; the switch is local to this block (recursive calls inherit
    the current group but section headers inside them stay scoped to
    the recursion).
    """
    if hard_end is None:
        hard_end = len(lines)
    totals = empty_totals()
    current_group = default_group
    i = start
    while i < hard_end:
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        # Group section header — update attribution. The line itself often
        # carries only athlete/lane metadata (no yardage), but if the doc
        # puts a set on the same line ('Sprint: 6x100 free') we still
        # parse any yardage that follows the keyword/delimiter so it's
        # attributed to the just-switched-to group.
        gh = detect_group_header(raw)
        if gh is not None:
            current_group = gh
            hm = GROUP_HEADER_RE.match(raw)
            rest = raw[hm.end():] if hm else ""
            rest = strip_tail_marker(rest)
            if rest.strip():
                accumulate(totals, parse_line_distance(rest, current_group, unit))
            i += 1
            continue
        if CHECKPOINT_RE.match(raw) or SUBITEM_RE.match(raw):
            i += 1
            continue
        if is_header_line(raw):
            i += 1
            continue

        line = strip_tail_marker(raw)

        # Rule 2: standalone multiplier — collect the block and recurse.
        m = STANDALONE_MULT_RE.match(line)
        if m:
            n = int(m.group(1))
            block_end = find_block_end(lines, i + 1, hard_end)
            sub, _ = parse_block(lines, unit, current_group, i + 1, block_end)
            multiply(sub, n)
            accumulate(totals, sub)
            i = block_end
            continue

        # Rule 3: "N rounds of"
        rounds = ROUNDS_OF_RE.search(line)
        if rounds:
            n = int(rounds.group(1))
            after = line[rounds.end():]
            inline = list(INLINE_SET_RE.finditer(after))
            if inline:
                line_totals = parse_line_distance(after, current_group, unit)
                multiply(line_totals, n)
                accumulate(totals, line_totals)
                i = find_block_end(lines, i + 1, hard_end)
                continue
            block_end = find_block_end(lines, i + 1, hard_end)
            sub, _ = parse_block(lines, unit, current_group, i + 1, block_end)
            multiply(sub, n)
            accumulate(totals, sub)
            i = block_end
            continue

        line_totals = parse_line_distance(line, current_group, unit)
        accumulate(totals, line_totals)
        i += 1
    return totals, i


# ---------------------------------------------------------------------------
# File-level splitting.
# ---------------------------------------------------------------------------

def split_workouts(lines: List[str]) -> List[Tuple[int, str, List[str]]]:
    """Split file lines into (week_num, header, body_lines) tuples.

    A new workout begins at:
      - a "Week N" header line (resets the week number),
      - a date header line (anchored weekday + year OR weekday + "(week N)"),
      - a Coach: line under an existing date header (parallel sub-workout),
      - an underscore separator under an existing date header.

    The current week number is carried with each workout so the reporter
    can group by chronological Week 1..10.
    """
    workouts: List[Tuple[int, str, List[str]]] = []
    current_label: str = ""
    current_date: str = ""
    current_body: List[str] = []
    current_week: int = 0
    body_has_workout_content = False

    def flush() -> None:
        """Append the in-progress workout if it has any non-blank content."""
        if current_label and any(ln.strip() for ln in current_body):
            workouts.append((current_week, current_label, list(current_body)))

    for line in lines:
        wm = WEEK_HEADER_RE.match(line)
        if wm:
            flush()
            current_week = int(wm.group(1))
            current_label = ""
            current_date = ""
            current_body = []
            body_has_workout_content = False
            continue
        if DATE_RE.search(line):
            flush()
            current_date = line.strip()
            current_label = current_date
            current_body = []
            body_has_workout_content = False
            continue
        if DAY_TIME_RE.match(line):
            # "Monday AM" / "Tuesday PM" style header — also a workout
            # boundary. Use it as the date label since the doc doesn't give
            # a more specific one in late-season weeks.
            flush()
            current_date = line.strip()
            current_label = current_date
            current_body = []
            body_has_workout_content = False
            continue
        if (COACH_LINE_SPLIT_RE.match(line)
                and body_has_workout_content
                and current_date):
            flush()
            current_label = f"{current_date}  —  {line.strip()}"
            current_body = [line]
            body_has_workout_content = False
            continue
        if (UNDERSCORE_SPLIT_RE.match(line)
                and body_has_workout_content
                and current_date):
            flush()
            current_label = f"{current_date}  —  (next block)"
            current_body = []
            body_has_workout_content = False
            continue
        current_body.append(line)
        if line.strip():
            body_has_workout_content = True
    flush()
    return workouts


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------

def _yard_total(t: Dict[str, int]) -> int:
    """Sum the yards-suffixed buckets (including all_y)."""
    return t["sprint_y"] + t["mid_y"] + t["distance_y"] + t["all_y"]


def _meter_total(t: Dict[str, int]) -> int:
    """Sum the meters-suffixed buckets (including all_m)."""
    return t["sprint_m"] + t["mid_m"] + t["distance_m"] + t["all_m"]


def _assign_gap(totals: Dict[str, int], unit: str, gap: int,
                default_group: str) -> None:
    """Add gap yardage to the sub-session's default-group bucket.

    When the doc's checkpoint exceeds what manual-calc derived, the
    missing yardage is real (typically an implicit warmup that wasn't
    written as NxDIST). Attribute it to the sub-session's coach-implied
    default group so the per-bucket numbers still sum to the workout
    total. If default_group is 'all' (unattributed sub-session), the gap
    stays in 'all'.
    """
    bucket = f"{default_group}_{unit}"
    totals[bucket] = totals.get(bucket, 0) + gap


def print_workout(date: str, totals: Dict[str, int], method: str = "manual",
                  default_group: str = "all") -> None:
    """Print one workout's breakdown: yards column, then meters column if any.

    Shows the four group buckets (Sprint / Mid / Distance / All) which by
    construction sum to the workout total. The default_group is shown next
    to the header so the reader sees which coach drove the attribution.
    """
    y_total = _yard_total(totals)
    m_total = _meter_total(totals)
    if y_total == 0 and m_total == 0:
        return
    tag = "  [checkpoint]" if method == "checkpoint" else "  [manual calc]"
    group_label = f"  (default: {default_group})"
    print(date + tag + group_label)
    print(f"  Total:      {y_total:5} yd"
          + (f"  /  {m_total:5} m" if m_total else ""))
    rows = [
        ("Sprint:    ", "sprint"),
        ("Mid:       ", "mid"),
        ("Distance:  ", "distance"),
        ("All:       ", "all"),
    ]
    for label, key in rows:
        y, m = totals[f"{key}_y"], totals[f"{key}_m"]
        if y == 0 and m == 0:
            continue
        y_pct = (y / y_total * 100) if y_total else 0
        m_pct = (m / m_total * 100) if m_total else 0
        line = f"  {label} {y:5} yd ({y_pct:4.1f}%)"
        if m_total:
            line += f"  /  {m:5} m ({m_pct:4.1f}%)"
        print(line)
    print()


def _print_bucket_breakdown(totals: Dict[str, int]) -> None:
    """Print the Total + Sprint/Mid/Distance/All rows for a totals dict.

    Shared between per-week subtotals and the grand total so the format
    stays consistent. Skips zero-only rows except for the Total line.
    """
    y_total = _yard_total(totals)
    m_total = _meter_total(totals)
    print(f"  Total:      {y_total:5} yd"
          + (f"  /  {m_total:5} m" if m_total else ""))
    rows = [("Sprint:    ", "sprint"),
            ("Mid:       ", "mid"),
            ("Distance:  ", "distance"),
            ("All:       ", "all")]
    for label, key in rows:
        y, m = totals[f"{key}_y"], totals[f"{key}_m"]
        y_pct = (y / y_total * 100) if y_total else 0
        m_pct = (m / m_total * 100) if m_total else 0
        line = f"  {label} {y:5} yd ({y_pct:4.1f}%)"
        if m_total:
            line += f"  /  {m:5} m ({m_pct:4.1f}%)"
        print(line)


def print_week_subtotal(week_num: int, totals: Dict[str, int]) -> None:
    """Print a per-week subtotal block with the 4-bucket breakdown."""
    print(f"---- Week {week_num} subtotal ----")
    _print_bucket_breakdown(totals)
    print()


def print_grand_total(totals: Dict[str, int], workouts_parsed: int) -> None:
    """Print the across-all-workouts grand total with all 4 group buckets."""
    print("GRAND TOTAL")
    _print_bucket_breakdown(totals)
    print(f"  Workouts parsed:   {workouts_parsed}")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def compute_workout_totals(text: str,
                           week_filter: Optional[int] = None,
                           dedupe: bool = True) -> Dict:
    """Parse workout text and return structured results.

    This is the shared entry point used by both the CLI (main()) and the
    Streamlit web app. It performs the full pipeline:
      1. Split into workout blocks.
      2. Build a season-wide athlete → group roster from the doc.
      3. For each workout: parse, apply checkpoint MAX fallback, attribute
         to a group via Coach line / Athletes line / header label.
      4. (optional) Dedupe sub-sessions whose normalized body has been
         seen earlier in the same run — catches paste artifacts in the
         source Doc where a coach pasted the same workout block into
         two different weeks (e.g. Week 14 Saturday = Week 15 Saturday).
      5. Tally per-week subtotals and a grand total.

    Returns a dict shaped:
        {
            "workouts": [
                {"week": int, "header": str, "method": "manual"|"checkpoint",
                 "default_group": str, "totals": Dict[str, int]},
                ...
            ],
            "weekly_subtotals": {week_num: totals_dict},
            "grand_total": totals_dict,
            "workouts_parsed": int,
            "deduped": [
                {"week": int, "header": str, "yards": int},
                ...   # workouts skipped as duplicates of an earlier one
            ],
        }

    'week_filter' (1..N) optionally restricts to a single week. Workouts
    producing zero yardage are excluded from the returned list.

    'dedupe=True' (default) drops sub-sessions whose normalized body has
    already been parsed. Set to False to keep raw output for auditing.
    """
    lines = text.splitlines()
    workouts = split_workouts(lines)
    rosters = build_athlete_rosters(text)

    weekly_by_num: Dict[int, Dict[str, int]] = {}
    daily_by_key: Dict[Tuple[int, str], Dict[str, int]] = {}
    grand = empty_totals()
    workout_records: List[Dict] = []
    deduped: List[Dict] = []
    seen_body_hashes: set = set()
    parsed = 0

    for week_num, header, body in workouts:
        if week_filter is not None and week_num != week_filter:
            continue
        body_text = "\n".join(body)
        unit = "m" if is_meter_workout(body_text) else "y"
        default_group = coach_group_from_body(body_text, header, rosters)
        manual, _ = parse_block(body, unit=unit, default_group=default_group)

        cp_max = extract_checkpoint_max(body_text)
        manual_total = _yard_total(manual) if unit == "y" else _meter_total(manual)
        method = "manual"
        if cp_max > manual_total:
            _assign_gap(manual, unit, cp_max - manual_total, default_group)
            method = "checkpoint"
        y_total = _yard_total(manual)
        m_total = _meter_total(manual)
        if y_total == 0 and m_total == 0:
            continue

        # Dedupe by normalized body hash. Two sub-sessions whose entire
        # body text (stripped, lowercased, blank lines dropped) matches
        # are treated as the same physical workout pasted twice in the
        # source. The first occurrence wins; subsequent ones are recorded
        # in `deduped` for transparency but excluded from totals.
        if dedupe:
            norm = '\n'.join(ln.strip().lower() for ln in body if ln.strip())
            if len(norm) >= 100:
                h = md5(norm.encode()).hexdigest()
                if h in seen_body_hashes:
                    deduped.append({
                        "week": week_num,
                        "header": header,
                        "yards": y_total + m_total,
                    })
                    continue
                seen_body_hashes.add(h)

        day_name = extract_day_name(header)
        workout_records.append({
            "week": week_num,
            "day": day_name,
            "header": header,
            "method": method,
            "default_group": default_group,
            "totals": manual,
        })
        accumulate(grand, manual)
        weekly_by_num.setdefault(week_num, empty_totals())
        accumulate(weekly_by_num[week_num], manual)
        day_key = (week_num, day_name)
        daily_by_key.setdefault(day_key, empty_totals())
        accumulate(daily_by_key[day_key], manual)
        parsed += 1

    return {
        "workouts": workout_records,
        "weekly_subtotals": weekly_by_num,
        "daily_subtotals": daily_by_key,
        "grand_total": grand,
        "workouts_parsed": parsed,
        "deduped": deduped,
    }


def parse_args() -> argparse.Namespace:
    """Build the argparse parser and return parsed args."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("input", type=Path, help="Path to the workouts text file.")
    p.add_argument("--week", type=int, default=None,
                   help="Show only this Week N (chronological 1..10).")
    return p.parse_args()


def main() -> int:
    """Run the parser end-to-end and return an exit code.

    Thin CLI wrapper: reads input, calls compute_workout_totals(), then
    streams the existing print_workout / print_week_subtotal /
    print_grand_total renderers in week-by-week order. All parsing logic
    lives in compute_workout_totals so the Streamlit app shares it.
    """
    args = parse_args()
    if not args.input.is_file():
        print(f"File not found: {args.input}", file=sys.stderr)
        return 1
    # utf-8-sig (not plain utf-8) transparently strips a leading BOM if the
    # file was saved by an editor that adds one (Google Docs export does on
    # some systems). Without this, a BOM would prefix the first line and a
    # 'Week 11' header at the top of the file fails to match WEEK_HEADER_RE.
    text = args.input.read_text(encoding="utf-8-sig")
    results = compute_workout_totals(text, week_filter=args.week)
    if not results["workouts"]:
        print("No workouts found.", file=sys.stderr)
        return 2

    last_week = None
    weekly_subtotals = results["weekly_subtotals"]
    for w in results["workouts"]:
        if w["week"] != last_week:
            # Flush the previous week's subtotal before starting a new
            # week's section, so each week's bucket split is printed
            # right next to the workouts that produced it.
            if last_week is not None and last_week in weekly_subtotals:
                print_week_subtotal(last_week, weekly_subtotals[last_week])
            print(f"================  Week {w['week']}  ================\n")
            last_week = w["week"]
        print_workout(w["header"], w["totals"], method=w["method"],
                      default_group=w["default_group"])

    # Flush the final week's subtotal (the loop only flushes on transition).
    if last_week is not None and last_week in weekly_subtotals:
        print_week_subtotal(last_week, weekly_subtotals[last_week])

    print_grand_total(results["grand_total"], results["workouts_parsed"])

    # Report any auto-deduped sub-sessions so the user can verify nothing
    # important was suppressed. Each entry was an exact body match of an
    # earlier sub-session in the same run (paste artifact in the source).
    if results.get("deduped"):
        total_y = sum(d["yards"] for d in results["deduped"])
        print(f"\n[Auto-dedupe] Skipped {len(results['deduped'])} duplicate "
              f"sub-session(s) totaling {total_y:,} yd/m "
              f"(exact body matches of earlier workouts in the doc).")
        for d in results["deduped"]:
            print(f"  - Week {d['week']}: {d['header'][:80]} ({d['yards']:,} yd)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
