"""Variant 3 (Sonoix + LLM) and 3b-Sonoix (Sonoix + difflib) experiment.

Two methods compared on the same Sonoix BPE token stream:

  LLM method:
    A. Transcribe vocals with Sonoix -> BPE tokens with ms-level timings
    B. Chunk lyrics into groups of 3 lines; for each chunk ask GPT-4o-mini to
       remap the corresponding tokens so the text matches the known lyrics.
    C. Assemble corrected tokens into SyllableTiming list with line markers.

  difflib method (3b-Sonoix):
    Same Sonoix tokens, but alignment uses difflib.SequenceMatcher instead of
    LLM.  No API cost, deterministic, useful as a baseline for the LLM approach.

Both methods evaluate against reference_timings.json (MAE, hit-rate, WER).

Usage:
    source /home/croked/miniforge3/etc/profile.d/conda.sh && conda activate bootstrap
    pip install openai python-dotenv  # if not installed yet
    python /home/croked/karaoke/m3_test/variant3/experiment_sonoix.py

Idempotent: if test_data/{N}/sonoix_tokens.json already exists the Sonoix
transcription step is skipped for that track.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Project path setup
# ---------------------------------------------------------------------------
sys.path.insert(0, "/home/croked/karaoke/v2/worker")
sys.path.insert(0, "/home/croked/karaoke/v2/shared")

from app.pipeline.sonoix_client import SonoixClient  # noqa: E402
from karaoke_shared.models.track import SyllableTiming  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_ENV_PATH = Path("/home/croked/karaoke/m3_test/variant3/.env")
load_dotenv(_ENV_PATH)

SONOIX_API_KEY: str = os.environ["SONOIX_API_KEY"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]

TEST_DATA_ROOT = Path("/home/croked/karaoke/m3_test/test_data")
RESULTS_DIR = Path("/home/croked/karaoke/m3_test/variant3/results")
TRACK_IDS = [1, 2, 3, 4, 5]

# Number of lyrics lines to send to GPT-4o-mini per request.
LINES_PER_CHUNK = 3

# Timing tolerance for hit-rate metric.
HIT_THRESHOLD_SEC = 0.1


# ---------------------------------------------------------------------------
# Step A: Sonoix transcription
# ---------------------------------------------------------------------------

async def _transcribe_with_sonoix(vocals_path: Path) -> list[dict]:
    """Call Sonoix API and return raw tokens as plain dicts.

    Returns a list of {"text": str, "start_ms": int, "end_ms": int}.
    """
    client = SonoixClient(api_key=SONOIX_API_KEY)
    result = await client.transcribe(str(vocals_path))

    tokens = []
    for t in result.tokens:
        tokens.append({
            "text": t.text,
            "start_ms": t.start_ms,
            "end_ms": t.end_ms,
            "confidence": t.confidence,
            "language": t.language,
        })
    return tokens


def get_sonoix_tokens(track_dir: Path) -> tuple[list[dict], bool]:
    """Return Sonoix tokens for a track, loading from cache when available.

    Returns:
        (tokens, from_cache) where from_cache is True when the cached file
        was used and no API call was made.
    """
    cache_path = track_dir / "sonoix_tokens.json"

    if cache_path.exists():
        print("    [A] sonoix_tokens.json found — skipping API call")
        tokens = json.loads(cache_path.read_text(encoding="utf-8"))
        return tokens, True

    vocals_path = track_dir / "vocals.wav"
    print(f"    [A] Uploading {vocals_path.name} to Sonoix ...")
    t0 = time.time()
    tokens = asyncio.run(_transcribe_with_sonoix(vocals_path))
    elapsed = time.time() - t0
    print(f"    [A] Done: {len(tokens)} tokens in {elapsed:.1f}s")

    cache_path.write_text(
        json.dumps(tokens, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    sonoix_text = "".join(t["text"] for t in tokens)
    (track_dir / "sonoix_text.txt").write_text(sonoix_text, encoding="utf-8")

    return tokens, False


# ---------------------------------------------------------------------------
# Shared helpers: token grouping and word/line utilities
# ---------------------------------------------------------------------------

def _normalize(word: str) -> str:
    """Lowercase and strip non-word characters for comparison."""
    return re.sub(r"[^\w]", "", word, flags=re.UNICODE).lower()


def group_tokens_into_words(tokens: list[dict]) -> list[dict]:
    """Merge BPE tokens into word groups.

    A new word starts whenever a token's text begins with a space character.
    Each returned dict has:
        word        - concatenated text (leading space stripped)
        tokens      - the constituent BPE tokens
        start_ms    - earliest start_ms
        end_ms      - latest end_ms
    """
    words: list[dict] = []
    current_tokens: list[dict] = []

    for token in tokens:
        text = token["text"]
        is_word_start = text.startswith(" ")

        if is_word_start and current_tokens:
            _flush_word(current_tokens, words)
            current_tokens = []

        current_tokens.append(token)

    if current_tokens:
        _flush_word(current_tokens, words)

    return words


def _flush_word(token_group: list[dict], out: list[dict]) -> None:
    """Append a completed word group to `out`."""
    combined_text = "".join(t["text"] for t in token_group).lstrip()
    out.append({
        "word": combined_text,
        "tokens": token_group,
        "start_ms": token_group[0]["start_ms"],
        "end_ms": token_group[-1]["end_ms"],
    })


def lyrics_to_lines_and_words(lyrics_text: str) -> tuple[list[str], list[str]]:
    """Return (non-empty lines, flat word list) from a lyrics string."""
    lines = [line for line in lyrics_text.splitlines() if line.strip()]
    words: list[str] = []
    for line in lines:
        words.extend(line.split())
    return lines, words


# ---------------------------------------------------------------------------
# Estimate how many Sonoix words correspond to a group of lyrics lines.
# We use a simple ratio: sonoix_word_count / known_word_count * line_word_count.
# ---------------------------------------------------------------------------

def estimate_sonoix_word_count_for_lines(
    known_words: list[str],
    sonoix_words: list[dict],
    line_words: list[str],
) -> int:
    """Estimate how many Sonoix words cover the given lyrics line words."""
    ratio = len(sonoix_words) / max(len(known_words), 1)
    estimated = round(len(line_words) * ratio)
    # Keep within bounds.
    return max(1, min(estimated, len(sonoix_words)))


# ---------------------------------------------------------------------------
# Step B-LLM: GPT-4o-mini correction
# ---------------------------------------------------------------------------

def _build_llm_prompt(correct_lines: list[str], tokens_chunk: list[dict]) -> str:
    """Build the GPT-4o-mini prompt for one chunk of lyrics lines."""
    correct_text = "\n".join(correct_lines)
    tokens_json = json.dumps(
        [{"text": t["text"], "start_ms": t["start_ms"], "end_ms": t["end_ms"]}
         for t in tokens_chunk],
        ensure_ascii=False,
    )
    return (
        "Ты — корректор текста для караоке-системы.\n"
        "\n"
        "Тебе дан правильный текст песни и список BPE-токенов от ASR (Sonoix).\n"
        "Каждый токен — часть слова (примерно слог) с таймингом в миллисекундах.\n"
        "Токен, чей text начинается с пробела — начало нового слова.\n"
        "\n"
        "Твоя задача: вернуть скорректированный список токенов, где:\n"
        "- text токенов изменён так, чтобы конкатенация совпадала с правильным текстом\n"
        "- start_ms и end_ms оригинальных токенов сохраняются максимально точно\n"
        "- если нужно добавить токен (ASR пропустил слово) — интерполируй тайминг\n"
        "- если нужно удалить лишний токен — убери его\n"
        "- пробел в начале text означает начало слова — сохраняй эту семантику\n"
        "- Возвращай ТОЛЬКО JSON в формате {\"tokens\": [...]}, без пояснений\n"
        "\n"
        "ПРАВИЛЬНЫЙ ТЕКСТ:\n"
        f"{correct_text}\n"
        "\n"
        "ТОКЕНЫ ОТ ASR (JSON):\n"
        f"{tokens_json}\n"
        "\n"
        "Верни JSON объект с полем \"tokens\" — массив скорректированных токенов:\n"
        "[{\"text\": \" лю\", \"start_ms\": 12340, \"end_ms\": 12560}, ...]"
    )


def _call_gpt(prompt: str, openai_client) -> tuple[list[dict], int, int]:
    """Send one prompt to GPT-4o-mini and return (tokens, input_tokens, output_tokens)."""
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    usage = response.usage
    raw = response.choices[0].message.content
    parsed = json.loads(raw)

    # The model should return {"tokens": [...]} but handle bare list too.
    if isinstance(parsed, list):
        tokens = parsed
    elif "tokens" in parsed:
        tokens = parsed["tokens"]
    else:
        # Try the first list value in the dict.
        for v in parsed.values():
            if isinstance(v, list):
                tokens = v
                break
        else:
            raise ValueError(f"Unexpected LLM JSON structure: {list(parsed.keys())}")

    return tokens, usage.prompt_tokens, usage.completion_tokens


def _validate_tokens(tokens: list[dict], label: str) -> bool:
    """Check that a token list has required fields and monotone start_ms."""
    for i, t in enumerate(tokens):
        if not all(k in t for k in ("text", "start_ms", "end_ms")):
            print(f"    [B-LLM] Validation FAIL ({label}): token {i} missing fields: {t}")
            return False
        if t["start_ms"] > t["end_ms"]:
            print(f"    [B-LLM] Validation FAIL ({label}): start_ms > end_ms at token {i}")
            return False
    # Check monotone order.
    for i in range(1, len(tokens)):
        if tokens[i]["start_ms"] < tokens[i - 1]["start_ms"]:
            print(
                f"    [B-LLM] Validation FAIL ({label}): out-of-order at tokens "
                f"{i - 1}/{i}: {tokens[i - 1]['start_ms']} > {tokens[i]['start_ms']}"
            )
            return False
    return True


def correct_tokens_with_llm(
    sonoix_tokens: list[dict],
    lyrics_lines: list[str],
    known_words: list[str],
    openai_client,
) -> tuple[list[dict], dict]:
    """Correct Sonoix BPE tokens using GPT-4o-mini, processing in line chunks.

    Returns:
        (corrected_tokens, cost_info) where cost_info has token counts and
        estimated USD cost.
    """
    sonoix_words = group_tokens_into_words(sonoix_tokens)

    total_input_tokens = 0
    total_output_tokens = 0
    all_corrected: list[dict] = []

    # Walk through lyrics lines in chunks of LINES_PER_CHUNK, consuming
    # Sonoix words proportionally.
    sonoix_word_cursor = 0
    known_word_cursor = 0

    line_cursor = 0
    while line_cursor < len(lyrics_lines):
        chunk_lines = lyrics_lines[line_cursor: line_cursor + LINES_PER_CHUNK]
        line_cursor += LINES_PER_CHUNK

        chunk_known_words = []
        for line in chunk_lines:
            chunk_known_words.extend(line.split())

        # Estimate how many Sonoix words this chunk covers.
        remaining_known = len(known_words) - known_word_cursor
        remaining_sonoix = len(sonoix_words) - sonoix_word_cursor
        chunk_sonoix_count = estimate_sonoix_word_count_for_lines(
            known_words[known_word_cursor:],
            sonoix_words[sonoix_word_cursor:],
            chunk_known_words,
        )

        # If this is the last chunk, take all remaining Sonoix words.
        if line_cursor >= len(lyrics_lines):
            chunk_sonoix_count = remaining_sonoix

        # Collect the BPE tokens for this chunk's Sonoix words.
        chunk_sonoix_words = sonoix_words[sonoix_word_cursor: sonoix_word_cursor + chunk_sonoix_count]
        chunk_tokens: list[dict] = []
        for w in chunk_sonoix_words:
            chunk_tokens.extend(w["tokens"])

        sonoix_word_cursor += chunk_sonoix_count
        known_word_cursor += len(chunk_known_words)

        if not chunk_tokens:
            # Interpolate: no Sonoix tokens cover this chunk — generate
            # placeholder tokens using the time between adjacent corrected
            # tokens.
            print(f"    [B-LLM] Chunk {line_cursor // LINES_PER_CHUNK}: no Sonoix tokens, generating placeholders")
            placeholders = _generate_placeholder_tokens(
                chunk_known_words, all_corrected
            )
            all_corrected.extend(placeholders)
            continue

        prompt = _build_llm_prompt(chunk_lines, chunk_tokens)
        chunk_label = f"lines {line_cursor - len(chunk_lines) + 1}-{line_cursor}"

        corrected_chunk: list[dict] | None = None
        for attempt in range(2):
            try:
                tokens_out, inp, outp = _call_gpt(prompt, openai_client)
                total_input_tokens += inp
                total_output_tokens += outp

                if _validate_tokens(tokens_out, chunk_label):
                    corrected_chunk = tokens_out
                    break
                else:
                    print(f"    [B-LLM] Attempt {attempt + 1} failed validation for {chunk_label}, retrying...")
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                print(f"    [B-LLM] Attempt {attempt + 1} JSON error for {chunk_label}: {exc}")

        if corrected_chunk is None:
            print(f"    [B-LLM] Both attempts failed for {chunk_label} — using original tokens")
            corrected_chunk = chunk_tokens

        all_corrected.extend(corrected_chunk)

    # GPT-4o-mini pricing as of 2026-03 (input $0.15/1M, output $0.60/1M).
    cost_usd = (total_input_tokens * 0.15 + total_output_tokens * 0.60) / 1_000_000

    cost_info = {
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cost_usd": round(cost_usd, 6),
    }

    print(
        f"    [B-LLM] Done: {len(all_corrected)} corrected tokens | "
        f"in={total_input_tokens} out={total_output_tokens} "
        f"cost=${cost_usd:.4f}"
    )
    return all_corrected, cost_info


def _generate_placeholder_tokens(
    words: list[str],
    preceding_tokens: list[dict],
) -> list[dict]:
    """Create zero-duration placeholder tokens when Sonoix missed a segment."""
    # Anchor time: end of last known token, or 0.
    anchor_ms = preceding_tokens[-1]["end_ms"] if preceding_tokens else 0

    placeholders = []
    for i, word in enumerate(words):
        prefix = " " if i > 0 else ""
        placeholders.append({
            "text": prefix + word,
            "start_ms": anchor_ms,
            "end_ms": anchor_ms,
        })
    return placeholders


# ---------------------------------------------------------------------------
# Step B-difflib: deterministic alignment on Sonoix BPE tokens
# ---------------------------------------------------------------------------

def correct_tokens_with_difflib(
    sonoix_tokens: list[dict],
    lyrics_lines: list[str],
    known_words: list[str],
) -> list[dict]:
    """Align Sonoix BPE tokens to known lyrics using difflib.SequenceMatcher.

    Strategy:
    1. Group BPE tokens into words.
    2. Match Sonoix words to known words via SequenceMatcher.
    3. equal  -> keep original BPE tokens, replace text with known spelling
    4. replace -> spread the time span of the Sonoix block across known words
                  as single-token words (one token per known word)
    5. insert  -> interpolate timing between neighbours
    6. delete  -> skip the extra Sonoix tokens
    """
    sonoix_words = group_tokens_into_words(sonoix_tokens)

    sonoix_normalized = [_normalize(w["word"]) for w in sonoix_words]
    known_normalized = [_normalize(w) for w in known_words]

    matcher = difflib.SequenceMatcher(
        None, sonoix_normalized, known_normalized, autojunk=False
    )
    opcodes = matcher.get_opcodes()

    # One slot per known word; each slot will become a list of tokens.
    result_word_slots: list[list[dict] | None] = [None] * len(known_words)

    for tag, s_i1, s_i2, k_i1, k_i2 in opcodes:
        if tag == "equal":
            # Keep original BPE tokens but overwrite text with correct spelling
            # for any minor casing/punctuation differences.
            for offset in range(k_i2 - k_i1):
                k_idx = k_i1 + offset
                s_idx = s_i1 + offset
                sonoix_word = sonoix_words[s_idx]
                # Rebuild tokens keeping original timings, fix first token's
                # leading space to canonical form.
                fixed_tokens = _fix_token_texts_for_word(
                    sonoix_word["tokens"], known_words[k_idx]
                )
                result_word_slots[k_idx] = fixed_tokens

        elif tag == "replace":
            # Time span of all Sonoix words in this block.
            span_start_ms = sonoix_words[s_i1]["start_ms"]
            span_end_ms = sonoix_words[s_i2 - 1]["end_ms"]
            block_known_words = known_words[k_i1:k_i2]
            new_slots = _distribute_span_to_words(
                block_known_words, span_start_ms, span_end_ms
            )
            for offset, slot in enumerate(new_slots):
                result_word_slots[k_i1 + offset] = slot

        elif tag == "delete":
            # Extra Sonoix words — discard, no known word slot to fill.
            pass

        elif tag == "insert":
            # Known words absent from Sonoix — leave as None for interpolation.
            pass

    # Interpolate None slots (inserts that had no Sonoix timing).
    _interpolate_none_slots(result_word_slots, known_words)

    # Flatten to a token list, adding word-start spaces.
    corrected: list[dict] = []
    for slot in result_word_slots:
        if slot:
            corrected.extend(slot)

    return corrected


def _fix_token_texts_for_word(
    original_tokens: list[dict],
    correct_word: str,
) -> list[dict]:
    """Return tokens with timings preserved but text reassigned to correct_word.

    The first token gets a leading space (word-start marker in BPE convention),
    unless it already has one.  The remaining tokens carry the rest of the
    correct word's text, split proportionally by character length.
    """
    if not original_tokens:
        return []

    # Split correct word characters across original token count proportionally.
    n = len(original_tokens)
    chars = list(correct_word)
    chunk_size = max(1, len(chars) // n)

    fixed: list[dict] = []
    char_cursor = 0
    for i, orig in enumerate(original_tokens):
        if i < n - 1:
            chunk = chars[char_cursor: char_cursor + chunk_size]
            char_cursor += chunk_size
        else:
            chunk = chars[char_cursor:]

        text = "".join(chunk)
        if i == 0:
            text = " " + text  # word-start marker

        fixed.append({
            "text": text,
            "start_ms": orig["start_ms"],
            "end_ms": orig["end_ms"],
        })

    return fixed


def _distribute_span_to_words(
    words: list[str],
    span_start_ms: int,
    span_end_ms: int,
) -> list[list[dict]]:
    """Divide a time span proportionally across a list of words.

    Returns one single-token slot per word.
    """
    if not words:
        return []

    char_lengths = [max(len(_normalize(w)), 1) for w in words]
    total_chars = sum(char_lengths)
    span_ms = span_end_ms - span_start_ms
    cursor_ms = span_start_ms

    slots: list[list[dict]] = []
    for i, word in enumerate(words):
        fraction = char_lengths[i] / total_chars
        word_end_ms = cursor_ms + round(span_ms * fraction)
        prefix = " " if i > 0 else ""
        slots.append([{
            "text": prefix + word,
            "start_ms": cursor_ms,
            "end_ms": word_end_ms,
        }])
        cursor_ms = word_end_ms

    return slots


def _interpolate_none_slots(
    slots: list[list[dict] | None],
    known_words: list[str],
) -> None:
    """Fill None slots in-place by linear interpolation between neighbours."""
    n = len(slots)
    if n == 0:
        return

    # Find boundary indices for clamping edge cases.
    first_valid = next((i for i, s in enumerate(slots) if s is not None), None)
    if first_valid is None:
        # Nothing at all — assign zero-duration at t=0.
        for i, word in enumerate(known_words):
            prefix = " " if i > 0 else ""
            slots[i] = [{"text": prefix + word, "start_ms": 0, "end_ms": 0}]
        return

    last_valid = next(
        (n - 1 - i for i, s in enumerate(reversed(slots)) if s is not None),
        first_valid,
    )

    i = 0
    while i < n:
        if slots[i] is not None:
            i += 1
            continue

        gap_start = i
        while i < n and slots[i] is None:
            i += 1
        gap_end = i  # exclusive

        # Left boundary end_ms.
        if gap_start == 0:
            left_ms = slots[last_valid][-1]["end_ms"]  # type: ignore[index]
        else:
            left_ms = slots[gap_start - 1][-1]["end_ms"]  # type: ignore[index]

        # Right boundary start_ms.
        if gap_end >= n:
            right_ms = left_ms
        else:
            right_ms = slots[gap_end][0]["start_ms"]  # type: ignore[index]

        gap_size = gap_end - gap_start
        step_ms = (right_ms - left_ms) // (gap_size + 1)

        for offset in range(gap_size):
            idx = gap_start + offset
            word = known_words[idx]
            prefix = " " if idx > 0 else ""
            word_start = left_ms + step_ms * (offset + 1)
            word_end = left_ms + step_ms * (offset + 2)
            slots[idx] = [{"text": prefix + word, "start_ms": word_start, "end_ms": word_end}]


# ---------------------------------------------------------------------------
# Step C: Assemble SyllableTiming from corrected BPE tokens
# ---------------------------------------------------------------------------

def tokens_to_syllable_timings(
    corrected_tokens: list[dict],
    lyrics_lines: list[str],
    known_words: list[str],
) -> list[SyllableTiming]:
    """Convert corrected BPE token list into SyllableTiming objects.

    Inserts newline markers at line boundaries by tracking how many
    word-start tokens (those with a leading space) have been consumed
    relative to the cumulative word counts per line.
    """
    # Build a list of cumulative word counts at each line end.
    line_end_word_counts: list[int] = []
    cumulative = 0
    for line in lyrics_lines:
        cumulative += len(line.split())
        line_end_word_counts.append(cumulative)

    timings: list[SyllableTiming] = []
    word_count = 0       # how many words (word-start tokens) we've seen
    line_idx = 0         # which line boundary we're tracking
    is_first_token = True

    for token in corrected_tokens:
        text = token["text"]
        if not text.strip():
            continue

        is_word_start = text.startswith(" ")

        if is_word_start:
            word_count += 1

            # Check if we've crossed into the next line.
            while (
                line_idx < len(line_end_word_counts)
                and word_count > line_end_word_counts[line_idx]
            ):
                line_idx += 1

            # Determine the correct prefix for this token.
            if is_first_token:
                # First syllable of the whole track: no prefix.
                text = text.lstrip()
            else:
                # Check if the *previous* word was the last of a line.
                prev_word_count = word_count - 1
                prev_line_idx = 0
                for j, end_count in enumerate(line_end_word_counts):
                    if prev_word_count <= end_count:
                        prev_line_idx = j
                        break

                if prev_line_idx < line_idx:
                    # We just moved to a new line.
                    text = "\n" + text.lstrip()
                # else: keep the leading space (same line, new word)

        timings.append(SyllableTiming(
            syllable=text,
            start=token["start_ms"] / 1000.0,
            end=token["end_ms"] / 1000.0,
        ))
        is_first_token = False

    return timings


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def compute_wer(hypothesis_words: list[str], reference_words: list[str]) -> float:
    """Compute Word Error Rate via dynamic programming edit distance."""
    n = len(reference_words)
    m = len(hypothesis_words)

    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if reference_words[i - 1] == hypothesis_words[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])

    return dp[n][m] / max(n, 1)


def words_from_timings(timings: list[SyllableTiming]) -> list[str]:
    """Reconstruct a normalized word list from a SyllableTiming list."""
    full_text = "".join(t.syllable for t in timings).replace("\n", " ")
    return [_normalize(w) for w in full_text.split() if _normalize(w)]


def words_from_tokens(tokens: list[dict]) -> list[str]:
    """Reconstruct a normalized word list from a raw token list."""
    full_text = "".join(t["text"] for t in tokens)
    return [_normalize(w) for w in full_text.split() if _normalize(w)]


def evaluate_timings(
    predicted: list[SyllableTiming],
    reference: list[SyllableTiming],
    hit_threshold: float = HIT_THRESHOLD_SEC,
) -> dict:
    """Compare predicted syllable timings to reference by position.

    Returns mae, hit_rate, alignment_count, and a table of the first 20 rows.
    """
    compare_count = min(len(predicted), len(reference))
    if compare_count == 0:
        return {"mae": None, "hit_rate": None, "alignment_count": 0, "table": []}

    deltas = []
    table_rows = []

    for i in range(compare_count):
        ref = reference[i]
        pred = predicted[i]
        delta = abs(pred.start - ref.start)
        deltas.append(delta)

        if i < 20:
            table_rows.append({
                "syllable_ref": ref.syllable,
                "syllable_pred": pred.syllable,
                "ref_start": round(ref.start, 3),
                "pred_start": round(pred.start, 3),
                "delta": round(delta, 3),
            })

    mae = sum(deltas) / len(deltas)
    hit_rate = sum(1 for d in deltas if d < hit_threshold) / len(deltas)

    return {
        "mae": round(mae, 4),
        "hit_rate": round(hit_rate, 4),
        "alignment_count": compare_count,
        "table": table_rows,
    }


def print_comparison_table(table: list[dict]) -> None:
    """Print the first-20-syllables comparison table to stdout."""
    print(f"    {'Ref syllable':<18} {'Pred syllable':<18} {'Ref start':>10} {'Pred start':>10} {'Delta':>8}")
    print(f"    {'-'*18} {'-'*18} {'-'*10} {'-'*10} {'-'*8}")
    for row in table:
        ref_s = repr(row["syllable_ref"])[:17]
        pred_s = repr(row["syllable_pred"])[:17]
        print(
            f"    {ref_s:<18} {pred_s:<18} "
            f"{row['ref_start']:>10.3f} {row['pred_start']:>10.3f} {row['delta']:>8.3f}"
        )


# ---------------------------------------------------------------------------
# Per-track pipeline
# ---------------------------------------------------------------------------

def process_track(track_id: int, track_dir: Path, openai_client) -> dict:
    """Run the full Sonoix + LLM and Sonoix + difflib pipelines for one track.

    Returns a dict with metrics for both methods.
    """
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  Track {track_id}: {track_dir.name}")
    print(sep)

    # Load track metadata.
    meta = json.loads((track_dir / "meta.json").read_text(encoding="utf-8"))
    language = meta["language"]
    lyrics_text = (track_dir / "lyrics.txt").read_text(encoding="utf-8")
    lyrics_lines, known_words = lyrics_to_lines_and_words(lyrics_text)

    reference_data = json.loads(
        (track_dir / "reference_timings.json").read_text(encoding="utf-8")
    )
    reference_timings = [SyllableTiming(**item) for item in reference_data]

    print(f"  Artist: {meta['artist']} — {meta['title']}")
    print(f"  Language: {language}")
    print(f"  Known words: {len(known_words)}, reference syllables: {len(reference_timings)}")

    # ------------------------------------------------------------------
    # Step A: Sonoix transcription (cached)
    # ------------------------------------------------------------------
    print("\n  [A] Sonoix transcription")
    sonoix_start = time.time()
    sonoix_tokens, from_cache = get_sonoix_tokens(track_dir)
    sonoix_elapsed = 0.0 if from_cache else time.time() - sonoix_start

    sonoix_word_count = len(group_tokens_into_words(sonoix_tokens))
    sonoix_words_normalized = words_from_tokens(sonoix_tokens)
    known_normalized = [_normalize(w) for w in known_words]
    sonoix_wer = compute_wer(sonoix_words_normalized, known_normalized)

    print(f"  Sonoix tokens: {len(sonoix_tokens)}, words: {sonoix_word_count}")
    print(f"  Sonoix WER (raw): {sonoix_wer:.1%}")

    # ------------------------------------------------------------------
    # Step B-LLM: GPT-4o-mini correction
    # ------------------------------------------------------------------
    print("\n  [B-LLM] GPT-4o-mini correction")
    llm_start = time.time()
    llm_tokens, cost_info = correct_tokens_with_llm(
        sonoix_tokens, lyrics_lines, known_words, openai_client
    )
    llm_elapsed = time.time() - llm_start

    # Step C-LLM: assemble SyllableTiming
    llm_timings = tokens_to_syllable_timings(llm_tokens, lyrics_lines, known_words)
    llm_metrics = evaluate_timings(llm_timings, reference_timings)

    llm_pred_words = words_from_timings(llm_timings)
    ref_words = words_from_timings(reference_timings)
    llm_wer = compute_wer(llm_pred_words, ref_words)

    print(f"  [LLM] Syllables: {len(llm_timings)}, elapsed: {llm_elapsed:.1f}s")
    print(
        f"  [LLM] MAE: {llm_metrics['mae']}s  "
        f"Hit rate: {llm_metrics['hit_rate']:.1%}  "
        f"WER: {llm_wer:.1%}  "
        f"Cost: ${cost_info['cost_usd']:.4f}"
    )
    print("  [LLM] First 20 syllables:")
    print_comparison_table(llm_metrics["table"])

    # ------------------------------------------------------------------
    # Step B-difflib: deterministic alignment on Sonoix tokens
    # ------------------------------------------------------------------
    print("\n  [B-difflib] Deterministic alignment")
    difflib_start = time.time()
    difflib_tokens = correct_tokens_with_difflib(
        sonoix_tokens, lyrics_lines, known_words
    )
    difflib_elapsed = time.time() - difflib_start

    # Step C-difflib: assemble SyllableTiming
    difflib_timings = tokens_to_syllable_timings(difflib_tokens, lyrics_lines, known_words)
    difflib_metrics = evaluate_timings(difflib_timings, reference_timings)

    difflib_pred_words = words_from_timings(difflib_timings)
    difflib_wer = compute_wer(difflib_pred_words, ref_words)

    print(f"  [difflib] Syllables: {len(difflib_timings)}, elapsed: {difflib_elapsed:.1f}s")
    print(
        f"  [difflib] MAE: {difflib_metrics['mae']}s  "
        f"Hit rate: {difflib_metrics['hit_rate']:.1%}  "
        f"WER: {difflib_wer:.1%}"
    )
    print("  [difflib] First 20 syllables:")
    print_comparison_table(difflib_metrics["table"])

    return {
        "track_id": track_id,
        "artist": meta["artist"],
        "title": meta["title"],
        "language": language,
        "sonoix_token_count": len(sonoix_tokens),
        "sonoix_word_count": sonoix_word_count,
        "sonoix_wer_raw": round(sonoix_wer, 4),
        "sonoix_elapsed_sec": round(sonoix_elapsed, 1),
        "reference_syllable_count": len(reference_timings),
        "llm": {
            "syllable_count": len(llm_timings),
            "mae": llm_metrics["mae"],
            "hit_rate_01s": llm_metrics["hit_rate"],
            "alignment_count": llm_metrics["alignment_count"],
            "wer": round(llm_wer, 4),
            "elapsed_sec": round(llm_elapsed, 1),
            "cost_usd": cost_info["cost_usd"],
            "input_tokens": cost_info["input_tokens"],
            "output_tokens": cost_info["output_tokens"],
            "comparison_table": llm_metrics["table"],
        },
        "difflib": {
            "syllable_count": len(difflib_timings),
            "mae": difflib_metrics["mae"],
            "hit_rate_01s": difflib_metrics["hit_rate"],
            "alignment_count": difflib_metrics["alignment_count"],
            "wer": round(difflib_wer, 4),
            "elapsed_sec": round(difflib_elapsed, 1),
            "comparison_table": difflib_metrics["table"],
        },
    }


# ---------------------------------------------------------------------------
# Summary writing
# ---------------------------------------------------------------------------

def write_summary(results: list[dict]) -> None:
    """Write a human-readable summary comparing both methods."""
    successful = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]

    lines = [
        "Experiment: Sonoix + LLM (GPT-4o-mini) vs Sonoix + difflib",
        "=" * 60,
        "",
        f"Tracks processed: {len(successful)}/{len(results)} successful",
        "",
    ]

    # Header row.
    lines.append(
        f"  {'Track':<5} {'Artist/Title':<35} {'Lang':<5} "
        f"{'LLM MAE':>8} {'LLM Hit%':>9} {'LLM WER':>8} {'LLM $':>7} | "
        f"{'dif MAE':>8} {'dif Hit%':>9} {'dif WER':>8}"
    )
    lines.append("  " + "-" * 110)

    for r in successful:
        lm = r["llm"]
        dm = r["difflib"]
        label = f"{r['artist']} — {r['title']}"[:34]
        lines.append(
            f"  {r['track_id']:<5} {label:<35} {r['language']:<5} "
            f"{lm['mae']:>8.4f} {lm['hit_rate_01s']:>8.1%} {lm['wer']:>7.1%} "
            f"${lm['cost_usd']:>5.4f} | "
            f"{dm['mae']:>8.4f} {dm['hit_rate_01s']:>8.1%} {dm['wer']:>7.1%}"
        )

    lines.append("")

    if successful:
        def avg(key_path: list[str]) -> float:
            total = 0.0
            count = 0
            for r in successful:
                val = r
                for k in key_path:
                    val = val[k]
                if val is not None:
                    total += val
                    count += 1
            return total / max(count, 1)

        lines.append("Averages:")
        lines.append(
            f"  LLM:     MAE={avg(['llm','mae']):.4f}s  "
            f"Hit={avg(['llm','hit_rate_01s']):.1%}  "
            f"WER={avg(['llm','wer']):.1%}  "
            f"Cost/track=${avg(['llm','cost_usd']):.4f}"
        )
        lines.append(
            f"  difflib: MAE={avg(['difflib','mae']):.4f}s  "
            f"Hit={avg(['difflib','hit_rate_01s']):.1%}  "
            f"WER={avg(['difflib','wer']):.1%}"
        )
        lines.append("")
        lines.append("Cost projection (LLM method):")
        cost_per_track = avg(["llm", "cost_usd"])
        for n in [1000, 5000, 17000]:
            lines.append(f"  {n:>6} tracks: ${cost_per_track * n:.2f}")

        lines.append("")
        lines.append("Sonoix WER (raw ASR, before any correction):")
        for r in successful:
            lines.append(f"  Track {r['track_id']}: {r['sonoix_wer_raw']:.1%}")

    lines.append("")
    lines.append("Interpretation:")
    lines.append("  MAE < 0.1s  = good timing (karaoke usable)")
    lines.append("  MAE < 0.05s = excellent timing")
    lines.append("  WER < 2%    = text nearly perfect (Variant 3 target)")
    lines.append("  WER < 5%    = text acceptable")

    if failed:
        lines.append("")
        lines.append("Failed tracks:")
        for r in failed:
            lines.append(f"  Track {r['track_id']}: {r.get('error', 'unknown')}")

    summary_text = "\n".join(lines)
    summary_path = RESULTS_DIR / "summary_sonoix.txt"
    summary_path.write_text(summary_text, encoding="utf-8")
    print(f"\nSummary saved to {summary_path}")
    print("\n" + summary_text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the Sonoix experiment on all 5 test tracks."""
    from openai import OpenAI

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

    all_results: list[dict] = []

    for track_id in TRACK_IDS:
        track_dir = TEST_DATA_ROOT / str(track_id)
        if not track_dir.exists():
            print(f"Track {track_id} directory not found, skipping.")
            continue

        try:
            result = process_track(track_id, track_dir, openai_client)
            all_results.append(result)
        except Exception as exc:
            import traceback
            print(f"\nERROR processing track {track_id}: {exc}")
            traceback.print_exc()
            all_results.append({"track_id": track_id, "error": str(exc)})

    # Save per-method JSON files.
    llm_results = [
        {**{k: v for k, v in r.items() if k != "difflib"}}
        for r in all_results
        if "error" not in r
    ]
    difflib_results = [
        {**{k: v for k, v in r.items() if k != "llm"}}
        for r in all_results
        if "error" not in r
    ]

    llm_path = RESULTS_DIR / "results_sonoix_llm.json"
    difflib_path = RESULTS_DIR / "results_sonoix_difflib.json"

    llm_path.write_text(
        json.dumps(llm_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    difflib_path.write_text(
        json.dumps(difflib_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\nResults saved to {llm_path}")
    print(f"Results saved to {difflib_path}")

    write_summary(all_results)


if __name__ == "__main__":
    main()
