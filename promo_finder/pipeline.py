"""Auditable end-to-end selection with immutable per-episode attempts."""
from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from .model import call_model, ModelCallError
from .parser import parse_episode, to_centiseconds, format_timestamp, ParseError
from .prompts import build_prompt
from .validator import validate_candidate, rank_and_deduplicate, ValidationError


SUCCESS = {"valid_zero_picks", "valid_candidates"}
CSV_FIELDS = ["rank", "episode", "start", "end", "duration", "hook", "cliffhanger",
              "why_it_works", "score", "hook_score", "cliffhanger_score", "clarity_score",
              "review_flags", "candidate_id", "start_event_id", "end_event_id",
              "hook_event_id", "cliffhanger_event_id", "source_name", "start_cs", "end_cs"]


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value: bytes | str) -> str:
    return sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def inspect_inputs(input_path: str | Path):
    path = Path(input_path).resolve()
    files = sorted(path.glob("*.txt")) if path.is_dir() else [path]
    if not files:
        raise ValueError(f"No .txt screenplays found in {path}")
    episodes, records, seen = [], [], {}
    for file in files:
        raw = file.read_bytes()
        record = {"source_name": file.name, "source_sha256": digest(raw),
                  "status": "not_requested", "entries": 0, "scenes": 0}
        try:
            episode = parse_episode(file)
        except (ParseError, UnicodeDecodeError) as exc:
            record.update(status="input_parse_failed", error=str(exc))
            records.append(record)
            continue
        if episode.number in seen:
            raise ValueError(f"Duplicate episode number {episode.number} in {seen[episode.number]} and {file.name}")
        seen[episode.number] = file.name
        episodes.append(episode)
        record.update(episode=episode.number, entries=len(episode.events), scenes=len(episode.scenes),
                      parser_warnings=episode.metadata.get("parser_warnings", []))
        records.append(record)
    episodes.sort(key=lambda ep: ep.number)
    records.sort(key=lambda record: (record.get("episode", 10**9), record["source_name"]))
    return episodes, records


def audit(input_path, out, expect_pack=False):
    out = Path(out).resolve()
    episodes, records = inspect_inputs(input_path)
    summary = {"created_at": now(), "input_files": len(records), "episodes": len(episodes),
               "scenes": sum(len(ep.scenes) for ep in episodes),
               "entries": sum(len(ep.events) for ep in episodes),
               "episode_numbers": [ep.number for ep in episodes],
               "parse_failures": sum(r["status"] == "input_parse_failed" for r in records)}
    summary["expected_pack_passed"] = (summary["episode_numbers"] == list(range(1, 51))
        and summary["scenes"] == 100 and summary["entries"] == 3727 and not summary["parse_failures"])
    write_json(out / "parser_audit.json", {"summary": summary, "episodes": records})
    print(f"Parser: {summary['episodes']} episodes, {summary['scenes']} scenes, {summary['entries']} entries; {summary['parse_failures']} failures.", flush=True)
    if summary["parse_failures"]:
        raise ValueError("Input parse failure; inspect parser_audit.json before making model calls")
    if expect_pack and not summary["expected_pack_passed"]:
        raise ValueError("Expected exactly episodes 1–50, 100 scenes and 3,727 entries; parser_audit.json records actual coverage")
    return episodes, records, summary


def _invalid_constant(token):
    raise ValueError(f"Non-finite JSON number: {token}")


def read_response(episode, raw_path):
    """Parse saved bytes, then validate each candidate without repairing output."""
    rejections, promos = [], []
    try:
        value = json.loads(Path(raw_path).read_bytes(), parse_constant=_invalid_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        return "response_parse_failed", promos, [{"episode": episode.number,
            "reason": "invalid_json", "error": str(exc)}], None
    if (not isinstance(value, dict) or set(value) != {"candidates"}
            or not isinstance(value.get("candidates"), list) or len(value["candidates"]) > 3):
        return "response_schema_failed", promos, [{"episode": episode.number,
            "reason": "expected_candidates_list_with_zero_to_three_items"}], None
    candidates = value["candidates"]
    for index, candidate in enumerate(candidates, 1):
        try:
            promo = validate_candidate(episode, candidate)
            expected = {"start_event_id", "end_event_id", "hook_event_id", "cliffhanger_event_id",
                        "hook", "cliffhanger", "why_it_works", "hook_score", "cliffhanger_score", "clarity_score"}
            if set(candidate) != expected:
                raise ValidationError("candidate_schema_fields_mismatch")
            promos.append(promo)
        except ValidationError as exc:
            rejections.append({"episode": episode.number, "candidate_index": index,
                               "candidate": candidate, "reason": str(exc)})
    # A structurally valid model response containing rejected candidates is not
    # the same as the model deliberately returning an empty candidates list.
    status = "valid_zero_picks" if not candidates else "valid_candidates"
    return status, promos, rejections, len(candidates)


def _attempts(out, number):
    root = Path(out) / "raw" / f"episode_{number:03}"
    return sorted(root.glob("attempt_*/metadata.json"))


def _cached_attempt(out, number, source_hash, prompt_hash, provider=None, model=None):
    for path in reversed(_attempts(out, number)):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (metadata.get("source_sha256") == source_hash
                and metadata.get("prompt_sha256") == prompt_hash
                and (provider is None or metadata.get("provider") == provider)
                and (provider is None or metadata.get("model_requested") == model)):
            return path, metadata
    return None


def _use_attempt(episode, metadata_path, metadata, cached):
    record = {**metadata, "cached": cached, "metadata_path": str(metadata_path)}
    raw_path = metadata_path.parent / "response.txt"
    if metadata.get("status") == "call_failed":
        return record, [], []
    if metadata.get("status") in {"pending", "success"}:
        record.update(status="call_failed", error="Attempt interrupted before its audit completed; retry explicitly to create a new attempt")
        return record, [], []
    if not raw_path.exists():
        record.update(status="call_failed", error="Attempt has no saved response; retry explicitly to create a new attempt")
        return record, [], []
    current_hash = digest(raw_path.read_bytes())
    if metadata.get("response_sha256") and metadata["response_sha256"] != current_hash:
        record.update(status="response_parse_failed", error="Saved response changed since its original audit hash")
        return record, [], [{"episode": episode.number, "reason": "response_hash_mismatch"}]
    status, promos, rejections, proposed = read_response(episode, raw_path)
    record.update(status=status, proposed_candidates=proposed, accepted_candidates=len(promos),
                  rejected_candidates=len(rejections), response_sha256=current_hash,
                  raw_response_path=str(raw_path))
    return record, promos, rejections


def process_episode(episode, source_hash, out, provider, model, timeout, retry_failed):
    prompt = build_prompt(episode)
    prompt_hash = digest(prompt)
    cached = _cached_attempt(out, episode.number, source_hash, prompt_hash, provider, model)
    if cached and (cached[1].get("status") in SUCCESS or not retry_failed):
        return _use_attempt(episode, *cached, True)
    # Include unfinished attempt directories when finding the next number.
    episode_root = Path(out) / "raw" / f"episode_{episode.number:03}"
    numbers = [int(p.name.split("_")[-1]) for p in episode_root.glob("attempt_*")
               if p.is_dir() and p.name.split("_")[-1].isdigit()]
    attempt = max(numbers, default=0) + 1
    directory = episode_root / f"attempt_{attempt:03}"
    directory.mkdir(parents=True, exist_ok=False)
    raw_path, metadata_path = directory / "response.txt", directory / "metadata.json"
    (directory / "prompt.txt").write_bytes(prompt.encode("utf-8"))
    metadata = {"episode": episode.number, "source_name": episode.source_name,
                "source_sha256": source_hash, "prompt_sha256": prompt_hash,
                "provider": provider, "model_requested": model,
                "attempt": attempt, "status": "pending", "created_at": now()}
    write_json(metadata_path, metadata)
    try:
        transport = call_model(prompt, raw_path, provider=provider, model=model, timeout=timeout)
        metadata.update(transport)
        status, promos, rejections, proposed = read_response(episode, raw_path)
        metadata.update(status=status, proposed_candidates=proposed, accepted_candidates=len(promos),
                        rejected_candidates=len(rejections), response_sha256=digest(raw_path.read_bytes()))
    except ModelCallError as exc:
        metadata.update(exc.metadata)
        metadata.update(status="call_failed", error=str(exc), proposed_candidates=None,
                        accepted_candidates=0, rejected_candidates=0)
        promos, rejections = [], []
    metadata["completed_at"] = now()
    write_json(metadata_path, metadata)
    return {**metadata, "cached": False, "metadata_path": str(metadata_path)}, promos, rejections


def write_outputs(out, episodes, records, summary, promos, rejections, provider=None, model=None):
    out = Path(out)
    ranked, duplicates = rank_and_deduplicate(promos)
    all_rejections = rejections + duplicates
    with (out / "ranked_promos.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for promo in ranked:
            row = dict(promo)
            row["duration"] = f"{promo['duration']:.2f}"
            row["score"] = f"{promo['score']:.4f}"
            row["review_flags"] = ";".join(promo["review_flags"])
            writer.writerow(row)
    status_counts = {}
    for record in records:
        status = record["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    requested = [r for r in records if r["status"] != "not_requested"]
    complete = all(r["status"] in SUCCESS for r in requested) and bool(requested)
    all_inputs_complete = complete and len(requested) == len(records)
    reported_models = sorted({r.get("model") for r in requested
                              if r.get("model") not in {None, "codex-default", "not_reported", "unknown"}})
    run_summary = {**summary, "updated_at": now(), "status_counts": status_counts,
                   "ranked_promos": len(ranked), "candidate_rejections": len(all_rejections),
                   "provider": provider, "model": ", ".join(reported_models) or "not_reported",
                   "model_requested": model,
                   "requested_episodes": len(requested),
                   "processed_episodes": sum(r["status"] in SUCCESS for r in records),
                   "unrequested_episodes": status_counts.get("not_requested", 0),
                   "selection_status": "complete" if complete else "incomplete",
                   "all_inputs_complete": all_inputs_complete,
                   "full_pack_complete": all_inputs_complete and summary["expected_pack_passed"],
                   "run_status": "complete" if all_inputs_complete else "partial" if complete else "incomplete"}
    write_json(out / "rejections.json", all_rejections)
    write_json(out / "run_log.json", {"summary": run_summary, "episodes": records})
    write_json(out / "data.json", {"meta": {**run_summary, "mode": "private"},
               "episodes": [asdict(ep) for ep in episodes], "promos": ranked,
               "log": records, "rejections": all_rejections})
    return ranked, run_summary


def run(input_path, out, *, episodes_requested=None, provider="codex", model=None,
        workers=1, timeout=240, retry_failed=False, expect_pack=False):
    if workers < 1:
        raise ValueError("workers must be at least 1")
    out = Path(out).resolve()
    episodes, records, summary = audit(input_path, out, expect_pack)
    by_number = {r["episode"]: r for r in records}
    selected = set(episodes_requested) if episodes_requested is not None else set(by_number)
    missing = selected - set(by_number)
    if missing:
        raise ValueError(f"Requested episode numbers not found: {sorted(missing)}")
    for record in records:
        if record["episode"] in selected:
            record["status"] = "pending"
    promos, rejections = [], []
    # Results from prior selected runs remain visible during a later full run.
    # Unrequested cached episodes are also replayed, with their original provenance.
    for episode in episodes:
        if episode.number in selected:
            continue
        cached = _cached_attempt(out, episode.number, by_number[episode.number]["source_sha256"], digest(build_prompt(episode)))
        if cached:
            record, accepted, rejected = _use_attempt(episode, *cached, True)
            by_number[episode.number].update(record)
            promos.extend(accepted)
            rejections.extend(rejected)
    write_outputs(out, episodes, records, summary, promos, rejections, provider, model)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_episode, ep, by_number[ep.number]["source_sha256"], out,
                               provider, model, timeout, retry_failed): ep for ep in episodes if ep.number in selected}
        for future in as_completed(futures):
            episode = futures[future]
            try:
                record, accepted, rejected = future.result()
            except Exception as exc:
                # Programming/process errors remain visible, never become zero picks.
                record, accepted, rejected = {"episode": episode.number, "status": "call_failed",
                    "error": f"{type(exc).__name__}: {exc}"}, [], []
            by_number[episode.number].update(record)
            promos.extend(accepted)
            rejections.extend(rejected)
            ranked, result_summary = write_outputs(out, episodes, records, summary, promos, rejections, provider, model)
            print(f"Episode {episode.number:02}: {record['status']}; accepted {len(accepted)}, rejected {len(rejected)}{' (cached)' if record.get('cached') else ''}", flush=True)
    ranked, result_summary = write_outputs(out, episodes, records, summary, promos, rejections, provider, model)
    print(f"Wrote {len(ranked)} ranked promos to {out / 'ranked_promos.csv'}", flush=True)
    return result_summary


def render(input_path, out, expect_pack=False):
    out = Path(out).resolve()
    episodes, records, summary = audit(input_path, out, expect_pack)
    by_number = {r["episode"]: r for r in records}
    promos, rejections = [], []
    providers, models_requested = set(), set()
    for episode in episodes:
        cached = _cached_attempt(out, episode.number, by_number[episode.number]["source_sha256"], digest(build_prompt(episode)))
        if cached:
            record, accepted, rejected = _use_attempt(episode, *cached, True)
            by_number[episode.number].update(record)
            promos.extend(accepted)
            rejections.extend(rejected)
            providers.add(record.get("provider", "unknown"))
            if record.get("model_requested"):
                models_requested.add(record["model_requested"])
    ranked, result_summary = write_outputs(out, episodes, records, summary, promos, rejections,
        ", ".join(sorted(providers)) or None, ", ".join(sorted(models_requested)) or None)
    print(f"Replayed saved responses; wrote {len(ranked)} ranked promos without model calls.", flush=True)
    return result_summary


def benchmark(input_path, out):
    out = Path(out).resolve()
    episodes, _, _ = audit(input_path, out)
    episode = next((ep for ep in episodes if ep.number == 3), None)
    if episode is None:
        raise ValueError("Manual benchmark needs Episode 3")
    a, b = to_centiseconds("01:24.00"), to_centiseconds("02:20.50")
    windows = []
    for boundary in (a, b):
        events = [asdict(e) for e in episode.events if e.start_cs <= boundary + 200 and e.end_cs >= boundary - 200]
        windows.append({"boundary": format_timestamp(boundary), "events": events})
        print(f"\nEvents around {format_timestamp(boundary)}:")
        for event in events:
            print(f"  {event['id']} {event['start']}–{event['end']} {event['label']}: {event['original_text'].strip()}")
    assert b - a == 5650
    value = {"episode": 3, "start": "01:24.00", "end": "02:20.50", "duration_cs": b - a,
             "duration": (b - a) / 100, "events_around_boundaries": windows,
             "note": "Manual benchmark only; never included in selection model prompts"}
    write_json(out / "benchmark_ep003.json", value)
    print(f"\nManual cut: 01:24.00–02:20.50 = {(b - a) / 100:.2f} seconds", flush=True)
    return value
