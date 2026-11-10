"""Split each scraped hearing into cases and save them to hearings.db.

Usage:
    python -m dmr_hearings.pipeline.cases                          # process every scraped hearing and print a report
    python -m dmr_hearings.pipeline.cases --video m2-k83Vx4XE -v   # one hearing, listing every case found
"""

import argparse
import bisect
import re
from dataclasses import dataclass

from .. import db
from ..util import fmt

# "case number 33087", "case 33087", "case no. 33087", "cases numbers 31968"
CASE_RE = re.compile(r"\bcases?\s+(?:(?:numbers?|no\.?|num)\s+)?(?=\d)", re.I)

# One case number, possibly split by spaces in the captions ("305 39").
NUM_RE = re.compile(r"\d+(?: \d+)*")

# Words that join case numbers heard together ("31968 and 31969", "31968, case number 31969").
JOIN_RE = re.compile(r"\s*(?:,|and|&)\s*(?:cases?\s+)?(?:(?:numbers?|no\.?)\s+)?(?=\d)", re.I)

# Status read from the docket list: "is continued", "will be dismissed", or just "dismissed".
STATUS_RE = re.compile(
    r"\.?\s*(?:(?:is|are|was|were|will be|has been|have been|being)\s+)?"
    r"(continued|dismissed|withdrawn)\b",
    re.I,
)

# End of a case: "concludes our hearing for case number", "close the hearing in case",
# "conclude the excuse me, the hearing for uh case number", "close the record on case number",
# "the record will be closed on case number", "that ends our hearing for case number".
CONCLUDE_RE = re.compile(
    r"(?:\b(?:conclud|clos|end)\w*\s+[^.?!]{0,60}?(?:hearing|record)s?"
    r"|\brecord\s+(?:will be|is|has been)\s+(?:now\s+)?closed)"
    r"\s+(?:\w+\s+){0,2}?(?:in|for|on|of)\s+(?:the\s+)?"
    r"(?:(?:uh|um),?\s+)?(?:cases?\s+)?(?:(?:numbers?|no\.?)\s+)?(?=\d)",
    re.I,
)

# Hearings that are one long case name it at the start instead of concluding it:
# "on the record in case number 29227", "case numbers 30869 through 30880".
OPENING_RE = re.compile(r"\bon the record\b", re.I)
RANGE_RE = re.compile(
    r"\bcases?\s+(?:numbers?\s+)?(\d{5}),?\s+(?:through|thru)\s+(?:case\s+)?(?:number\s+)?(\d{5})\b", re.I
)
CONSOLIDATED_RE = re.compile(r"\bconsolidat", re.I)

# Case numbers read out digit by digit: "case number two eight nine zero seven".
DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
_DIGIT = "(?:" + "|".join(DIGIT_WORDS) + ")"
SPOKEN_RE = re.compile(rf"\b{_DIGIT}(?:[\s,-]+{_DIGIT}){{4,}}\b", re.I)

# Phrases that introduce a case just before its number is read.
INTRO_RE = re.compile(r"\b(?:next|first|last)\s+(?:case|matter)\b|\bbegin with\b|\bmoving to\b", re.I)

# Caption tags like [Applause] and speaker-change markers.
TAG_RE = re.compile(r"\[[^\]]*\]|>>")


@dataclass
class Mention:
    pos: int  # character position in the joined text
    ms: int  # timestamp in the video
    raw: str  # digits as captioned, spaces removed
    kind: str = "mention"  # mention, status, conclude
    status: str | None = None
    number: str | None = None
    repaired: bool = False


class Transcript:
    """All caption lines of a hearing joined into one string, with a map from position back to time."""

    def __init__(self, segments: list[tuple[int, str]]):
        parts, self.offsets, self.times = [], [], []
        pos = 0
        for start_ms, text in segments:
            text = re.sub(r"\s+", " ", TAG_RE.sub(" ", text)).strip()
            if not text:
                continue
            self.offsets.append(pos)
            self.times.append(start_ms)
            parts.append(text)
            pos += len(text) + 1
        self.text = " ".join(parts)
        self._convert_spoken_numbers()

    def _convert_spoken_numbers(self) -> None:
        """Replace "two eight nine zero seven" with "28907", and fix the position map to match."""
        pieces, edits, last = [], [], 0
        for m in SPOKEN_RE.finditer(self.text):
            digits = "".join(DIGIT_WORDS[w] for w in re.findall(r"[a-z]+", m.group().lower()))
            groups = [digits[i : i + 5] for i in range(0, len(digits), 5)]
            # Only whole 5-digit case numbers, which start with 2 or 3 in these years.
            if len(digits) % 5 or not all(g[0] in "23" for g in groups):
                continue
            replacement = ", ".join(groups)
            pieces += [self.text[last : m.start()], replacement]
            edits.append((m.start(), m.end(), len(replacement) - len(m.group())))
            last = m.end()
        if not edits:
            return
        pieces.append(self.text[last:])
        self.text = "".join(pieces)

        new_offsets = []
        for offset in self.offsets:
            shift = 0
            for start, end, delta in edits:
                if offset >= end:
                    shift += delta
                elif offset > start:  # line started inside a replaced number
                    offset = start
                    break
                else:
                    break
            new_offsets.append(offset + shift)
        self.offsets = new_offsets

    def ms_at(self, pos: int) -> int:
        i = bisect.bisect_right(self.offsets, pos) - 1
        return self.times[max(i, 0)]

    def context(self, pos: int, width: int = 80) -> str:
        return self.text[max(pos - width, 0) : pos + width]


def read_chain(text: str, pos: int) -> tuple[list[tuple[int, str]], int]:
    """Read one or more joined case numbers starting at pos. Returns [(position, digits)] and the end position."""
    numbers = []
    while m := NUM_RE.match(text, pos):
        numbers.append((m.start(), merge_digits(m.group())))
        pos = m.end()
        join = JOIN_RE.match(text, pos)
        if not join:
            break
        pos = join.end()
    return numbers, pos


def merge_digits(group: str) -> str:
    """Rejoin a number the captions split with spaces ("305 39" -> "30539") without swallowing the next number."""
    pieces = group.split(" ")
    digits = pieces[0]
    for piece in pieces[1:]:
        # Stop at 6 digits: one extra digit can still be repaired, two numbers run together can't.
        if len(digits) >= 5 or len(digits + piece) > 6:
            break
        digits += piece
    return digits


def find_mentions(t: Transcript) -> list[Mention]:
    mentions: dict[int, Mention] = {}

    for m in CASE_RE.finditer(t.text):
        chain, end = read_chain(t.text, m.end())
        status = STATUS_RE.match(t.text, end)
        for pos, digits in chain:
            if pos in mentions:
                continue
            mention = Mention(pos, t.ms_at(pos), digits)
            if status:
                mention.kind, mention.status = "status", status.group(1).lower()
            mentions[pos] = mention

    for m in CONCLUDE_RE.finditer(t.text):
        chain, _ = read_chain(t.text, m.end())
        for pos, digits in chain:
            mention = mentions.setdefault(pos, Mention(pos, t.ms_at(pos), digits))
            mention.kind = "conclude"

    return sorted(mentions.values(), key=lambda x: x.pos)


def resolve_numbers(mentions: list[Mention], docket: list[str]) -> None:
    """Set each mention's case number, repairing garbled ones where there is exactly one sensible fix.

    A repair is matched against, in order:
    1. the official docket for this hearing date, if there is one
    2. clean numbers said elsewhere in the same hearing
    3. the hearing's number range, since case numbers are assigned in order
    """
    known = sorted({m.raw for m in mentions if len(m.raw) == 5} | set(docket))
    if not known:
        return
    median = int(known[len(known) // 2])
    nearby = {str(n) for n in range(median - 500, median + 500)}
    pools = [set(docket), set(known), nearby] if docket else [set(known), nearby]

    for m in mentions:
        if len(m.raw) == 5:
            m.number = m.raw
            continue
        for pool in pools:
            candidates = repairs(m.raw, pool)
            if candidates:
                break
        if len(candidates) == 1:
            m.number, m.repaired = candidates.pop(), True


def repairs(raw: str, pool: set[str]) -> set[str]:
    """Numbers in pool that differ from raw by one extra or one missing digit."""
    if len(raw) == 6:  # extra digit: "313000" -> "31300"
        return {raw[:i] + raw[i + 1 :] for i in range(6)} & pool
    if len(raw) == 4:  # missing digit: "3310" -> "33100"
        return {k for k in pool if any(k[:i] + k[i + 1 :] == raw for i in range(5))}
    return set()


@dataclass
class Case:
    number: str
    status: str
    start_ms: int | None
    end_ms: int | None
    confidence: str


def build_cases(
    t: Transcript, mentions: list[Mention], docket: list[str], own_docket: list[tuple[str, str]]
) -> tuple[list[Case], list[str]]:
    """Turn mentions into cases. Returns the cases and a list of problems for the report."""
    problems = []
    on_docket = set(docket)

    # Group conclusion mentions that belong to the same sentence ("31968 and 31969").
    conclusions: list[tuple[int, set[str], list[Mention]]] = []
    for m in (m for m in mentions if m.kind == "conclude"):
        if conclusions and m.pos - conclusions[-1][2][-1].pos < 40:
            conclusions[-1][2].append(m)
        else:
            conclusions.append((m.pos, set(), [m]))
    conclusions = [(pos, {m.number for m in group if m.number}, group) for pos, _, group in conclusions]

    concluded = {n for _, numbers, _ in conclusions for n in numbers}

    cases: list[Case] = []
    prev_end_pos = 0
    prev_numbers: set[str] = set()
    for pos, numbers, group in conclusions:
        inferred = False
        if not numbers:
            numbers = guess_conclusion(group[0].raw, mentions, concluded, docket, prev_end_pos, pos, prev_numbers)
            if not numbers:
                problems.append(f"{fmt(t.ms_at(pos))} conclusion with unreadable case number: '{group[0].raw}'")
                continue
            inferred = True
            problems.append(
                f"{fmt(t.ms_at(pos))} unreadable conclusion '{group[0].raw}'; guessed {next(iter(numbers))}"
            )

        # The case starts at its first mention after the previous case ended.
        starts = [m for m in mentions if m.kind == "mention" and m.number in numbers and prev_end_pos < m.pos < pos]
        if starts:
            start_pos = starts[0].pos
            intro = [i for i in INTRO_RE.finditer(t.text, max(start_pos - 150, 0), start_pos)]
            if intro:
                start_pos = intro[-1].start()
            confidence = "high"
        else:
            start_pos = prev_end_pos
            confidence = "low"
            problems.append(
                f"{fmt(t.ms_at(pos))} no start found for {', '.join(sorted(numbers))}; using end of previous case"
            )
        if inferred or any(m.repaired for m in group):
            confidence = "low"

        if on_docket and not numbers <= on_docket:
            confidence = "low"
            problems.append(f"{fmt(t.ms_at(pos))} not on docket: {', '.join(sorted(numbers - on_docket))}")

        start_ms, end_ms = t.ms_at(start_pos), t.ms_at(pos)
        for number in sorted(numbers):
            cases.append(Case(number, "heard", start_ms, end_ms, confidence))
        prev_end_pos, prev_numbers = pos, numbers

        # Cases with no opposition are often called and closed in seconds, so only flag long ones.
        if end_ms - start_ms > 4 * 3_600_000:
            problems.append(f"{fmt(start_ms)}-{fmt(end_ms)} over 4 hours for {', '.join(sorted(numbers))}")

    # A hearing can be concluded twice (for example after a break). Keep the earliest start and latest end.
    merged: dict[str, Case] = {}
    for case in cases:
        if case.number in merged:
            existing = merged[case.number]
            existing.start_ms = min(existing.start_ms, case.start_ms)
            existing.end_ms = max(existing.end_ms, case.end_ms)
            if "low" in (existing.confidence, case.confidence):
                existing.confidence = "low"
        else:
            merged[case.number] = case

    # No case was ever concluded: this is probably one long hearing for one case or a
    # consolidated group of cases, so the whole video belongs to them.
    if not merged and t.times:
        numbers, confidence, reason = whole_hearing_cases(t, mentions, own_docket)
        for number in sorted(numbers):
            merged[number] = Case(number, "heard", 0, t.times[-1], confidence)
        if numbers:
            problems.append(f"no cases concluded; whole hearing is {', '.join(sorted(numbers))} ({reason})")

    for m in mentions:
        if m.kind == "status" and m.number and m.number not in merged:
            merged[m.number] = Case(m.number, m.status, None, None, "low" if m.repaired else "high")

    unresolved = [m.raw for m in mentions if not m.number]
    if unresolved:
        problems.append(f"unreadable case numbers: {', '.join(unresolved)}")

    return sorted(merged.values(), key=lambda c: (c.start_ms is None, c.start_ms or 0, c.number)), problems


def guess_conclusion(
    raw: str,
    mentions: list[Mention],
    concluded: set[str],
    docket: list[str],
    after_pos: int,
    before_pos: int,
    prev_numbers: set[str],
) -> set[str]:
    """Guess the case for a conclusion whose number the captions garbled. Returns an empty set if there's no good guess."""
    if len(raw) < 3:
        return set()  # one or two digits could be almost any case
    prefix = raw[:3]

    def fits(number: str) -> bool:
        return number.startswith(prefix) and number not in concluded and (not docket or number in docket)

    # 1. The number said most since the previous case ended.
    said = [
        m.number
        for m in mentions
        if m.kind == "mention" and m.number and fits(m.number) and after_pos < m.pos < before_pos
    ]
    if said:
        return {max(set(said), key=said.count)}

    # 2. The next case on the docket after the previous heard case.
    if docket and prev_numbers & set(docket):
        last = max(docket.index(n) for n in prev_numbers if n in docket)
        for number in docket[last + 1 :]:
            if fits(number):
                return {number}
    return set()


def whole_hearing_cases(
    t: Transcript, mentions: list[Mention], own_docket: list[tuple[str, str]]
) -> tuple[set[str], str, str]:
    """Find the case(s) a hearing is entirely about. Returns (numbers, confidence, reason)."""
    # 1. Named right after going on the record.
    opening = OPENING_RE.search(t.text)
    start = opening.start() if opening else 0
    end = start + 600
    numbers = {m.number for m in mentions if m.kind != "status" and m.number and start <= m.pos < end}
    for r in RANGE_RE.finditer(t.text, start, end):
        first, last = int(r.group(1)), int(r.group(2))
        if 0 < last - first <= 50:
            numbers |= {str(n) for n in range(first, last + 1)}
    main_docket = [number for number, section in own_docket if section != "administrative"]
    # "there are four cases in the docket which will be consolidated ... we'll begin with case number 30122"
    if CONSOLIDATED_RE.search(t.text, start, end) and 1 < len(main_docket) <= 12:
        return numbers | set(main_docket), "high", "consolidated at the opening"
    if numbers:
        return numbers, "high", "named at the opening"

    # 2. The only case on this day's docket.
    if len(main_docket) == 1:
        return set(main_docket), "high", "only case on the docket"

    # 3. A small docket heard as one consolidated hearing.
    if 1 < len(main_docket) <= 12 and CONSOLIDATED_RE.search(t.text):
        return set(main_docket), "low", "consolidated hearing of the whole docket"

    return set(), "", ""


def split_hearing(
    segments: list[tuple[int, str]], docket: list[str] | None = None, own_docket: list[tuple[str, str]] | None = None
) -> tuple[list[Case], list[Mention], list[str], Transcript]:
    """docket: case numbers from this and nearby days' dockets, this day's first, in docket order.
    own_docket: (case_number, section) for this day's docket only."""
    docket = docket or []
    t = Transcript(segments)
    mentions = find_mentions(t)
    resolve_numbers(mentions, docket)
    cases, problems = build_cases(t, mentions, docket, own_docket or [])
    return cases, mentions, problems, t


def process_video(conn, video_id: str, hearing_date: str | None) -> tuple[list[Case], list[str]]:
    segments = conn.execute(
        "SELECT start_ms, text FROM segments WHERE video_id = ? ORDER BY start_ms", (video_id,)
    ).fetchall()
    # Hearings often run over two or three days with a docket for each day, and a case
    # can be heard on a different day than its docket. Use this day's docket first,
    # then the dockets from two days either side.
    docket = [
        row["case_number"]
        for row in conn.execute(
            """SELECT case_number FROM docket_cases
           WHERE hearing_date BETWEEN date(?, '-2 days') AND date(?, '+2 days')
           ORDER BY hearing_date != ?, hearing_date, position""",
            (hearing_date, hearing_date, hearing_date),
        )
    ]
    own_docket = [
        (row["case_number"], row["section"])
        for row in conn.execute(
            "SELECT case_number, section FROM docket_cases WHERE hearing_date = ? ORDER BY position", (hearing_date,)
        )
    ]
    cases, mentions, problems, t = split_hearing([(s["start_ms"], s["text"]) for s in segments], docket, own_docket)
    if not docket:
        problems.insert(0, "no docket for this date; numbers checked against the transcript only")

    # A day with no docket and no cases found, right after a day that was one long hearing,
    # is that hearing continuing (for example 6/11/2024 continuing on 6/12/2024).
    if segments and not own_docket and not any(c.status == "heard" for c in cases):
        previous = [
            row["case_number"]
            for row in conn.execute(
                """SELECT c.case_number FROM cases c JOIN videos v USING (video_id)
               WHERE v.video_id = (
                   SELECT v2.video_id FROM videos v2
                   WHERE v2.hearing_date BETWEEN date(?, '-2 days') AND ? AND v2.video_id != ?
                     AND EXISTS (SELECT 1 FROM cases c2 WHERE c2.video_id = v2.video_id AND c2.status = 'heard')
                   ORDER BY v2.hearing_date DESC LIMIT 1)
                 AND c.status = 'heard' AND c.start_ms = 0""",
                (hearing_date, hearing_date, video_id),
            )
        ]
        heard_elsewhere = conn.execute(
            "SELECT COUNT(*) FROM cases c JOIN videos v USING (video_id) WHERE v.hearing_date BETWEEN date(?, '-2 days') AND ? "
            "AND v.video_id != ? AND c.status = 'heard' AND c.start_ms != 0",
            (hearing_date, hearing_date, video_id),
        ).fetchone()[0]
        if previous and not heard_elsewhere:
            end_ms = segments[-1]["start_ms"]
            cases = [c for c in cases if c.number not in previous]
            cases += [Case(number, "heard", 0, end_ms, "low") for number in previous]
            problems.append(f"no cases found; continuing the previous day's hearing of {', '.join(previous)}")

    # Replace this hearing's old results in one transaction.
    with conn:
        conn.execute("DELETE FROM cases WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM case_mentions WHERE video_id = ?", (video_id,))
        conn.executemany(
            "INSERT INTO cases (video_id, case_number, status, start_ms, end_ms, confidence) VALUES (?, ?, ?, ?, ?, ?)",
            [(video_id, c.number, c.status, c.start_ms, c.end_ms, c.confidence) for c in cases],
        )
        conn.executemany(
            "INSERT INTO case_mentions (video_id, case_number, raw, kind, start_ms, context) VALUES (?, ?, ?, ?, ?, ?)",
            [(video_id, m.number, m.raw, m.kind, m.ms, t.context(m.pos)) for m in mentions],
        )
    return cases, problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", help="only process this video ID")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every case found")
    args = parser.parse_args()

    conn = db.connect()
    query = "SELECT video_id, title, hearing_date FROM videos WHERE status = 'done'"
    params: tuple = ()
    if args.video:
        query += " AND video_id = ?"
        params = (args.video,)
    # Oldest first, so a hearing that continues the day before can see that day's cases.
    videos = conn.execute(query + " ORDER BY hearing_date, title", params).fetchall()

    totals = {"heard": 0, "other": 0, "low": 0, "problems": 0}
    for video in videos:
        cases, problems = process_video(conn, video["video_id"], video["hearing_date"])
        heard = [c for c in cases if c.status == "heard"]
        low = [c for c in heard if c.confidence == "low"]
        totals["heard"] += len(heard)
        totals["other"] += len(cases) - len(heard)
        totals["low"] += len(low)
        totals["problems"] += len(problems)

        print(
            f"{video['title']} ({video['video_id']}): {len(heard)} heard, "
            f"{len(cases) - len(heard)} continued/dismissed, {len(low)} low confidence"
        )
        if args.verbose:
            for c in cases:
                span = f"{fmt(c.start_ms):>8} - {fmt(c.end_ms):<8}" if c.start_ms is not None else " " * 19
                print(f"    {c.number}  {c.status:<10} {span} {c.confidence}")
        for p in problems:
            print(f"    ! {p}")

    print(
        f"\n{len(videos)} hearings: {totals['heard']} cases heard ({totals['low']} low confidence), "
        f"{totals['other']} continued/dismissed, {totals['problems']} problems"
    )
    conn.close()


if __name__ == "__main__":
    main()
