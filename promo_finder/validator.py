"""Mechanical eligibility checks, separate from subjective editorial scores."""
from __future__ import annotations
import math
from .parser import Episode


class ValidationError(ValueError):
    pass


ID_FIELDS = ("start_event_id", "end_event_id", "hook_event_id", "cliffhanger_event_id")
SCORE_FIELDS = ("hook_score", "cliffhanger_score", "clarity_score")
TEXT_FIELDS = ("hook", "cliffhanger", "why_it_works")


def validate_candidate(episode: Episode, candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        raise ValidationError("candidate_not_object")
    by_id = {e.id: e for e in episode.events}
    for field in ID_FIELDS:
        if not isinstance(candidate.get(field), str) or candidate[field] not in by_id:
            raise ValidationError(f"unknown_event_id: {field}={candidate.get(field)!r}")
    for field in SCORE_FIELDS:
        value = candidate.get(field)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 5:
            raise ValidationError(f"invalid_score: {field}={value!r}")
    for field in TEXT_FIELDS:
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            raise ValidationError(f"missing_explanation: {field}")
    start, end, hook, cliff = (by_id[candidate[f]] for f in ID_FIELDS)
    a, b = start.start_cs, end.end_cs
    if not 0 <= a < b <= episode.end_cs:
        raise ValidationError("cut_outside_episode_or_reversed")
    duration_cs = b - a
    if not 2000 <= duration_cs <= 6000:
        raise ValidationError(f"duration_out_of_range: {duration_cs / 100:.2f}s")
    if not a <= hook.start_cs <= a + 300:
        raise ValidationError("hook_outside_first_three_seconds")
    if hook.end_cs > b:
        raise ValidationError("hook_incomplete")
    if not a <= cliff.start_cs <= cliff.end_cs <= b:
        raise ValidationError("cliffhanger_not_fully_contained")
    flags = []
    for boundary, value in (("start", a), ("end", b)):
        for event in episode.events:
            if event.type == "DIALOGUE" and event.start_cs < value < event.end_cs:
                flags.append(f"{boundary}_inside_dialogue:{event.id}")
    if candidate["clarity_score"] < 3:
        flags.append("low_standalone_clarity")
    if b - cliff.end_cs > 300:
        flags.append("review_outcome_after_cliffhanger")
    score = (40 * candidate["hook_score"] + 35 * candidate["cliffhanger_score"] + 25 * candidate["clarity_score"]) / 100
    return {**candidate, "candidate_id": f"EP{episode.number:03}-{start.id}-{end.id}",
            "episode": episode.number, "source_name": episode.source_name,
            "start": start.start, "end": end.end, "start_cs": a, "end_cs": b,
            "duration": duration_cs / 100, "score": round(score, 4),
            "review_flags": flags}


def rank_and_deduplicate(promos: list[dict]) -> tuple[list[dict], list[dict]]:
    ordered = sorted(promos, key=lambda p: (-p["score"], -p["clarity_score"], p["episode"], p["start_cs"], p["end_cs"]))
    kept, rejected = [], []
    for promo in ordered:
        duplicate = next((p for p in kept if p["episode"] == promo["episode"]
            and p["hook_event_id"] == promo["hook_event_id"]
            and p["cliffhanger_event_id"] == promo["cliffhanger_event_id"]
            and max(0, min(p["end_cs"], promo["end_cs"]) - max(p["start_cs"], promo["start_cs"]))
            / min(p["end_cs"] - p["start_cs"], promo["end_cs"] - promo["start_cs"]) >= 0.8), None)
        if duplicate:
            rejected.append({"candidate": promo, "reason": "duplicate_overlap", "kept_candidate_id": duplicate["candidate_id"]})
        else:
            kept.append(promo)
    for rank, promo in enumerate(kept, 1):
        promo["rank"] = rank
    return kept, rejected
