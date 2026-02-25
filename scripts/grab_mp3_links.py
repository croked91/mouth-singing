#!/usr/bin/env python3
"""
Граббер MP3 ссылок с rus.hitmotop.com
Читает bootstrap CSV, для каждого трека ищет первую ссылку скачивания.

Использование:
  python3 grab_mp3_links.py bootstrap_3000_final.csv output.csv
"""

import csv
import sys
import time
import re
import os
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}

REQUEST_DELAY = 0.5   # сек между запросами
RETRY_DELAY   = 5.0   # сек при ошибке
MAX_RETRIES   = 3
TIMEOUT       = 10    # сек на запрос

MP3_PATTERN   = re.compile(r'https://rus\.hitmotop\.com/get/music/[^\s"\']+\.mp3')

# ─── Core ─────────────────────────────────────────────────────────────────────

def get_first_mp3(search_url: str, session: requests.Session) -> str | None:
    """Загружает страницу поиска и возвращает href первой кнопки скачивания."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(search_url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()

            # Ищем первое вхождение MP3 ссылки в HTML
            match = MP3_PATTERN.search(resp.text)
            if match:
                return match.group(0)

            # Если regex не нашел — попробуем через BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            btn = soup.find(class_="track__download-btn")
            if btn and btn.get("href"):
                return btn["href"]

            return None  # Трек не найден на сайте

        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                print(f"    ⚠️  Ошибка (попытка {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    ❌ Пропускаю: {search_url} — {e}")
                return None


def load_done(output_path: str) -> set[str]:
    """Читает уже обработанные поисковые URL из выходного файла (resume)."""
    done = set()
    if not os.path.exists(output_path):
        return done
    with open(output_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("SearchURL"):
                done.add(row["SearchURL"])
    return done


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(input_csv: str, output_csv: str) -> None:
    # Читаем входной файл
    rows = []
    with open(input_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    total = len(rows)
    print(f"📋 Треков в файле: {total}")

    # Resume: пропускаем уже обработанные
    done_urls = load_done(output_csv)
    print(f"✅ Уже обработано: {len(done_urls)} (resume)")

    # Открываем выходной файл (append если resume)
    file_exists = os.path.exists(output_csv) and len(done_urls) > 0
    out_mode = "a" if file_exists else "w"

    out_file = open(output_csv, out_mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(out_file, fieldnames=["Артист", "Название", "URL", "SearchURL"])
    if not file_exists:
        writer.writeheader()

    session = requests.Session()

    found   = 0
    skipped = 0
    processed = 0

    try:
        for i, row in enumerate(rows, 1):
            artist     = row.get("Артист", "")
            title      = row.get("Название", "")
            search_url = row.get("URL", "")

            if search_url in done_urls:
                continue

            processed += 1
            pct = (i / total) * 100

            print(f"[{i}/{total} {pct:.1f}%] {artist} — {title}")

            mp3_url = get_first_mp3(search_url, session)

            if mp3_url:
                found += 1
                writer.writerow({
                    "Артист":    artist,
                    "Название":  title,
                    "URL":       mp3_url,
                    "SearchURL": search_url,
                })
                out_file.flush()
                print(f"    🎵 {mp3_url}")
            else:
                skipped += 1
                print(f"    ⚠️  Не найдено на hitmotop")

            time.sleep(REQUEST_DELAY)

    except KeyboardInterrupt:
        print("\n⏸️  Прервано. Запусти снова — продолжит с того же места.")

    finally:
        out_file.close()
        session.close()

    print(f"\n📊 Итог:")
    print(f"   Обработано: {processed}")
    print(f"   Найдено MP3: {found}")
    print(f"   Не найдено:  {skipped}")
    print(f"   Файл: {output_csv}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python3 grab_mp3_links.py input.csv output.csv")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
