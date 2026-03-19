#!/usr/bin/env python3
"""Parse karaoke chart websites and categorize tracks by popularity.

Usage:
    python scripts/parse_karaoke_charts.py --db /data/sqlite/karaoke.db
    python scripts/parse_karaoke_charts.py --db /data/sqlite/karaoke.db --dry-run

Categories (in priority order):
    eternal_hit   — in karaoke "all time" lists AND in current charts
    current_hit   — in top-10 of any chart OR in 3+ charts
    former_hit    — was in charts before (chart_count > 0) but not currently
    artist_best   — most chart-mentioned track per artist
    regular       — everything else

Sources:
    Russian karaoke lists: vkaraoke.org, karaopa2.ru, hitlist.ru
    English karaoke lists: billboard.com, luckyvoice.com
    Current charts: hitmotop.com/top_charts
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ChartEntry:
    """A track found in an external chart or karaoke list."""

    artist: str
    title: str
    source: str  # which chart/list
    position: int = 0  # 0 = unranked (just "in list")


@dataclass
class CatalogTrack:
    """A track from our SQLite catalog."""

    id: str
    artist: str
    title: str
    chart_count: int = 0
    chart_last_seen: str | None = None
    popularity_category: str = "regular"


# ---------------------------------------------------------------------------
# Parsers for individual sources
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _fetch(url: str) -> str | None:
    """Fetch URL with error handling."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        print(f"  [WARN] Failed to fetch {url}: {exc}")
        return None


def parse_karaopa2_top() -> list[ChartEntry]:
    """Parse karaopa2.ru/karaoke/top/ — top 300 karaoke songs."""
    html = _fetch("https://karaopa2.ru/karaoke/top/")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for i, link in enumerate(soup.select("a.song-link, .song-title a, .track-name"), 1):
        text = link.get_text(strip=True)
        parts = text.split(" - ", 1)
        if len(parts) == 2:
            entries.append(ChartEntry(artist=parts[0].strip(), title=parts[1].strip(), source="karaopa2", position=i))
    # Fallback: try table rows or list items
    if not entries:
        for i, el in enumerate(soup.select("tr, li"), 1):
            text = el.get_text(strip=True)
            parts = re.split(r"\s*[-–—]\s*", text, maxsplit=1)
            if len(parts) == 2 and len(parts[0]) > 1 and len(parts[1]) > 1:
                entries.append(ChartEntry(artist=parts[0].strip(), title=parts[1].strip(), source="karaopa2", position=i))
            if len(entries) >= 300:
                break
    print(f"  karaopa2: {len(entries)} entries")
    return entries


def parse_hitlist_chart() -> list[ChartEntry]:
    """Parse hitlist.ru/s/karaoke_chart — karaoke chart."""
    html = _fetch("https://www.hitlist.ru/s/karaoke_chart")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for i, el in enumerate(soup.select(".chart-item, .track, tr"), 1):
        text = el.get_text(" ", strip=True)
        parts = re.split(r"\s*[-–—]\s*", text, maxsplit=1)
        if len(parts) == 2 and len(parts[0]) > 1 and len(parts[1]) > 1:
            entries.append(ChartEntry(artist=parts[0].strip(), title=parts[1].strip(), source="hitlist", position=i))
        if len(entries) >= 100:
            break
    print(f"  hitlist: {len(entries)} entries")
    return entries


def parse_hitmotop_charts() -> list[ChartEntry]:
    """Parse hitmotop.com/top_charts — multiple current charts."""
    html = _fetch("https://rus.hitmotop.com/top_charts")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    # Try to find chart sections and track listings
    for track_el in soup.select(".track__title, .track-item, .chart-track"):
        artist_el = track_el.select_one(".track__artist, .artist")
        title_el = track_el.select_one(".track__name, .title")
        if artist_el and title_el:
            entries.append(ChartEntry(
                artist=artist_el.get_text(strip=True),
                title=title_el.get_text(strip=True),
                source="hitmotop",
            ))
    # Fallback: generic link parsing
    if not entries:
        for link in soup.select("a"):
            text = link.get_text(strip=True)
            parts = re.split(r"\s*[-–—]\s*", text, maxsplit=1)
            if len(parts) == 2 and 2 < len(parts[0]) < 60 and 2 < len(parts[1]) < 80:
                entries.append(ChartEntry(artist=parts[0].strip(), title=parts[1].strip(), source="hitmotop"))
            if len(entries) >= 200:
                break
    print(f"  hitmotop: {len(entries)} entries")
    return entries


# ---------------------------------------------------------------------------
# Hardcoded eternal karaoke hits (fallback if parsing fails)
# ---------------------------------------------------------------------------

ETERNAL_HITS_RU = [
    ("Жуки", "Батарейка"),
    ("Кино", "Группа крови"),
    ("Кино", "Звезда по имени Солнце"),
    ("Кино", "Кукушка"),
    ("Кино", "Пачка сигарет"),
    ("Кино", "Перемен"),
    ("ДДТ", "Что такое осень"),
    ("Сплин", "Выхода нет"),
    ("Земфира", "Искала"),
    ("Ленинград", "Экспонат"),
    ("Григорий Лепс", "Рюмка водки на столе"),
    ("Баста", "Выпускной"),
    ("Руки Вверх", "Крошка моя"),
    ("Руки Вверх", "Он тебя целует"),
    ("Натали", "О боже какой мужчина"),
    ("Верка Сердючка", "Всё будет хорошо"),
    ("Ария", "Штиль"),
    ("Ария", "Беспечный ангел"),
    ("Король и Шут", "Лесник"),
    ("Король и Шут", "Кукла колдуна"),
    ("Наутилус Помпилиус", "Я хочу быть с тобой"),
    ("Алла Пугачёва", "Миллион алых роз"),
    ("Филипп Киркоров", "Зайка моя"),
    ("Олег Газманов", "Офицеры"),
    ("Любэ", "Конь"),
    ("Андрей Губин", "Зима-холода"),
    ("Отпетые мошенники", "Люби меня"),
    ("Иванушки International", "Тучи"),
    ("Дискотека Авария", "Новогодняя"),
    ("Звери", "Районы-кварталы"),
    ("Мумий Тролль", "Утекай"),
    ("Би-2", "Полковнику никто не пишет"),
    ("Чайф", "Аргентина-Ямайка"),
    ("Сектор Газа", "Лирика"),
    ("Макс Корж", "Горы по колено"),
    ("Oxxxymiron", "Где нас нет"),
    ("Нойз МС", "Из окна"),
]

ETERNAL_HITS_EN = [
    ("Queen", "Bohemian Rhapsody"),
    ("Queen", "We Will Rock You"),
    ("Queen", "We Are the Champions"),
    ("Scorpions", "Still Loving You"),
    ("Scorpions", "Wind of Change"),
    ("AC/DC", "Highway to Hell"),
    ("Metallica", "Nothing Else Matters"),
    ("Metallica", "Enter Sandman"),
    ("Nirvana", "Smells Like Teen Spirit"),
    ("Deep Purple", "Smoke on the Water"),
    ("Led Zeppelin", "Stairway to Heaven"),
    ("Eagles", "Hotel California"),
    ("Eminem", "Lose Yourself"),
    ("Eminem", "Without Me"),
    ("50 Cent", "In Da Club"),
    ("Rihanna", "Umbrella"),
    ("Whitney Houston", "I Will Always Love You"),
    ("Whitney Houston", "I Wanna Dance with Somebody"),
    ("Adele", "Someone Like You"),
    ("Adele", "Rolling in the Deep"),
    ("ABBA", "Dancing Queen"),
    ("ABBA", "Mamma Mia"),
    ("The Killers", "Mr. Brightside"),
    ("Backstreet Boys", "I Want It That Way"),
    ("Frank Sinatra", "My Way"),
    ("Journey", "Don't Stop Believin'"),
    ("Bon Jovi", "It's My Life"),
    ("Bon Jovi", "Livin' on a Prayer"),
    ("Elton John", "Your Song"),
    ("Michael Jackson", "Billie Jean"),
    ("The Beatles", "Let It Be"),
    ("The Beatles", "Yesterday"),
    ("Dua Lipa", "Levitating"),
    ("Beyoncé", "Single Ladies"),
    ("Shakira", "Waka Waka"),
]


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Normalize string for comparison: lowercase, strip punctuation."""
    s = s.lower().strip()
    s = re.sub(r"[''\"«»\(\)\[\]]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def fuzzy_match(
    artist1: str, title1: str, artist2: str, title2: str, threshold: float = 0.75
) -> bool:
    """Check if two tracks match using fuzzy string comparison."""
    a1, t1 = _normalize(artist1), _normalize(title1)
    a2, t2 = _normalize(artist2), _normalize(title2)

    # Exact match on normalized strings
    if a1 == a2 and t1 == t2:
        return True

    # Fuzzy match
    artist_sim = SequenceMatcher(None, a1, a2).ratio()
    title_sim = SequenceMatcher(None, t1, t2).ratio()

    return artist_sim >= threshold and title_sim >= threshold


# ---------------------------------------------------------------------------
# Categorization logic
# ---------------------------------------------------------------------------


def categorize_tracks(
    catalog: list[CatalogTrack],
    chart_entries: list[ChartEntry],
    eternal_hits: list[tuple[str, str]],
) -> dict[str, tuple[str, int]]:
    """Categorize each catalog track and return {track_id: (category, chart_count)}.

    Priority: eternal_hit > current_hit > artist_best > former_hit > regular
    """
    # Count chart appearances per catalog track
    track_chart_counts: dict[str, set[str]] = {}  # track_id -> set of sources
    track_in_top10: dict[str, bool] = {}

    for track in catalog:
        track_chart_counts[track.id] = set()
        track_in_top10[track.id] = False

        for entry in chart_entries:
            if fuzzy_match(track.artist, track.title, entry.artist, entry.title):
                track_chart_counts[track.id].add(entry.source)
                if entry.position > 0 and entry.position <= 10:
                    track_in_top10[track.id] = True

    # Check eternal hits
    track_is_eternal: dict[str, bool] = {}
    for track in catalog:
        track_is_eternal[track.id] = False
        for artist, title in eternal_hits:
            if fuzzy_match(track.artist, track.title, artist, title):
                track_is_eternal[track.id] = True
                break

    # Find best track per artist (by chart_count)
    artist_tracks: dict[str, list[tuple[str, int]]] = {}  # artist_norm -> [(track_id, count)]
    for track in catalog:
        artist_norm = _normalize(track.artist)
        count = len(track_chart_counts.get(track.id, set()))
        artist_tracks.setdefault(artist_norm, []).append((track.id, count))

    artist_best_ids: set[str] = set()
    for tracks in artist_tracks.values():
        if len(tracks) > 1:
            best = max(tracks, key=lambda x: x[1])
            if best[1] > 0:  # Only mark if at least 1 chart mention
                artist_best_ids.add(best[0])

    # Assign categories
    results: dict[str, tuple[str, int]] = {}
    for track in catalog:
        chart_count = len(track_chart_counts.get(track.id, set()))
        is_eternal = track_is_eternal.get(track.id, False)
        in_top10 = track_in_top10.get(track.id, False)

        if is_eternal:
            category = "eternal_hit"
        elif in_top10 or chart_count >= 3:
            category = "current_hit"
        elif track.id in artist_best_ids:
            category = "artist_best"
        elif chart_count > 0 or (track.chart_count or 0) > 0:
            category = "former_hit"
        else:
            category = "regular"

        results[track.id] = (category, chart_count)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Parse karaoke charts and categorize tracks")
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Print results without updating DB")
    args = parser.parse_args()

    # Load catalog
    print("Loading catalog...")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT id, artist, title, chart_count, chart_last_seen, popularity_category "
        "FROM tracks WHERE status = 'ready'"
    )
    catalog = [
        CatalogTrack(
            id=row["id"],
            artist=row["artist"],
            title=row["title"],
            chart_count=row["chart_count"] if row["chart_count"] else 0,
            chart_last_seen=row["chart_last_seen"],
            popularity_category=row["popularity_category"] if row["popularity_category"] else "regular",
        )
        for row in cursor.fetchall()
    ]
    print(f"  {len(catalog)} ready tracks in catalog")

    # Parse charts
    print("\nParsing charts...")
    all_entries: list[ChartEntry] = []
    all_entries.extend(parse_karaopa2_top())
    all_entries.extend(parse_hitlist_chart())
    all_entries.extend(parse_hitmotop_charts())
    print(f"\n  Total chart entries: {len(all_entries)}")

    # Combine hardcoded eternal hits
    eternal_hits = ETERNAL_HITS_RU + ETERNAL_HITS_EN

    # Categorize
    print("\nCategorizing...")
    results = categorize_tracks(catalog, all_entries, eternal_hits)

    # Stats
    stats: dict[str, int] = {}
    for category, _ in results.values():
        stats[category] = stats.get(category, 0) + 1
    print(f"\n  Results:")
    for cat in ["eternal_hit", "current_hit", "artist_best", "former_hit", "regular"]:
        print(f"    {cat}: {stats.get(cat, 0)}")

    if args.dry_run:
        print("\n  [DRY RUN] No changes written to database.")
        # Show some examples
        for cat in ["eternal_hit", "current_hit", "artist_best"]:
            examples = [
                t for t in catalog
                if results.get(t.id, ("regular", 0))[0] == cat
            ][:5]
            if examples:
                print(f"\n  Examples of {cat}:")
                for t in examples:
                    print(f"    {t.artist} — {t.title}")
        conn.close()
        return

    # Update database
    print("\nUpdating database...")
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    for track_id, (category, chart_count) in results.items():
        chart_last_seen = now if chart_count > 0 else None
        conn.execute(
            """
            UPDATE tracks
            SET popularity_category = ?, chart_count = ?, chart_last_seen = ?, updated_at = ?
            WHERE id = ?
            """,
            (category, chart_count, chart_last_seen, now, track_id),
        )
        updated += 1
    conn.commit()
    conn.close()
    print(f"  Updated {updated} tracks.")


if __name__ == "__main__":
    main()
