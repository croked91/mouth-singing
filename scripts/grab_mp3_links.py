#!/usr/bin/env python3
"""
Граббер MP3 ссылок с rus.hitmotop.com (многопоточный).
Читает bootstrap CSV, для каждого трека ищет первую ссылку скачивания.

Использование:
  python3 grab_mp3_links.py input.csv output.csv [--workers 4]
"""

import argparse
import csv
import os
import re
import threading
import time

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

REQUEST_DELAY = 0.3   # сек между запросами на поток
RETRY_DELAY   = 5.0
MAX_RETRIES   = 3
TIMEOUT       = 10

MP3_PATTERN = re.compile(r'https://rus\.hitmotop\.com/get/music/[^\s"\']+\.mp3')

# ─── Core ─────────────────────────────────────────────────────────────────────

def get_first_mp3(search_url: str, session: requests.Session) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(search_url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()

            match = MP3_PATTERN.search(resp.text)
            if match:
                return match.group(0)

            soup = BeautifulSoup(resp.text, "html.parser")
            btn = soup.find(class_="track__download-btn")
            if btn and btn.get("href"):
                return btn["href"]

            return None

        except requests.RequestException:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            else:
                return None


def load_done(output_path: str) -> set[str]:
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

def main():
    parser = argparse.ArgumentParser(description="Grab MP3 links from hitmotop")
    parser.add_argument("input_csv")
    parser.add_argument("output_csv")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    rows = []
    with open(args.input_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)

    total = len(rows)

    done_urls = load_done(args.output_csv)
    todo = [r for r in rows if r.get("URL", "") not in done_urls]

    print(f"Total: {total}, already done: {len(done_urls)}, todo: {len(todo)}", flush=True)

    file_exists = os.path.exists(args.output_csv) and len(done_urls) > 0

    lock = threading.Lock()
    out_file = open(args.output_csv, "a" if file_exists else "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_file, fieldnames=["Артист", "Название", "URL", "SearchURL"])
    if not file_exists:
        writer.writeheader()

    found = 0
    not_found = 0
    processed = 0

    def process(row):
        nonlocal found, not_found, processed

        artist = row.get("Артист", "")
        title = row.get("Название", "")
        search_url = row.get("URL", "")

        session = requests.Session()
        mp3_url = get_first_mp3(search_url, session)
        session.close()

        with lock:
            processed += 1
            done_total = len(done_urls) + processed
            pct = done_total / total * 100

            if mp3_url:
                found += 1
                writer.writerow({
                    "Артист": artist,
                    "Название": title,
                    "URL": mp3_url,
                    "SearchURL": search_url,
                })
                out_file.flush()
                print(f"[{done_total}/{total} {pct:.1f}%] {artist} — {title}", flush=True)
            else:
                not_found += 1
                if processed % 50 == 0:
                    print(f"[{done_total}/{total} {pct:.1f}%] ... ({not_found} not found)", flush=True)

        time.sleep(REQUEST_DELAY)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(process, row) for row in todo]
            for f in as_completed(futures):
                f.result()  # raise exceptions if any
    except KeyboardInterrupt:
        print("\nInterrupted. Resume by running again.", flush=True)
    finally:
        out_file.close()

    print(f"\nDone! Found: {found}, not found: {not_found}, file: {args.output_csv}", flush=True)


if __name__ == "__main__":
    main()
