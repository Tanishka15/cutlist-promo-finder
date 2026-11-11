"""Lossless timed-block parser; all arithmetic uses integer centiseconds."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import re

TIME = r"\d{2,}:\d{2}\.\d{2}"
SCENE = re.compile(r"^SCENE\s+(\d+)\s*$", re.M | re.I)
BLOCK = re.compile(
    rf"^\*\*(?P<label>[^\n]+?):\*\*[ \t]*\r?\n"
    rf"[ \t]*Start:[ \t]*\((?P<start>{TIME})\)[ \t]*\|[ \t]*End:[ \t]*\((?P<end>{TIME})\)[ \t]*\r?\n"
    r"(?P<body>.*?)(?=^\*\*[^\n]+?:\*\*|^SCENE\s+\d+\s*$|^---\s*SCRIPT END\s*---|\Z)",
    re.M | re.I | re.S,
)


class ParseError(ValueError):
    pass


def to_centiseconds(timestamp: str) -> int:
    if not isinstance(timestamp, str) or not re.fullmatch(TIME, timestamp):
        raise ParseError(f"Invalid timestamp: {timestamp!r}; expected MM:SS.cc")
    minutes, rest = timestamp.split(":")
    seconds, hundredths = rest.split(".")
    if int(seconds) >= 60:
        raise ParseError(f"Seconds out of range: {timestamp}")
    return (int(minutes) * 60 + int(seconds)) * 100 + int(hundredths)


def format_timestamp(cs: int) -> str:
    if not isinstance(cs, int) or isinstance(cs, bool) or cs < 0:
        raise ValueError("Timestamp must be a nonnegative integer centisecond value")
    minutes, rem = divmod(cs, 6000)
    seconds, hundredths = divmod(rem, 100)
    return f"{minutes:02}:{seconds:02}.{hundredths:02}"


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    start: str
    end: str
    start_cs: int
    end_cs: int
    original_text: str
    label: str
    scene_id: str


@dataclass(frozen=True)
class Scene:
    id: str
    heading: str
    start: str
    end: str
    start_cs: int
    end_cs: int


@dataclass
class Episode:
    number: int
    events: list[Event]
    scenes: list[Scene]
    source_name: str
    metadata: dict

    @property
    def end_cs(self) -> int:
        return max([e.end_cs for e in self.events] + [s.end_cs for s in self.scenes])

    def to_dict(self) -> dict:
        return asdict(self)


def parse_text(text: str, source_name: str = "<memory>") -> Episode:
    number_match = re.search(r"^EPISODE NUMBER:\s*(?:Episode\s*)?(\d+)\s*$", text, re.M | re.I)
    if not number_match:
        raise ParseError(f"{source_name}: missing EPISODE NUMBER metadata")
    number = int(number_match[1])
    metadata = {m[1].lower().replace(" ", "_"): m[2] for m in re.finditer(
        r"^(EPISODE LOGLINE|CONTINUITY NOTES):[ \t]*(.*)$", text, re.M | re.I)}
    scene_matches = list(SCENE.finditer(text))
    scenes, ranges = [], []
    for i, match in enumerate(scene_matches):
        stop = scene_matches[i + 1].start() if i + 1 < len(scene_matches) else len(text)
        header = text[match.end():stop].split("**", 1)[0]
        values = {}
        for key in ("heading", "start", "end"):
            found = re.search(rf"^SCENE {key}:[ \t]*(.+?)[ \t]*$", header, re.M | re.I)
            if not found:
                raise ParseError(f"{source_name}: SCENE {match[1]} missing {key}")
            values[key] = found[1]
        start_cs, end_cs = to_centiseconds(values["start"]), to_centiseconds(values["end"])
        if end_cs < start_cs:
            raise ParseError(f"{source_name}: reversed scene interval")
        scene_id = f"S{number:03}-{int(match[1]):02}"
        if any(s.id == scene_id for s in scenes):
            raise ParseError(f"{source_name}: duplicate scene ID {scene_id}")
        scenes.append(Scene(scene_id, values["heading"], values["start"], values["end"], start_cs, end_cs))
        ranges.append((match.start(), stop, scene_id, start_cs, end_cs))
    if not scenes:
        raise ParseError(f"{source_name}: no SCENE headings")
    events = []
    matches = list(BLOCK.finditer(text))
    # Independent coverage counts prevent unfamiliar headings from silently losing events.
    timed_lines = re.findall(r"^[ \t]*Start:", text, re.M | re.I)
    bold_headings = re.findall(r"^\*\*[^\n]+?:\*\*", text, re.M)
    if len(matches) != len(timed_lines) or len(matches) != len(bold_headings):
        raise ParseError(f"{source_name}: parsed {len(matches)} blocks, found {len(timed_lines)} timed lines and {len(bold_headings)} headings")
    for i, match in enumerate(matches, 1):
        label, start, end = match["label"], match["start"], match["end"]
        start_cs, end_cs = to_centiseconds(start), to_centiseconds(end)
        scene = next((r for r in ranges if r[0] <= match.start() < r[1]), None)
        if scene is None or end_cs < start_cs:
            raise ParseError(f"{source_name}: invalid event interval at block {i}")
        if start_cs < scene[3] or end_cs > scene[4]:
            metadata.setdefault("parser_warnings", []).append(f"block {i} extends beyond declared scene {scene[2]}")
        base = re.split(r"\s*\(", label, 1)[0].strip().upper()
        kind = {"CAMERA": "CAMERA", "ACTION": "ACTION", "SOUND FX": "SOUND", "SFX": "SOUND", "MUSIC": "SOUND"}.get(base, "DIALOGUE")
        events.append(Event(f"E{number:03}-{i:04}", kind, start, end, start_cs, end_cs, match["body"], label, scene[2]))
    if not events:
        raise ParseError(f"{source_name}: no timed entries")
    return Episode(number, events, scenes, source_name, metadata)


def parse_episode(path: str | Path) -> Episode:
    path = Path(path)
    # newline='' preserves source text rather than normalizing CRLF line endings.
    with path.open(encoding="utf-8-sig", newline="") as source:
        return parse_text(source.read(), path.name)
