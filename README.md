# CUTLIST · Find the promos

[Open the live demo](https://tanishka15.github.io/cutlist-promo-finder/)

Select auditable 20–60 second, continuous promos from timestamped screenplays.
Python handles exact timing and eligibility; a model proposes and scores cuts;
an editor makes the posting decision. No dependency installation is needed.

## Quick start

Requires Python 3.10+. Run these commands **from this extracted project folder**.

```sh
# Explore a fictional example without a key or model call.
python3 -m promo_finder serve --out promo_finder/web --port 8765
# Open http://127.0.0.1:8765 and choose a clip.

# Parse any screenplay using the documented text format.
python3 -m promo_finder audit examples/synthetic_episode.txt --out /tmp/cutlist-audit

# Use an authenticated Codex CLI; defaults to its built-in model.
python3 -m promo_finder run /path/to/screenplays --out /path/to/private-results --provider codex

# Or use the OpenAI Responses API; choose a model available to your account.
export OPENAI_API_KEY='your-key'
python3 -m promo_finder run /path/to/new-screenplay.txt --out /path/to/private-results --provider openai --model YOUR_MODEL

# Inspect the actual run in the local editor.
python3 -m promo_finder serve --out /path/to/private-results --port 8765
```

The editor assets are included in the Python package, including wheel installations.
The source ZIP download on the hosted demo is optional locally: the extracted
project already contains the Python source and this README.

The hosted demo contains fictional, pre-authored sample results. It makes no live
model calls. Download this source to process a new screenplay locally. Model calls
use your existing provider account. Footage is optional: attach a local episode
video in the editor to seek to exact cut boundaries; files stay in your browser.

## Input format

Episode numbers come from file content, never filenames. Headings are matched
case-insensitively. Each block must have a label, explicit start/end timecodes,
and original text. Minutes can exceed 59; seconds must be 00–59. Example:

```text
EPISODE NUMBER: Episode 1

--- SCRIPT START ---
SCENE 01
Scene heading: INT. STATION CONTROL ROOM – NIGHT
Scene start: 00:00.00
Scene end: 00:30.00

**CAMERA:**
Start: (00:00.00) | End: (00:30.00)
A red signal flashes over an empty platform.

--- SCRIPT END ---
```

`parse_episode(path)` preserves source order, overlapping blocks, original text,
labels and timestamp strings. IDs such as `E001-0001` identify source blocks;
numeric timing is always integer centiseconds. Unknown/malformed timed blocks
cause a parse error, rather than silently disappearing. A block extending beyond
its declared scene is preserved with a source warning.

## Workflow and evidence

```sh
# Verify a complete assignment pack, if you have it privately.
python3 -m promo_finder audit /path/to/pack --out /path/to/private-results --expect-pack

# Development gate, then fresh episodes, then the entire pack.
python3 -m promo_finder run /path/to/pack --out /path/to/private-results --provider codex --episodes 2,3,6
python3 -m promo_finder run /path/to/pack --out /path/to/private-results --provider codex --episodes 23,41,45
python3 -m promo_finder run /path/to/pack --out /path/to/private-results --provider codex --workers 3

# Replay saved responses; no model call.
python3 -m promo_finder render /path/to/pack --out /path/to/private-results

# Print manual Episode 3 boundary evidence separately from model selection.
python3 -m promo_finder benchmark /path/to/pack --out /path/to/private-results
```

Each episode gets one independent request with zero to three candidates. The
prompt contains timed events only: loglines, continuity notes, manual benchmarks,
and future identity mappings are withheld. Character labels stay as observed in
the scene. The model judges clarity from the selected cut's content.

`call_model()` in `model.py` is the provider boundary. The Codex adapter uses an
isolated working directory, ephemeral read-only execution, and no tools. The
OpenAI adapter uses structured outputs through the Responses API. The exact
response is saved before candidate JSON is parsed. Raw traces/HTTP envelopes,
prompts, source/prompt hashes, transport metadata, and errors are retained.

Existing responses are reused only with matching provenance. Failed attempts are
not retried silently; `--retry-failed` creates a new immutable attempt. A valid
empty `candidates` list is distinct from call, JSON, schema, and candidate failures.

Outputs:

| Artifact | Purpose |
| --- | --- |
| `ranked_promos.csv` | Exact timecodes, duration, evidence IDs, explanations, scores, flags and rank |
| `raw/episode_*/attempt_*/` | Unchanged response, prompt and provider evidence |
| `run_log.json` | Every source episode, parser counts, status and provenance |
| `rejections.json` | Specific invalid candidates and overlap suppression reasons |
| `data.json` | Private editor data, including screenplay events |

All four event IDs must exist in the same episode. Duration must be 20–60 seconds
inclusive. The hook must start inside the first three seconds, and the entire
cliffhanger event must fit. Either boundary strictly inside **any** overlapping
dialogue is flagged for review, even if the selected boundary is a camera shot.
The ranking is `0.40 × hook + 0.35 × cliffhanger + 0.25 × clarity`, on a 0–5 scale.
Cuts sharing hook and cliffhanger IDs with at least 80% overlap of the shorter
cut are deduplicated after scoring. Ties prefer higher clarity, then stable timing.

Timing checks cannot establish that a question remains unresolved or that a shot
plays well. Review the transcript inside each cut, then confirm against footage.
The editor supports post/adjust/drop with a reason, saved in this browser and
exported as CSV. These decisions do not alter raw model outputs or scores.

## Tests

```sh
python3 -m unittest discover -s tests -v
# Optional private integration; scripts are never copied into this project.
SCREENPLAY_DIR=/path/to/pack python3 -m unittest discover -s tests -v
```

Tests cover parser coverage, exact timing, overlapping events, unknown IDs,
inclusive duration/hook limits, incomplete cliffhangers, interrupted dialogue,
invalid scores and evidence-aware deduplication.

## Confidentiality and deployment

Keep source scripts, results, prompts, traces, attached footage and credentials
outside this public project. The public download contains only code, synthetic
fixtures, and synthetic demonstration results. Serve private results on loopback;
the local server exposes an explicit asset allowlist, without directory listings.

Provider references: [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
and [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
