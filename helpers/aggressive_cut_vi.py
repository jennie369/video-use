"""Aggressive single-source auto-cut for Vietnamese talking-head videos.

Pipeline (no questions, no confirmation — sane defaults shipped):
  1. Transcribe with diarize+audio_events (catches slate/noise events)
  2. Re-transcribe clean (no diarize, no audio events) → recover word timing
     for spans Scribe collapsed into giant audio_event blobs
  3. Hybrid merge: clean is primary; replace broken tokens (single word
     spanning >5s) with original transcript words for that span
  4. Slate drop: anything before first content word in clean (typically <33s)
  5. Gap >=1s = cut point. Group remaining words into speech blocks.
  6. Per-block surgical trim:
       - drop pure-filler blocks (ờ/ừ/ơ/à/...)
       - drop 1-2 word cutoff fragments ending --
       - trim trailing cutoff/filler words from each kept block
       - trim leading filler words
  7. Dedupe REPEAT_PREFIX (rule-based, broadened 2026-05-06):
       - SequenceMatcher ratio >= 0.65 on first 80 chars (was 0.75 / 40 chars)
       - Drop shorter block when adjacent blocks overlap; SHORT_REPEAT_MAX=10s
       - Drops the previously-required "next is 1.5x longer" gate (caught
         too few partial-restate retakes)
  8. LLM review pass (Gemini Flash) — DEFAULT OFF (changed 2026-06-20):
       - OPT-IN with --llm-review, and ONLY for standalone/headless runs
         (cron, batch) where no in-loop LLM is available.
       - WHEN CLAUDE (or any in-loop LLM) DRIVES: do NOT use Gemini. The
         driving LLM reads the transcript itself and decides retakes
         (cross-block AND intra-block) via helpers/apply_retake_cuts.py.
         The in-loop model is stronger than Flash, has no rate limit, and
         never caches empty 429 responses. See SKILL.md "Claude-driven
         retake removal" + VIDEO_EDITING_EVOLUTION_LOG (2026-06-20).
       - When enabled: ambiguous adjacent pairs (ratio 0.30-0.85, gap<=8s,
         neither block >25s) → Gemini retake verdict; caches to
         edit/llm-review-cache.json; fails open to rule-based.
  9. Build EDL → render.py final.mp4 (no grade, no subtitle, no overlay)

Known limitation: rule-based + LLM dedupe only fire on CROSS-BLOCK retakes
(takes separated by >=gap pause). If speaker stutters and restarts WITHIN
the same block (no >=1s pause between failed take and restart), both takes
join into one string and survive. Future option A would add intra-block
n-gram dedupe; not enabled per current design.

Usage:
    python helpers/aggressive_cut_vi.py <video_path>
    python helpers/aggressive_cut_vi.py <video_path> --gap 1.0 --slate-end 33
    python helpers/aggressive_cut_vi.py <video_path> --no-llm-review  # rule-only

Per-language: tweak FILLER_RE / NOISE_RE for non-Vietnamese sources.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests

SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
DEFAULT_GAP = 1.0
DEFAULT_HEAD_PAD = 0.05  # 50ms
DEFAULT_TAIL_PAD = 0.08  # 80ms
BROKEN_TOKEN_DURATION = 5.0  # word spanning >5s is a Scribe artifact

# B (broadened dedupe, 2026-05-06):
DEDUPE_RATIO = 0.65          # was 0.75 — catch partial restates
DEDUPE_PREFIX_CHARS = 80     # was 40 — wider compare window
SHORT_REPEAT_MAX = 10.0      # was 5.0 — longer false-starts qualify

# C (LLM review trigger zone):
LLM_REVIEW_RATIO_LO = 0.20   # was 0.30 - catch even slight similarities
LLM_REVIEW_RATIO_HI = 0.85   # above: rule-based "duplicate" stands
LLM_REVIEW_MAX_GAP = 10.0     # was 8.0 - catch longer pauses
LLM_REVIEW_MAX_BLOCK = 25.0  # if either block longer, retakes implausible

FILLER_RE = re.compile(r"^(ờ|ừ|ơ|à|ờm|ừm|èm|hờ|ầm|uhm|umm)[.,!?-]*$", re.IGNORECASE)


def _load_env_key(var: str) -> str:
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",
        Path(".env"),
    ]
    for c in candidates:
        if c.exists():
            for line in c.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{var}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(var, "")


def load_api_key() -> str:
    v = _load_env_key("ELEVENLABS_API_KEY")
    if not v:
        sys.exit("ELEVENLABS_API_KEY not found")
    return v


def load_gemini_key() -> str:
    """Returns empty string when missing; caller decides whether to skip LLM review."""
    return _load_env_key("GEMINI_API_KEY")


def extract_audio(video: Path, dest: Path) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def call_scribe(audio: Path, key: str, language: str, diarize: bool, tag_events: bool) -> dict:
    with open(audio, "rb") as f:
        resp = requests.post(
            SCRIBE_URL,
            headers={"xi-api-key": key},
            files={"file": (audio.name, f, "audio/wav")},
            data={
                "model_id": "scribe_v1",
                "diarize": "true" if diarize else "false",
                "tag_audio_events": "true" if tag_events else "false",
                "timestamps_granularity": "word",
                "language_code": language,
            },
            timeout=1800,
        )
    if resp.status_code != 200:
        sys.exit(f"Scribe {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def transcribe_dual(video: Path, edit_dir: Path, language: str, key: str) -> tuple[Path, Path]:
    """Transcribe twice: original (with audio events) + clean (without).
    Cached. Returns (orig_path, clean_path).
    """
    tdir = edit_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    orig = tdir / f"{video.stem}.json"
    clean = tdir / f"{video.stem}-clean.json"

    if orig.exists() and clean.exists():
        print(f"  cached: both transcripts exist")
        return orig, clean

    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "audio.wav"
        print("  extract audio...", flush=True)
        extract_audio(video, audio)
        size_mb = audio.stat().st_size / (1024 * 1024)
        print(f"  audio {size_mb:.1f} MB ready", flush=True)

        if not orig.exists():
            print("  scribe pass 1: diarize + audio events", flush=True)
            t0 = time.time()
            data = call_scribe(audio, key, language, diarize=True, tag_events=True)
            orig.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"    saved {orig.name} in {time.time()-t0:.1f}s")

        if not clean.exists():
            print("  scribe pass 2: clean (no diarize, no events)", flush=True)
            t0 = time.time()
            data = call_scribe(audio, key, language, diarize=False, tag_events=False)
            clean.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"    saved {clean.name} in {time.time()-t0:.1f}s")

    return orig, clean


def build_hybrid_words(orig_path: Path, clean_path: Path) -> tuple[list[dict], float]:
    orig = json.load(open(orig_path, encoding="utf-8"))
    clean = json.load(open(clean_path, encoding="utf-8"))
    duration = clean.get("audio_duration_secs") or orig.get("audio_duration_secs", 0.0)

    clean_words = [w for w in clean["words"] if w.get("type") == "word"]
    orig_words = [w for w in orig["words"] if w.get("type") == "word"]

    hybrid = []
    broken_count = 0
    for cw in clean_words:
        if (cw["end"] - cw["start"]) > BROKEN_TOKEN_DURATION:
            broken_count += 1
            replacement = [
                ow for ow in orig_words
                if ow["start"] >= cw["start"] - 0.1 and ow["end"] <= cw["end"] + 0.1
            ]
            if replacement:
                hybrid.extend(replacement)
            else:
                hybrid.append(cw)
        else:
            hybrid.append(cw)
    hybrid.sort(key=lambda w: w["start"])
    print(f"  hybrid: {len(hybrid)} words ({broken_count} broken-token spans replaced)")
    return hybrid, duration


def auto_slate_end(hybrid: list[dict]) -> float:
    """First content word in clean transcript is typically the start of real audio.
    Pre-content noise (testing, alo) is collapsed into broken tokens or appears
    before. Use start of first hybrid word as slate-end.
    """
    return hybrid[0]["start"] if hybrid else 0.0


# ─── LLM review pass (Gemini Flash) ────────────────────────────────────────

LLM_PROMPT = """Bạn xem 2 đoạn nói liên tiếp trong video tiếng Việt 1 người nói:

ĐOẠN 1 ({s1:.2f}s → {e1:.2f}s, dài {d1:.1f}s):
\"\"\"{t1}\"\"\"

ĐOẠN 2 ({s2:.2f}s → {e2:.2f}s, dài {d2:.1f}s):
\"\"\"{t2}\"\"\"

Câu hỏi: ĐOẠN 2 có phải là RETAKE (nói lại để thay thế) của ĐOẠN 1 không?

- DROP_FIRST: Đoạn 2 nói lại cùng ý đoạn 1 nhưng ĐẦY ĐỦ/MƯỢT hơn (đoạn 1 là take lỗi, vấp, dở dang). Bỏ đoạn 1, giữ đoạn 2.
- DROP_SECOND: Đoạn 1 đầy đủ rồi, đoạn 2 chỉ là vấp lặp lại không hoàn chỉnh. Bỏ đoạn 2, giữ đoạn 1.
- KEEP_BOTH: Đoạn 2 nói tiếp ý mới HOẶC nhắc lại có CHỦ Ý nhấn mạnh. Giữ cả hai.

Chỉ trả lời CHÍNH XÁC 1 trong 3 từ: DROP_FIRST, DROP_SECOND, KEEP_BOTH"""


def _cache_key(s1: float, e1: float, t1: str, s2: float, e2: float, t2: str) -> str:
    import hashlib
    h = hashlib.sha1(f"{s1:.3f}|{e1:.3f}|{t1}|{s2:.3f}|{e2:.3f}|{t2}".encode("utf-8")).hexdigest()[:16]
    return h


def llm_review_pair(
    s1: float, e1: float, t1: str,
    s2: float, e2: float, t2: str,
    api_key: str,
    cache: dict,
) -> str:
    """Returns DROP_FIRST | DROP_SECOND | KEEP_BOTH. Falls back to KEEP_BOTH on API error."""
    key = _cache_key(s1, e1, t1, s2, e2, t2)
    if key in cache:
        return cache[key]

    prompt = LLM_PROMPT.format(
        s1=s1, e1=e1, d1=e1 - s1, t1=t1[:500],
        s2=s2, e2=e2, d2=e2 - s2, t2=t2[:500],
    )
    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 256},
            },
            timeout=30,
        )
        if resp.status_code != 200:
            verdict = "KEEP_BOTH"
        else:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
            for tok in ("DROP_FIRST", "DROP_SECOND", "KEEP_BOTH"):
                if tok in text:
                    verdict = tok
                    break
            else:
                verdict = "KEEP_BOTH"
    except Exception:
        verdict = "KEEP_BOTH"

    cache[key] = verdict
    return verdict


def _should_llm_review(s1: float, e1: float, t1: str, s2: float, e2: float, t2: str, ratio: float) -> bool:
    if not (LLM_REVIEW_RATIO_LO <= ratio <= LLM_REVIEW_RATIO_HI):
        return False
    if (s2 - e1) > LLM_REVIEW_MAX_GAP:
        return False
    if (e1 - s1) > LLM_REVIEW_MAX_BLOCK or (e2 - s2) > LLM_REVIEW_MAX_BLOCK:
        return False
    return True


def _remove_intra_block_stutters(words: list[dict]) -> tuple[list[dict], int]:
    """Greedy N-gram deduplication to drop stuttered words within a block.
    Removes the first occurrence of repeating word sequences (length 1 to 5)."""
    changed = True
    intra_drops = 0
    while changed:
        changed = False
        n_words = len(words)
        # Look for longest repeats first (e.g. 5 words down to 1 word)
        for N in range(min(5, n_words // 2), 0, -1):
            for i in range(n_words - 2*N + 1):
                # Clean punctuation for comparison
                seq1 = [re.sub(r'[.,!?-]', '', w["text"]).lower().strip() for w in words[i:i+N]]
                seq2 = [re.sub(r'[.,!?-]', '', w["text"]).lower().strip() for w in words[i+N:i+2*N]]
                if seq1 == seq2 and all(seq1):
                    # Found a stutter! Drop the first N words.
                    words = words[:i] + words[i+N:]
                    intra_drops += N
                    changed = True
                    break
            if changed:
                break
    return words, intra_drops


def compute_cut_plan(
    hybrid: list[dict],
    gap_threshold: float,
    slate_end: float,
    head_pad: float,
    tail_pad: float,
    llm_key: str = "",
    llm_cache: dict | None = None,
) -> tuple[list[tuple[float, float, str]], dict]:
    # Drop slate region
    hybrid = [w for w in hybrid if w["start"] >= slate_end]
    if not hybrid:
        return [], {"reason": "no words after slate"}

    # Group blocks
    blocks = [[hybrid[0]]]
    for i in range(1, len(hybrid)):
        if hybrid[i]["start"] - hybrid[i-1]["end"] >= gap_threshold:
            blocks.append([hybrid[i]])
        else:
            blocks[-1].append(hybrid[i])

    keep, skip_counts = [], {"PURE_FILLER": 0, "CUTOFF": 0, "SHORT": 0}
    intra_drops_total = 0
    for block in blocks:
        n = len(block)
        bs, be = block[0]["start"], block[-1]["end"]
        if n <= 3 and all(FILLER_RE.match(w["text"]) for w in block):
            skip_counts["PURE_FILLER"] += 1
            continue
        if n <= 2 and any(w["text"].endswith("--") or w["text"].endswith("-") for w in block):
            skip_counts["CUTOFF"] += 1
            continue
        if (be - bs) < 0.4 and n <= 2:
            skip_counts["SHORT"] += 1
            continue

        kept = list(block)
        while kept and (kept[-1]["text"].endswith("--") or kept[-1]["text"].endswith("-") or FILLER_RE.match(kept[-1]["text"])):
            kept.pop()
        while kept and FILLER_RE.match(kept[0]["text"]):
            kept.pop(0)
            
        if kept:
            kept, drops = _remove_intra_block_stutters(kept)
            intra_drops_total += drops
            
        if not kept:
            skip_counts["CUTOFF"] += 1
            continue

        keep.append((kept[0]["start"], kept[-1]["end"], " ".join(w["text"] for w in kept)))

    # Two-tier dedupe: rule-based (B) → ambiguous-zone LLM review (C).
    # Walk through keep[] producing decisions: drop[i] = True means skip block i.
    drop = [False] * len(keep)
    rule_drops, llm_drops = 0, 0
    llm_calls, llm_keep = 0, 0
    if llm_cache is None:
        llm_cache = {}

    for i in range(len(keep) - 1):
        if drop[i]:
            continue
        s1, e1, t1 = keep[i]
        s2, e2, t2 = keep[i + 1]
        ratio = SequenceMatcher(
            None,
            t1[:DEDUPE_PREFIX_CHARS].lower(),
            t2[:DEDUPE_PREFIX_CHARS].lower(),
        ).ratio()

        # Tier 1: rule-based confident-drop.
        # Drops the SHORTER of two adjacent blocks when prefix similarity is
        # high and the shorter block is below SHORT_REPEAT_MAX (likely false start).
        d1 = e1 - s1
        d2 = e2 - s2
        if ratio >= DEDUPE_RATIO and min(d1, d2) < SHORT_REPEAT_MAX:
            if d1 <= d2:
                drop[i] = True
            else:
                drop[i + 1] = True
            rule_drops += 1
            continue

        # Tier 2: LLM review for the ambiguous middle band.
        if llm_key and _should_llm_review(s1, e1, t1, s2, e2, t2, ratio):
            verdict = llm_review_pair(s1, e1, t1, s2, e2, t2, llm_key, llm_cache)
            llm_calls += 1
            if verdict == "DROP_FIRST":
                drop[i] = True
                llm_drops += 1
            elif verdict == "DROP_SECOND":
                drop[i + 1] = True
                llm_drops += 1
            else:
                llm_keep += 1

    deduped = [(s, e, t) for idx, (s, e, t) in enumerate(keep) if not drop[idx]]

    stats = {
        "blocks_input": len(blocks),
        "blocks_kept": len(deduped),
        "skip_pure_filler": skip_counts["PURE_FILLER"],
        "skip_cutoff": skip_counts["CUTOFF"],
        "skip_short": skip_counts["SHORT"],
        "intra_stutter_words_dropped": intra_drops_total,
        "deduped_rule": rule_drops,
        "deduped_llm": llm_drops,
        "llm_calls": llm_calls,
        "llm_keep_both": llm_keep,
    }
    return deduped, stats


def write_edl(ranges: list[tuple[float, float, str]], video: Path, source_dur: float, head_pad: float, tail_pad: float, edit_dir: Path) -> Path:
    src_key = video.stem.replace(" ", "_").replace("!", "").replace("?", "")[:32]
    edl = {
        "version": 1,
        "sources": {src_key: str(video).replace("\\", "/")},
        "ranges": [
            {
                "source": src_key,
                "start": round(max(0.0, s - head_pad), 3),
                "end": round(min(source_dur, e + tail_pad), 3),
                "beat": f"block_{i:03d}",
                "quote": t[:100],
            }
            for i, (s, e, t) in enumerate(ranges)
        ],
        "grade": None,
        "overlays": [],
    }
    out = edit_dir / "edl.json"
    out.write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggressive single-source Vietnamese auto-cut")
    ap.add_argument("video", type=Path)
    ap.add_argument("--language", default="vi")
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP)
    ap.add_argument("--head-pad", type=float, default=DEFAULT_HEAD_PAD)
    ap.add_argument("--tail-pad", type=float, default=DEFAULT_TAIL_PAD)
    ap.add_argument("--slate-end", type=float, default=None,
                    help="Drop everything before this many seconds. Default: auto from first hybrid word.")
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--llm-review", action="store_true",
                    help="(standalone/headless ONLY) opt-in to Gemini Flash cross-block review. "
                         "DEFAULT OFF. When Claude drives in-loop, Claude does retake removal itself "
                         "via apply_retake_cuts.py — do NOT use Gemini.")
    ap.add_argument("--no-llm-review", action="store_true",
                    help="(deprecated; LLM review is OFF by default now)")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")
    edit_dir = (video.parent / "edit").resolve()
    edit_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] Transcribe: {video.name}")
    api = load_api_key()
    orig_path, clean_path = transcribe_dual(video, edit_dir, args.language, api)

    print(f"[2/5] Build hybrid word list")
    hybrid, duration = build_hybrid_words(orig_path, clean_path)
    slate_end = args.slate_end if args.slate_end is not None else auto_slate_end(hybrid)
    print(f"  source duration: {duration:.1f}s, slate ends at: {slate_end:.2f}s")

    print(f"[3/5] Compute cut plan (gap>={args.gap}s)")
    # LLM review is OFF by default (2026-06-20). Opt-in only for standalone runs.
    # In-loop LLM (Claude) should do semantic retake removal via apply_retake_cuts.py.
    llm_key = load_gemini_key() if args.llm_review else ""
    if args.llm_review and not llm_key:
        print("  warn: GEMINI_API_KEY not found → LLM review disabled (rule-based dedupe only)")
    if not args.llm_review:
        print("  LLM review OFF (default). For semantic retake removal (cross+intra block),")
        print("  drive with Claude + helpers/apply_retake_cuts.py — do NOT call Gemini.")
    cache_path = edit_dir / "llm-review-cache.json"
    llm_cache: dict = {}
    if cache_path.exists():
        try:
            llm_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            llm_cache = {}

    ranges, stats = compute_cut_plan(
        hybrid, args.gap, slate_end, args.head_pad, args.tail_pad,
        llm_key=llm_key, llm_cache=llm_cache,
    )

    if llm_key:
        cache_path.write_text(json.dumps(llm_cache, ensure_ascii=False, indent=2), encoding="utf-8")

    out_dur = sum(e - s + args.head_pad + args.tail_pad for s, e, _ in ranges)
    print(f"  blocks: {stats['blocks_kept']}/{stats['blocks_input']} kept")
    print(f"  skip: pure_filler={stats['skip_pure_filler']} cutoff={stats['skip_cutoff']} short={stats['skip_short']}")
    print(f"  intra-block dedupe: dropped {stats.get('intra_stutter_words_dropped', 0)} stuttered words")
    print(f"  dedupe: rule={stats['deduped_rule']} llm={stats['deduped_llm']} (llm_calls={stats['llm_calls']}, kept_both={stats['llm_keep_both']})")
    print(f"  estimated output: {out_dur:.1f}s ({out_dur/60:.2f}min) — cut {(duration-out_dur)/duration*100:.1f}%")

    print(f"[4/5] Write EDL")
    edl_path = write_edl(ranges, video, duration, args.head_pad, args.tail_pad, edit_dir)
    print(f"  → {edl_path}")

    if args.no_render:
        print("[5/5] Skip render (--no-render)")
        return

    out = args.output or (edit_dir / "final.mp4")
    print(f"[5/5] Render → {out.name}")
    skill_dir = Path(__file__).resolve().parent
    cmd = [
        sys.executable, str(skill_dir / "render.py"),
        str(edl_path),
        "-o", str(out),
        "--no-subtitles",
    ]
    subprocess.run(cmd, check=True)
    print(f"\nDONE: {out}")


if __name__ == "__main__":
    main()
