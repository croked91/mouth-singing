#!/usr/bin/env python3
"""
Скачиватель MP3 из final_mp3_library.csv
Формат файла: Исполнитель - Название.mp3

Использование:
  python3 download_mp3s.py final_mp3_library.csv /output/dir [--workers 4]
"""

import csv
import sys
import time
import os
import re
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ─── Config ───────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://rus.hitmotop.com/",
}

TIMEOUT       = 30   # сек на соединение
READ_TIMEOUT  = 120  # сек на скачивание файла
MAX_RETRIES   = 3
RETRY_DELAY   = 5.0

# ─── Helpers ──────────────────────────────────────────────────────────────────

UNSAFE_CHARS = re.compile(r'[\\/*?:"<>|]')

def safe_filename(artist: str, title: str) -> str:
    """Формирует безопасное имя файла: Исполнитель - Название.mp3"""
    artist = UNSAFE_CHARS.sub("_", artist).strip(". ")
    title  = UNSAFE_CHARS.sub("_", title).strip(". ")
    name   = f"{artist} - {title}.mp3"
    # Ограничим длину имени файла (255 байт для ext4)
    if len(name.encode("utf-8")) > 250:
        name = name[:80] + "….mp3"
    return name


def download_one(row: dict, out_dir: Path, session: requests.Session) -> tuple[str, bool, str]:
    """Скачивает один трек. Возвращает (filename, ok, message)."""
    artist   = row["Артист"]
    title    = row["Название"]
    url      = row["URL"]
    filename = safe_filename(artist, title)
    dest     = out_dir / filename

    if dest.exists() and dest.stat().st_size > 0:
        return filename, True, "skip"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(
                url,
                headers=HEADERS,
                timeout=(TIMEOUT, READ_TIMEOUT),
                stream=True,
            )
            resp.raise_for_status()

            tmp = dest.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            tmp.rename(dest)
            return filename, True, "ok"

        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            else:
                return filename, False, str(e)

    return filename, False, "unknown error"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Скачать MP3 из CSV-библиотеки")
    parser.add_argument("input_csv",  help="Входной CSV (Артист, Название, URL)")
    parser.add_argument("output_dir", help="Папка для MP3 файлов")
    parser.add_argument("--workers",  type=int, default=4,
                        help="Параллельных потоков скачивания (по умолчанию 4)")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    with open(args.input_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("URL"):
                rows.append(row)

    total = len(rows)
    print(f"📋 Треков в файле: {total}")
    print(f"📁 Папка: {out_dir}")
    print(f"⚡ Потоков: {args.workers}")
    print()

    # Resume: сколько уже есть
    existing = sum(1 for f in out_dir.iterdir() if f.suffix == ".mp3" and f.stat().st_size > 0)
    if existing:
        print(f"✅ Уже скачано: {existing} файлов (resume)\n")

    done = 0
    failed = 0
    skipped = 0

    session = requests.Session()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(download_one, row, out_dir, session): row
                for row in rows
            }
            for i, future in enumerate(as_completed(futures), 1):
                row = futures[future]
                filename, ok, msg = future.result()
                pct = i / total * 100

                if msg == "skip":
                    skipped += 1
                    status = "⏭️ "
                elif ok:
                    done += 1
                    status = "✅"
                else:
                    failed += 1
                    status = "❌"

                if msg not in ("skip", "ok"):
                    print(f"[{i}/{total} {pct:.1f}%] {status} {filename[:60]}  {msg}")
                elif msg == "ok":
                    print(f"[{i}/{total} {pct:.1f}%] {status} {filename[:70]}")

    except KeyboardInterrupt:
        print("\n⏸️  Прервано. Запусти снова — продолжит с того же места.")
    finally:
        session.close()

    print(f"\n📊 Итог:")
    print(f"   Скачано:   {done}")
    print(f"   Пропущено: {skipped} (уже были)")
    print(f"   Ошибок:    {failed}")
    print(f"   Папка:     {out_dir}")


if __name__ == "__main__":
    main()
