# TorchCTCAligner adjustment fixtures

Each subdirectory holds a frozen production-pipeline state for one
reference track:

```
<track>/
    vocals.wav       # 16 kHz mono lead-vocals (PRE-Silero-trim — tests slice as needed)
    alignment.json   # ratio, trim_offset, silero_start_samples, words, first_flags, word_spans
    lyrics.txt       # input lyrics, committed source
    source.json      # mp3 path (relative to repo root) + lyrics file + language
```

The WAV is exactly the waveform that `TorchCTCAligner.align` passes
into the RMS-based adjustment methods after pre-processing. The JSON
captures the rest of that state so the test suite can reconstruct the
`_compute_line_start_adjustments` / `_compute_word_end_adjustments` /
`_compute_word_end_extensions` inputs without loading UVR, BackVocal,
Silero, or MMS models.

## Tracks

- **leps** — «Григорий Лепс — Она не твоя». Covers sustained line-start
  vowels («Это», «Она», «Знаешь»), true pre-word silence («Ссоры»),
  severe MMS last-phoneme drift («говорил», final «любовь?»), and
  line-end sustained vowel that MMS closes at attack («твоя»).
- **st1m** — «ST1M — Я — Рэп (Remix)». Covers rap-style line-start
  «Я» with soft backing/leakage pre-attack (walk path) and uniform
  leakage (span1.start fallback path).

## Regenerating

Fixtures must be regenerated whenever UVR / BackVocal / Silero / MMS
model versions change, or when the tokenizer / Silero trim logic
changes. Easiest path is the dedicated pytest flag — it walks every
fixture dir, reads its ``source.json``, copies the MP3 + lyrics into
the worker container, runs the full pipeline, and copies the new
artifacts back before tests start:

```bash
make up-gpu      # ensure the worker container is up
pytest tests/worker/test_torch_ctc_aligner_adjustments.py \
    --confcutdir=tests/worker --regen-fixtures -q
```

Adding a new fixture: create ``tests/worker/fixtures/alignment/<name>/``
with ``lyrics.txt`` and ``source.json``, then run with
``--regen-fixtures``. The MP3 referenced in ``source.json`` is resolved
relative to the repo root and is **not committed** (typically lives
under ``a-b-alignment/``).

Updating an expected value in the test file is a deliberate act — it
means a behavioural change has been re-validated against real audio.
