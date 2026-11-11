# CUTLIST · Find the promos

**CUTLIST finds short promo clips (20–60 seconds) inside a screenplay.**
An AI suggests the clips, Python checks the timing and ranks them, and a person makes the final call in a simple browser editor.

👉 **[Try the live demo](https://tanishka15.github.io/cutlist-promo-finder/)**. No install is needed. The demo uses an invented sample story and saved results, so it does not call an AI.

---

## What is it for?

Short-video teams need teaser clips that make people want to watch the next episode. Finding them by hand means reading a whole script. CUTLIST gives an editor a ranked shortlist instead.

A good promo has three things. CUTLIST scores each one:

| What it checks | The question | Weight |
| --- | --- | --- |
| **Hook** | Does something grab attention in the first 3 seconds? | 40% |
| **Cliffhanger** | Does it stop before an important question is answered? | 35% |
| **Clarity** | Can a new viewer follow the conflict? | 25% |

## How it works

```
 Screenplay (.txt)  →  AI suggests up to 3 cuts  →  Python checks & ranks  →  You keep or drop each one
```

- **The AI** picks start and end points and explains why.
- **Python** rejects anything with wrong timing (not 20–60 s, a hook too late, an ending cut off) and ranks the rest.
- **You** read each clip in the editor and decide. The editor works from text: "Play script" scrolls the lines against a clock. You can also attach a video file if you have one.

---

## Option 1: Try the demo in your browser (1 minute)

1. Open the **[live demo](https://tanishka15.github.io/cutlist-promo-finder/)**.
2. Click the first clip card (**#01**) on the left.
3. Press **Play script** to watch the timed text play.
4. Click **Opening evidence** and **Ending evidence** to see why the clip was picked.
5. Under **Your editorial call**, choose **Post** or **Drop** and write a reason.
6. Click **Export reviews** to download your decisions as a CSV.

## Option 2: Run it on your own computer

**You need:** Python 3.10 or newer. Nothing else needs installing.

**Step 1: Get the code**

```sh
git clone https://github.com/Tanishka15/cutlist-promo-finder.git
cd cutlist-promo-finder
```

**Step 2: Open the editor with the sample results (no AI needed)**

```sh
python3 -m promo_finder serve --out promo_finder/web --port 8765
```

Open **http://127.0.0.1:8765** in your browser. Press `Control+C` in the terminal to stop.

**Step 3: Find promos in a screenplay with AI**

Choose **one** AI provider:

```sh
# A) Codex CLI (if you are already signed in to it)
python3 -m promo_finder run examples/synthetic_episode.txt --out results --provider codex

# B) OpenAI API (use a model your account can access)
export OPENAI_API_KEY='your-key'
python3 -m promo_finder run examples/synthetic_episode.txt --out results --provider openai --model YOUR_MODEL
```

The run is finished when the terminal prints a line ending in `ranked_promos.csv`.

**Step 4: Review your results**

```sh
python3 -m promo_finder serve --out results --port 8765
```

Open **http://127.0.0.1:8765**, then post or drop each suggested clip.

> 💡 To use your own script, replace `examples/synthetic_episode.txt` with the path to your file or to a folder of scripts. Keep the `--out` folder private.

---

## What you get

All results are saved in the `--out` folder:

| File | What's inside |
| --- | --- |
| `ranked_promos.csv` | The clip list: start/end times, length, scores, reasons and rank. **Start here.** |
| `data.json` | Data for the browser editor |
| `run_log.json` | What was processed and whether each episode succeeded |
| `rejections.json` | Clips that failed the checks, and why |
| `raw/` | The AI's unedited answers, kept so every result can be checked |

---

## Writing a screenplay file

Each file is plain text. It needs an episode number, then scenes, then timed blocks. Each block has a label in `**…:**`, a start and end time (`MM:SS.hh`), and its text:

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

To check a file before running the AI (no AI call):

```sh
python3 -m promo_finder audit your_script.txt --out audit-check
```

It reports how many episodes, scenes and entries it found, and it points to any formatting errors.

---

## All commands at a glance

| Command | What it does | Calls the AI? |
| --- | --- | --- |
| `audit` | Reads and checks screenplay files | No |
| `run` | Finds promo clips | **Yes** |
| `render` | Rebuilds results from saved AI answers | No |
| `serve` | Opens the browser editor | No |
| `benchmark` | Prints a manual timing reference for comparison | No |

Useful `run` options: `--episodes 2,3,6` (only these episodes), `--workers 3` (process episodes in parallel) and `--retry-failed` (try failed episodes again).

---

<details>
<summary><b>How it works in detail</b> (for developers)</summary>

### Parsing
- Episode numbers come from the file content, never from filenames. Headings match case-insensitively.
- Minutes can exceed 59; seconds must be 00–59. All timing is stored as whole centiseconds.
- Block IDs look like `E001-0001`. Source order, overlapping blocks, labels and original text are kept as they are.
- An unknown or malformed timed block causes an error instead of being skipped silently. A block that runs past its scene is kept, with a warning.

### Model calls
- Each episode gets one independent request that returns zero to three candidates.
- The prompt contains timed events only. Loglines, continuity notes, benchmarks and future identity mappings are withheld.
- `call_model()` in `model.py` is the provider boundary. The Codex adapter runs in an isolated, ephemeral, read-only directory with no tools. The OpenAI adapter uses structured outputs through the Responses API.
- The exact response is saved before it is parsed. Saved responses are reused only when their provenance matches. Failed calls are never retried silently; `--retry-failed` creates a new attempt.

### Validation and ranking
- All four event IDs must exist in the same episode.
- The duration must be 20–60 seconds inclusive, the hook must start within the first 3 seconds, and the whole cliffhanger event must fit inside the cut.
- A boundary that falls inside overlapping dialogue is flagged for review.
- The score is `0.40 × hook + 0.35 × cliffhanger + 0.25 × clarity` on a 0–5 scale.
- Near-duplicate cuts are removed: these share hook and cliffhanger IDs and overlap at least 80%. Ties go to higher clarity, then the earlier timing.

### Limits
Timing checks cannot prove that a question stays unresolved or that a shot plays well. Always read the transcript and check the footage. Editor decisions are saved in the browser and exported as CSV. They never change the raw model outputs or scores.

### Tests
```sh
python3 -m unittest discover -s tests -v
```
The tests cover parsing, exact timing, overlapping events, unknown IDs, duration and hook limits, incomplete cliffhangers, interrupted dialogue, invalid scores and deduplication.

### Privacy
Keep scripts, results, prompts, footage and API keys outside this public repo. Serve private results only on `127.0.0.1`. The local server exposes only an explicit list of files and does not list directories.

References: [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive) · [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

</details>
