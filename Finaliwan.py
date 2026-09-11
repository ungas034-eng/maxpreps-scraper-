#!/usr/bin/env python3
"""
MaxPreps scraper with interactive UI
Python 3.8+ — stdlib only

Jalankan:
  python3 maxpreps_scraper.py
Lalu buka http://127.0.0.1:8080

Alur UI:
  1. Pilih sport
  2. Pilih state
  3. Cek tanggal → daftar hari yang ada game
  4. Isi link Watch Live
  5. Scrape → file per state (schedules/tx-football.txt) atau timpa
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

WATCH_LIVE_DEFAULT = "https://example.com/live"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "schedules")
OUTPUT_FILE = os.path.join(BASE_DIR, "schedules.txt")
MASCOT_CACHE = os.path.join(BASE_DIR, "mascot_cache.json")
WATCH_FILE = os.path.join(BASE_DIR, "watch_links.txt")

STATES = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "dc": "Washington DC", "fl": "Florida", "ga": "Georgia", "hi": "Hawaii",
    "id": "Idaho", "il": "Illinois", "in": "Indiana", "ia": "Iowa",
    "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana", "me": "Maine",
    "md": "Maryland", "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota",
    "ms": "Mississippi", "mo": "Missouri", "mt": "Montana", "ne": "Nebraska",
    "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey", "nm": "New Mexico",
    "ny": "New York", "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio",
    "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island",
    "sc": "South Carolina", "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas",
    "ut": "Utah", "vt": "Vermont", "va": "Virginia", "wa": "Washington",
    "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming", "ps": "Prep Schools",
}

SPORTS = {
    "football": ("Football (Boys)", "football"),
    "basketball": ("Basketball (Boys)", "basketball"),
    "basketball-girls": ("Basketball (Girls)", "basketball/girls"),
    "baseball": ("Baseball", "baseball"),
    "softball": ("Softball", "softball"),
    "volleyball": ("Volleyball (Girls)", "volleyball"),
    "volleyball-boys": ("Volleyball (Boys)", "volleyball/boys"),
    "soccer": ("Soccer (Boys)", "soccer"),
    "soccer-girls": ("Soccer (Girls)", "soccer/girls"),
}

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.137 Safari/537.36",
]


def load_watch_map(path: Optional[str] = None) -> Dict[str, str]:
    path = path or WATCH_FILE
    mapping: Dict[str, str] = {}
    if not os.path.isfile(path):
        return mapping
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            mapping[key.strip().lower()] = val.strip()
    return mapping


def save_watch_link(url: str, state: str = "", sport: str = "") -> None:
    """Persist the watch-live URL so next run remembers it."""
    mapping = load_watch_map()
    if url:
        mapping["default"] = url
        if state:
            mapping[state.lower()] = url
        if state and sport:
            mapping[f"{state.lower()}/{sport}"] = url
    lines = ["# watch live links", "# key=url", ""]
    for k in sorted(mapping):
        if mapping[k]:
            lines.append(f"{k}={mapping[k]}")
    with open(WATCH_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def resolve_watch_url(watch_cli: str, watch_map: Dict[str, str], state: str, sport: str) -> str:
    for k in (f"{state}/{sport}", f"{state}-{sport}", state, sport, "default"):
        if watch_map.get(k):
            return watch_map[k]
    return watch_cli or WATCH_LIVE_DEFAULT


def fetch(url: str, referer: str = "https://www.maxpreps.com/", insecure: bool = False) -> Tuple[int, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40, context=ctx) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body
    except Exception as exc:
        return 0, str(exc)


def to_mdy(value: str) -> str:
    value = (value or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value)
    if m:
        return f"{int(m.group(2))}/{int(m.group(3))}/{m.group(1)}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
    if m:
        return f"{int(m.group(1))}/{int(m.group(2))}/{m.group(3)}"
    now = datetime.now()
    return f"{now.month}/{now.day}/{now.year}"


def mdy_to_iso(mdy: str) -> str:
    parts = mdy.split("/")
    if len(parts) != 3:
        return datetime.now().strftime("%Y-%m-%d")
    return f"{int(parts[2]):04d}-{int(parts[0]):02d}-{int(parts[1]):02d}"


def format_tanggal(mdy: str) -> str:
    parts = mdy.split("/")
    try:
        dt = datetime(int(parts[2]), int(parts[0]), int(parts[1]))
    except Exception:
        return mdy
    return dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")


def scores_url(state: str, sport_key: str, mdy: Optional[str]) -> str:
    slug = SPORTS.get(sport_key, SPORTS["football"])[1]
    url = f"https://www.maxpreps.com/{state}/{slug}/scores/"
    if mdy:
        url += f"?date={mdy}"
    return url


def hashtag(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "", text or "")
    return "#" + text if text else ""


def strip_rank(name: str) -> str:
    return re.sub(r"^\(#\d+\)\s*", "", name).strip()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class ScoreParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: List[dict] = []
        self._in_card = False
        self._card_depth = 0
        self._cur: Optional[dict] = None
        self._buf: List[str] = []
        self._capture = None
        self._pending_team = {"name": "", "score": ""}

    def handle_starttag(self, tag, attrs):
        amap = dict(attrs)
        cls = amap.get("class", "")
        if tag == "div" and "contest-box-item" in cls:
            self._in_card = True
            self._card_depth = 1
            self._cur = {
                "state_attr": amap.get("data-contest-state", ""),
                "href": "",
                "teams": [],
                "details": "",
            }
            self._pending_team = {"name": "", "score": ""}
            return
        if not self._in_card or self._cur is None:
            return
        if tag == "div":
            self._card_depth += 1
        if tag == "a" and "c-c" in cls:
            self._cur["href"] = amap.get("href", "")
        if tag == "div" and "name" in cls.split():
            self._capture, self._buf = "name", []
        elif tag == "div" and "score" in cls.split():
            self._capture, self._buf = "score", []
        elif tag == "div" and "details" in cls.split():
            self._capture, self._buf = "details", []

    def handle_endtag(self, tag):
        if not self._in_card or self._cur is None:
            return
        if self._capture and tag == "div":
            text = clean("".join(self._buf))
            if self._capture == "name":
                self._pending_team["name"] = strip_rank(text)
                if self._pending_team["name"]:
                    self._cur["teams"].append(dict(self._pending_team))
                    self._pending_team = {"name": "", "score": ""}
            elif self._capture == "score":
                self._pending_team["score"] = text
            elif self._capture == "details":
                self._cur["details"] = text
            self._capture, self._buf = None, []
        if tag == "div":
            self._card_depth -= 1
            if self._card_depth <= 0:
                if self._cur.get("state_attr") != "placeholder":
                    self.cards.append(self._cur)
                self._in_card = False
                self._cur = None

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)


def parse_cards(html: str) -> List[dict]:
    p = ScoreParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    if p.cards:
        return p.cards
    for m in re.finditer(r'class="contest-box-item"[^>]*>(.{0,2500}?)</div>\s*</li>', html, re.S):
        block = m.group(0)
        names = [strip_rank(clean(re.sub(r"<[^>]+>", "", x))) for x in re.findall(r'class="name"[^>]*>(.*?)</div>', block, re.S)]
        names = [n for n in names if n]
        scores = [clean(re.sub(r"<[^>]+>", "", x)) for x in re.findall(r'class="score"[^>]*>(.*?)</div>', block, re.S)]
        hm = re.search(r'href="([^"]+)"', block)
        href = hm.group(1) if hm else ""
        dm = re.search(r'class="details"[^>]*>(.*?)</div>', block, re.S)
        det = clean(re.sub(r"<[^>]+>", " ", dm.group(1))) if dm else ""
        if len(names) >= 2:
            teams = [{"name": names[i], "score": scores[i] if i < len(scores) else ""} for i in range(2)]
            p.cards.append({"href": href, "teams": teams, "details": det, "state_attr": ""})
    return p.cards


def parse_calendar(html: str) -> List[Tuple[str, int]]:
    """Parse kalender; hanya kembalikan hari ini & tanggal mendatang."""
    best: Dict[str, int] = {}
    for href, count in re.findall(
        r'href="[^"]*[?&]date=(\d{1,2}/\d{1,2}/\d{4})"[^>]*data-contest-count="(\d+)"',
        html,
    ):
        best[href] = max(int(count), best.get(href, 0))
    for href in re.findall(r'href="[^"]*[?&]date=(\d{1,2}/\d{1,2}/\d{4})"[^>]*class="[^"]*active', html):
        best.setdefault(href, 0)

    today = datetime.now().date()

    def key(item):
        d = item[0].split("/")
        return (int(d[2]), int(d[0]), int(d[1]))

    def is_today_or_future(mdy: str) -> bool:
        try:
            parts = mdy.split("/")
            dt = datetime(int(parts[2]), int(parts[0]), int(parts[1])).date()
            return dt >= today
        except Exception:
            return False

    filtered = [(d, c) for d, c in best.items() if is_today_or_future(d)]
    return sorted(filtered, key=key)


def load_cache() -> dict:
    if os.path.isfile(MASCOT_CACHE):
        try:
            with open(MASCOT_CACHE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    with open(MASCOT_CACHE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)


_CACHE_LOCK = __import__("threading").Lock()


def load_cache_safe() -> dict:
    with _CACHE_LOCK:
        return load_cache()


def merge_save_cache(updates: dict) -> None:
    """Merge partial cache updates thread-safely."""
    if not updates:
        return
    with _CACHE_LOCK:
        cache = load_cache()
        cache.update(updates)
        save_cache(cache)


def scrape_game_info(game_url: str, insecure: bool) -> Tuple[str, str, str]:
    """Ambil mascot + teks Game Info dari halaman game MaxPreps.

    Returns: (mascot_a, mascot_b, game_info_description)

    Contoh:
      The Animo Robinson varsity football team has an away non-conference
      game @ Chadwick (Palos Verdes Peninsula, CA) on Friday, September 11 @ 3:30p.
    """
    if not game_url:
        return "", "", ""
    code, page = fetch(game_url, insecure=insecure)
    if code != 200:
        return "", "", ""

    names = [clean(x) for x in re.findall(r'class="mascot-name"\s*>\s*([^<]+)', page)]
    if len(names) < 2:
        for extra in re.findall(r'"mascot"\s*:\s*"([^"]+)"', page):
            extra = clean(extra)
            if extra and extra not in names:
                names.append(extra)
    mascot_a = names[0] if names else ""
    mascot_b = names[1] if len(names) > 1 else ""

    description = ""
    m = re.search(
        r'class="game-info"[^>]*>.{0,1200}?class="description"[^>]*>(.*?)</div>',
        page,
        re.S | re.I,
    )
    if not m:
        m = re.search(r'class="description"[^>]*>(.*?)</div>', page, re.S | re.I)
    if m:
        description = clean(re.sub(r"<[^>]+>", " ", m.group(1)))
        description = re.sub(r"\s*@\s*", " @ ", description)
        description = re.sub(r"\s{2,}", " ", description).strip()

    return mascot_a, mascot_b, description


def scrape_mascots(game_url: str, insecure: bool) -> Tuple[str, str]:
    a, b, _ = scrape_game_info(game_url, insecure)
    return a, b


def pretty_clock(text: str) -> str:
    """7:00 PM / 6:00p → 6p ; keep Live / Final / quarter."""
    t = clean(text)
    t = re.sub(r"\s+", " ", t)
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", t, re.I)
    if m:
        hour = int(m.group(1))
        mins = m.group(2) or "00"
        ap = m.group(3).lower()
        clock = f"{hour}{ap}" if mins == "00" else f"{hour}:{mins}{ap}"
        t = re.sub(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b", clock, t, flags=re.I)
    t = t.replace("Missing score", "score not reported")
    return t


def build_detail(team_a: str, team_b: str, raw_details: str, score_a: str, score_b: str) -> str:
    """Fallback kalau Game Info halaman game tidak tersedia."""
    status = pretty_clock(raw_details or "")
    low = status.lower()
    if status and not re.search(r"\b(live|final|quarter|halftime|progress|scheduled|tbd)\b", low):
        if re.search(r"\d+[ap]\b", low) or re.search(r"\d+:\d+", low):
            status = "starts at " + status
            if "started" in low or "start" in low:
                status = pretty_clock(raw_details)
    line = f"{team_a} @ {team_b}"
    bits = [line]
    if status:
        bits.append(status)
    if score_a or score_b:
        bits.append(f"{score_a or '-'}–{score_b or '-'}")
    return ", ".join(bits[:2]) + ((" | " + bits[2]) if len(bits) > 2 else "")


def watch_with_title(watch: str, team_a: str, team_b: str) -> str:
    """Tambah ?title=TeamA%20Vs.%20TeamB (atau &title= kalau URL sudah ada query)."""
    base = (watch or "").strip()
    if not base:
        return base
    parts = urllib.parse.urlsplit(base)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k.lower() != "title"]
    q.append(("title", f"{team_a} Vs. {team_b}"))
    new_query = urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def sport_hashtag_label(sport: str) -> str:
    """football → Football ; basketball-girls → BasketballGirls.
    Boys dihilangkan (default). Girls tetap ditempel agar beda.
    """
    label = SPORTS.get(sport, SPORTS["football"])[0]
    core = re.sub(r"\s*\([^)]*\)\s*", "", label).strip()
    gender = ""
    m = re.search(r"\((Girls)\)", label, re.I)
    if m:
        gender = "Girls"
    return hashtag(core + gender).lstrip("#") or "Football"


def format_match(
    title,
    team_a,
    mascot_a,
    team_b,
    mascot_b,
    tanggal,
    watch,
    detail,
    state_name,
    state_code: str = "",
    sport: str = "football",
    add_title: bool = True,
) -> str:
    """Format output:

    ===========================
    Title

    TeamA vs TeamB
    MascotA @ mascotB

    📺watch live: link
    🗒️Game Info

    #State #XXHSSport #MascotA #MascotB
    ============================
    """
    live = watch_with_title(watch, team_a, team_b) if add_title else (watch or "")
    state_tag = hashtag(state_name.replace(" ", ""))
    code = (state_code or "").upper() or "XX"
    sport_part = sport_hashtag_label(sport)
    combo_tag = f"#{code}HS{sport_part}"
    mascot_tags = " ".join(t for t in [hashtag(mascot_a), hashtag(mascot_b)] if t)
    tags = " ".join(t for t in [state_tag, combo_tag, mascot_tags] if t)
    # Hanya tampilkan baris mascot jika keduanya ada; jika kosong → baris kosong
    if mascot_a and mascot_b:
        mascot_line = f"{mascot_a} @ {mascot_b}"
    elif mascot_a or mascot_b:
        mascot_line = mascot_a or mascot_b
    else:
        mascot_line = ""
    _ = tanggal  # kompatibilitas pemanggil
    return (
        "===========================\n"
        f"{title}\n"
        f"{team_a} vs {team_b}\n"
        f"{mascot_line}\n"
        f"📺watch live: {live}\n"
        f"🗒️{detail}\n"
        f"\n"
        f"{tags}\n"
        "============================\n"
        "\n"
    )



def state_file(state: str, sport: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sport_safe = re.sub(r"[^a-z0-9]+", "-", (sport or "football").lower()).strip("-")
    return os.path.join(OUTPUT_DIR, f"{state.lower()}-{sport_safe}.txt")


def all_states_file(sport: str, mdy: str) -> str:
    """Satu file untuk All States: schedules/{sport}-{YYYY-MM-DD}.txt"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sport_safe = re.sub(r"[^a-z0-9]+", "-", (sport or "football").lower()).strip("-")
    iso = mdy_to_iso(mdy)
    return os.path.join(OUTPUT_DIR, f"{sport_safe}-{iso}.txt")


def block_key(block: str) -> str:
    lines = block.strip().splitlines()
    return "\n".join(lines[:3]) if len(lines) >= 3 else block


def write_blocks(path: str, blocks: List[str], overwrite: bool) -> Tuple[int, int]:
    """overwrite=True ganti isi file. False = append, skip duplikat."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    written = skipped = 0
    if overwrite:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(blocks))
        return len(blocks), 0
    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    with open(path, "a", encoding="utf-8") as fh:
        for block in blocks:
            key = block_key(block)
            if key and key in existing:
                skipped += 1
                continue
            fh.write(block)
            existing += block
            written += 1
    return written, skipped


def append_unique(path: str, block: str) -> bool:
    w, _ = write_blocks(path, [block], overwrite=False)
    return w == 1


def list_dates(state: str, sport: str, insecure: bool = False) -> Tuple[int, str, List[Tuple[str, int]]]:
    # Jangan pernah fetch /all/ — MaxPreps tidak punya path itu
    probe = state if state in STATES else "tx"
    url = scores_url(probe, sport, None)
    code, page = fetch(url, referer="https://www.maxpreps.com/", insecure=insecure)
    if code != 200:
        return code, url, []
    return code, url, parse_calendar(page)


def list_dates_with_fallback(sport: str, insecure: bool = False) -> Tuple[int, str, List[Tuple[str, int]], str]:
    """Coba beberapa state acuan sampai kalender kebaca (untuk mode All States)."""
    for probe in ("tx", "ca", "fl", "oh", "pa", "ga"):
        code, url, dates = list_dates(probe, sport, insecure)
        if code == 200 and dates:
            return code, url, dates, probe
        if code == 200 and not dates:
            # halaman ok tapi kosong — coba state lain
            continue
    # last attempt result
    code, url, dates = list_dates("tx", sport, insecure)
    return code, url, dates, "tx"


def scrape_state(
    state: str,
    sport: str,
    mdy: str,
    watch: str,
    with_mascots: bool,
    mascot_limit: int,
    insecure: bool,
    add_title: bool = True,
    with_game_info: bool = True,
    game_info_limit: int = 80,
    game_workers: int = 4,
) -> Tuple[int, str, List[str]]:
    """Scrape scores + (opsional) Game Info paralel per state."""
    state = state.lower()
    sport_label = SPORTS.get(sport, SPORTS["football"])[0]
    state_name = STATES.get(state, state.upper())
    title = f"{state_name} High School {sport_label}"
    tanggal = format_tanggal(mdy)
    url = scores_url(state, sport, mdy)
    code, page = fetch(url, referer=f"https://www.maxpreps.com/{state}/", insecure=insecure)
    if code != 200:
        return code, url, []
    cards = parse_cards(page)
    cache = load_cache_safe()
    fetch_limit = mascot_limit if with_mascots else game_info_limit
    need_fetch = (with_game_info or with_mascots) and fetch_limit > 0

    prepared = []
    pending_urls = []
    for card in cards:
        teams = card.get("teams") or []
        if len(teams) < 2:
            continue
        team_a, team_b = teams[0]["name"], teams[1]["name"]
        score_a, score_b = teams[0].get("score", ""), teams[1].get("score", "")
        href = card.get("href") or ""
        if href.startswith("/"):
            href = urljoin("https://www.maxpreps.com/", href)
        mascot_a = mascot_b = ""
        game_info = ""
        ck = f"{state}|{team_a}|{team_b}|{mdy}".lower()
        ck_legacy = f"{state}|{team_a}|{team_b}".lower()
        cached = cache.get(ck) or cache.get(ck_legacy)
        if cached and isinstance(cached, list) and len(cached) >= 2:
            mascot_a, mascot_b = cached[0] or "", cached[1] or ""
            if len(cached) >= 3:
                game_info = cached[2] or ""
        idx = len(prepared)
        prepared.append({
            "team_a": team_a, "team_b": team_b,
            "score_a": score_a, "score_b": score_b,
            "href": href, "ck": ck,
            "mascot_a": mascot_a, "mascot_b": mascot_b,
            "game_info": game_info,
            "raw_details": card.get("details") or "",
        })
        if need_fetch and href:
            want_info = with_game_info and not game_info
            want_mascot = with_mascots and not (mascot_a and mascot_b)
            if want_info or want_mascot:
                pending_urls.append(idx)

    pending_urls = pending_urls[:fetch_limit]
    cache_updates = {}

    def _fetch_one(idx: int):
        href = prepared[idx]["href"]
        time.sleep(random.uniform(0.05, 0.18))
        return idx, scrape_game_info(href, insecure)

    workers = max(1, min(game_workers, 8))
    if pending_urls:
        if workers == 1 or len(pending_urls) == 1:
            results = [_fetch_one(idx) for idx in pending_urls]
        else:
            results = []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_fetch_one, idx) for idx in pending_urls]
                for fut in as_completed(futs):
                    try:
                        results.append(fut.result())
                    except Exception:
                        pass
        for i, (ma, mb, desc) in results:
            if ma or mb:
                prepared[i]["mascot_a"] = prepared[i]["mascot_a"] or ma
                prepared[i]["mascot_b"] = prepared[i]["mascot_b"] or mb
            if desc:
                prepared[i]["game_info"] = desc
            cache_updates[prepared[i]["ck"]] = [
                prepared[i]["mascot_a"], prepared[i]["mascot_b"], prepared[i]["game_info"]
            ]

    if cache_updates:
        merge_save_cache(cache_updates)

    blocks: List[str] = []
    for row in prepared:
        if row["game_info"]:
            details = row["game_info"]
        else:
            details = build_detail(
                row["team_a"], row["team_b"], row["raw_details"],
                row["score_a"], row["score_b"],
            )
        blocks.append(
            format_match(
                title, row["team_a"], row["mascot_a"], row["team_b"], row["mascot_b"],
                tanggal, watch, details, state_name,
                state_code=state, sport=sport, add_title=add_title,
            )
        )
    return code, url, blocks


def scrape_states_parallel(
    states: List[str],
    sport: str,
    mdy: str,
    watch: str,
    with_mascots: bool,
    mascot_limit: int,
    insecure: bool,
    add_title: bool,
    with_game_info: bool,
    game_info_limit: int,
    state_workers: int = 6,
    game_workers: int = 3,
) -> Tuple[List[str], List[str], int, str, List[str]]:
    """Scrape banyak state secara paralel. Returns all_blocks, failed, last_code, last_url, last_blocks."""
    all_blocks: List[str] = []
    failed: List[str] = []
    last_code, last_url = 0, ""
    last_blocks: List[str] = []
    lock = __import__("threading").Lock()

    def job(st: str):
        wurl = resolve_watch_url(watch, load_watch_map(), st, sport)
        return st, scrape_state(
            st, sport, mdy, wurl or WATCH_LIVE_DEFAULT,
            with_mascots, mascot_limit, insecure, add_title=add_title,
            with_game_info=with_game_info, game_info_limit=game_info_limit,
            game_workers=game_workers,
        )

    workers = max(1, min(state_workers, 10))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(job, st): st for st in states}
        for fut in as_completed(futs):
            st = futs[fut]
            try:
                st2, (code, url, blocks) = fut.result()
            except Exception as exn:
                with lock:
                    failed.append(f"{st.upper()} ERR {type(exn).__name__}")
                continue
            with lock:
                last_code, last_url = code, url
                if code != 200:
                    failed.append(f"{st2.upper()} HTTP {code}")
                else:
                    all_blocks.extend(blocks)
                    if blocks:
                        last_blocks = blocks
    return all_blocks, failed, last_code, last_url, last_blocks


def list_schedule_files() -> List[str]:
    """Daftar file .txt di schedules/ + schedules.txt di root."""
    files: List[str] = []
    if os.path.isfile(OUTPUT_FILE):
        files.append(OUTPUT_FILE)
    if os.path.isdir(OUTPUT_DIR):
        for name in sorted(os.listdir(OUTPUT_DIR)):
            if name.endswith(".txt"):
                files.append(os.path.join(OUTPUT_DIR, name))
    return files


def safe_schedule_path(req: str) -> Optional[str]:
    """Hanya izinkan file di dalam BASE_DIR (schedules atau schedules.txt)."""
    if not req:
        return None
    abs_path = os.path.abspath(req)
    base = os.path.abspath(BASE_DIR)
    if not abs_path.startswith(base) or not os.path.isfile(abs_path):
        return None
    if not abs_path.endswith(".txt"):
        return None
    return abs_path


def page_html(ctx: dict) -> str:
    sport = ctx.get("sport") or "football"
    state = ctx.get("state") or "tx"
    watch = ctx.get("watch") or load_watch_map().get("default") or WATCH_LIVE_DEFAULT
    dates = ctx.get("dates") or []
    selected_date = ctx.get("date") or ""
    flash = ctx.get("flash") or ""
    error = ctx.get("error") or ""
    preview = ctx.get("preview") or ""
    mascots = ctx.get("mascots")
    overwrite = ctx.get("overwrite")
    split_state = ctx.get("split_state", True)
    add_title = ctx.get("add_title", True)
    source = ctx.get("source") or ""
    outfile = ctx.get("outfile") or ""
    editor_file = ctx.get("editor_file") or ""
    editor_content = ctx.get("editor_content")
    editor_msg = ctx.get("editor_msg") or ""
    editor_err = ctx.get("editor_err") or ""
    find_val = ctx.get("find_val") or ""
    replace_val = ctx.get("replace_val") or ""
    tab = ctx.get("tab") or "scrape"

    sport_opts = "".join(
        f'<option value="{k}" {"selected" if k == sport else ""}>{htmllib.escape(v[0])}</option>'
        for k, v in SPORTS.items()
    )
    state_opts = (
        f'<option value="all" {"selected" if state == "all" else ""}>ALL STATES ({len(STATES)})</option>'
        + "".join(
            f'<option value="{k}" {"selected" if k == state else ""}>{htmllib.escape(n)} ({k.upper()})</option>'
            for k, n in STATES.items()
        )
    )

    date_block = ""
    if dates:
        radios = []
        for i, (d, c) in enumerate(dates):
            iso = mdy_to_iso(d)
            label = format_tanggal(d)
            chk = "checked" if (selected_date in (d, iso) or (not selected_date and i == 0)) else ""
            radios.append(
                f'<label class="date-item"><input type="radio" name="date" value="{htmllib.escape(d)}" {chk}>'
                f'<span class="date-main"><b>{htmllib.escape(d)}</b><small>{htmllib.escape(label)}</small></span>'
                f'<em class="badge">{c} game{"s" if c != 1 else ""}</em></label>'
            )
        date_block = (
            '<div class="dates"><p class="lbl">Tanggal tersedia (hari ini &amp; mendatang)</p>'
            + "".join(radios)
            + "</div>"
        )
    elif ctx.get("checked"):
        date_block = '<p class="warn">Tidak ketemu tanggal hari ini/mendatang. Coba state/sport lain, atau cek VPN.</p>'

    flash_html = f'<div class="ok">{htmllib.escape(flash)}</div>' if flash else ""
    err_html = f'<div class="err">{htmllib.escape(error)}</div>' if error else ""
    src_html = f'<p class="mut">Sumber: {htmllib.escape(source)}</p>' if source else ""
    prev_html = f'<div class="card"><p class="lbl">Preview</p><pre class="out">{htmllib.escape(preview)}</pre></div>' if preview else ""
    mascot_chk = "checked" if mascots else ""
    over_chk = "checked" if overwrite else ""
    split_chk = "checked" if split_state else ""
    title_chk = "checked" if add_title else ""
    out_html = (
        f'<p class="mut">Tersimpan: <a class="link" href="/file?path={htmllib.escape(urllib.parse.quote(outfile))}">{htmllib.escape(os.path.basename(outfile))}</a></p>'
        if outfile else ""
    )

    # editor file list
    sched_files = list_schedule_files()
    file_opts = '<option value="">— pilih file —</option>' + "".join(
        f'<option value="{htmllib.escape(f)}" {"selected" if f == editor_file else ""}>{htmllib.escape(os.path.basename(f))}</option>'
        for f in sched_files
    )
    ed_msg = f'<div class="ok">{htmllib.escape(editor_msg)}</div>' if editor_msg else ""
    ed_err = f'<div class="err">{htmllib.escape(editor_err)}</div>' if editor_err else ""
    ed_content = "" if editor_content is None else editor_content
    tab_scrape = "active" if tab != "editor" else ""
    tab_editor = "active" if tab == "editor" else ""
    panel_scrape = "" if tab != "editor" else "hidden"
    panel_editor = "" if tab == "editor" else "hidden"

    return f"""<!DOCTYPE html>
<html lang="id" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MaxPreps Scraper · Ridwan</title>
<style>
:root, [data-theme="dark"] {{
  --bg: #0b0f14;
  --bg2: #121820;
  --card: #151c26;
  --ink: #e8eef6;
  --mut: #8b9bb0;
  --acc: #3d8bfd;
  --acc2: #22c55e;
  --bad: #f87171;
  --line: #243041;
  --input: #0e141c;
  --shadow: 0 8px 30px rgba(0,0,0,.35);
  --ok-bg: #0f2a1c; --ok-fg: #86efac;
  --err-bg: #2a1216; --err-fg: #fecaca;
  --warn: #fbbf24;
  --radius: 14px;
}}
[data-theme="light"] {{
  --bg: #f4f6fa;
  --bg2: #ffffff;
  --card: #ffffff;
  --ink: #0f172a;
  --mut: #64748b;
  --acc: #2563eb;
  --acc2: #16a34a;
  --bad: #dc2626;
  --line: #e2e8f0;
  --input: #f8fafc;
  --shadow: 0 8px 28px rgba(15,23,42,.08);
  --ok-bg: #ecfdf5; --ok-fg: #166534;
  --err-bg: #fef2f2; --err-fg: #991b1b;
  --warn: #b45309;
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0;
  font: 15px/1.5 "Inter", system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  background: var(--bg);
  color: var(--ink);
  min-height: 100vh;
}}
a.link {{ color: var(--acc); text-decoration: none; font-weight: 600; }}
a.link:hover {{ text-decoration: underline; }}
.wrap {{ max-width: 880px; margin: 0 auto; padding: 20px 16px 80px; }}
.topbar {{
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-bottom: 18px; flex-wrap: wrap;
}}
.brand {{ display: flex; align-items: center; gap: 12px; }}
.logo {{
  width: 42px; height: 42px; border-radius: 12px;
  background: linear-gradient(135deg, var(--acc), #7c3aed);
  display: grid; place-items: center; font-weight: 800; color: #fff; font-size: 14px;
  box-shadow: var(--shadow);
}}
h1 {{ font-size: 1.35rem; margin: 0; letter-spacing: -.02em; }}
.sub {{ color: var(--mut); margin: 2px 0 0; font-size: 13px; }}
.theme-btn {{
  border: 1px solid var(--line); background: var(--card); color: var(--ink);
  border-radius: 999px; padding: 8px 14px; font-weight: 600; cursor: pointer;
  display: inline-flex; align-items: center; gap: 8px; transition: .15s ease;
}}
.theme-btn:hover {{ border-color: var(--acc); }}
.tabs {{
  display: flex; gap: 6px; background: var(--bg2); border: 1px solid var(--line);
  border-radius: 12px; padding: 4px; margin-bottom: 14px;
}}
.tab-btn {{
  flex: 1; border: 0; background: transparent; color: var(--mut); font-weight: 700;
  padding: 10px 12px; border-radius: 10px; cursor: pointer; transition: .15s;
}}
.tab-btn.active {{ background: var(--card); color: var(--ink); box-shadow: var(--shadow); }}
.panel.hidden {{ display: none; }}
.card {{
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 18px; margin: 0 0 14px; box-shadow: var(--shadow);
}}
.lbl, label.field {{
  display: block; font-size: 11px; color: var(--mut); margin: 0 0 6px;
  text-transform: uppercase; letter-spacing: .06em; font-weight: 700;
}}
label.field {{ margin-top: 12px; }}
select, input[type=url], input[type=text], textarea {{
  width: 100%; padding: 11px 12px; border-radius: 10px; border: 1px solid var(--line);
  background: var(--input); color: var(--ink); font: inherit; transition: border-color .15s;
}}
select:focus, input:focus, textarea:focus {{ outline: none; border-color: var(--acc); }}
textarea {{ min-height: 280px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; line-height: 1.45; resize: vertical; }}
.row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
@media (max-width: 640px) {{
  .row {{ grid-template-columns: 1fr; }}
  .wrap {{ padding: 14px 12px 72px; }}
  h1 {{ font-size: 1.15rem; }}
}}
.btns {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }}
button.primary, button.secondary, button.ghost {{
  border: 0; border-radius: 10px; padding: 11px 16px; font-weight: 700; cursor: pointer;
  font: inherit; transition: transform .1s, opacity .15s;
}}
button.primary {{ background: var(--acc); color: #fff; }}
button.secondary {{ background: var(--line); color: var(--ink); }}
button.ghost {{ background: transparent; border: 1px solid var(--line); color: var(--ink); }}
button.primary:hover, button.secondary:hover, button.ghost:hover {{ filter: brightness(1.08); }}
button:active {{ transform: scale(.98); }}
button:disabled {{ opacity: .55; cursor: wait; }}
.check {{
  display: flex; gap: 10px; align-items: flex-start; margin-top: 10px;
  color: var(--ink); font-size: 14px; padding: 8px 10px; border-radius: 10px;
  background: var(--input); border: 1px solid transparent;
}}
.check:hover {{ border-color: var(--line); }}
.check input {{ margin-top: 3px; accent-color: var(--acc); }}
.ok {{ background: var(--ok-bg); color: var(--ok-fg); padding: 11px 12px; border-radius: 10px; margin: 0 0 12px; }}
.err {{ background: var(--err-bg); color: var(--err-fg); padding: 11px 12px; border-radius: 10px; margin: 0 0 12px; }}
.warn {{ color: var(--warn); }}
.mut {{ color: var(--mut); font-size: 13px; }}
.dates {{ display: flex; flex-direction: column; gap: 8px; max-height: 300px; overflow: auto; margin-top: 10px; padding-right: 2px; }}
.date-item {{
  display: flex; justify-content: space-between; gap: 10px; align-items: center;
  padding: 10px 12px; border: 1px solid var(--line); border-radius: 12px; background: var(--input);
  cursor: pointer; transition: border-color .15s, background .15s;
}}
.date-item:hover {{ border-color: var(--acc); }}
.date-item:has(input:checked) {{ border-color: var(--acc); background: color-mix(in srgb, var(--acc) 12%, var(--input)); }}
.date-main {{ display: flex; flex-direction: column; gap: 2px; flex: 1; }}
.date-main small {{ color: var(--mut); font-size: 12px; }}
.badge {{
  color: var(--mut); font-style: normal; font-size: 12px; white-space: nowrap;
  background: var(--card); border: 1px solid var(--line); padding: 4px 8px; border-radius: 999px;
}}
pre.out {{ white-space: pre-wrap; background: var(--input); padding: 12px; border-radius: 10px; font-size: 12.5px; border: 1px solid var(--line); max-height: 360px; overflow: auto; }}
code {{ background: var(--input); padding: 1px 6px; border-radius: 6px; font-size: 12.5px; }}
.replace-row {{ display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; align-items: end; }}
@media (max-width: 640px) {{ .replace-row {{ grid-template-columns: 1fr; }} }}
#overlay {{
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 50;
  align-items: center; justify-content: center; padding: 24px; backdrop-filter: blur(4px);
}}
#overlay.show {{ display: flex; }}
.loader {{
  width: min(420px, 100%); background: var(--card); border: 1px solid var(--line);
  border-radius: 16px; padding: 24px 20px; text-align: center; box-shadow: var(--shadow);
}}
.spin {{
  width: 36px; height: 36px; margin: 0 auto 14px; border-radius: 50%;
  border: 3px solid var(--line); border-top-color: var(--acc); animation: spin .75s linear infinite;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
#loadTitle {{ font-weight: 700; margin: 0 0 6px; }}
#loadSub {{ color: var(--mut); font-size: 14px; margin: 0 0 8px; }}
#loadEta {{
  display: inline-flex; align-items: center; gap: 8px; justify-content: center;
  margin: 0 auto 14px; padding: 8px 14px; border-radius: 999px;
  background: color-mix(in srgb, var(--acc) 12%, var(--input));
  border: 1px solid var(--line); color: var(--ink); font-weight: 700; font-size: 13px;
}}
#loadEta span.eta-num {{ color: var(--acc); font-variant-numeric: tabular-nums; }}
.progress-bar {{
  height: 6px; border-radius: 999px; background: var(--line); overflow: hidden; margin: 0 0 14px;
}}
.progress-bar > i {{
  display: block; height: 100%; width: 0%; background: linear-gradient(90deg, var(--acc), var(--acc2));
  border-radius: 999px; transition: width .4s ease;
}}
.steps {{ text-align: left; font-size: 13px; color: var(--mut); }}
.steps li {{ margin: 4px 0; }}
.steps li.on {{ color: var(--acc2); font-weight: 600; }}
.steps li.done {{ color: var(--mut); opacity: .7; }}
.watermark {{
  text-align: center; margin-top: 28px; padding: 16px 8px 8px;
  color: var(--mut); font-size: 12px; letter-spacing: .04em;
  border-top: 1px solid var(--line);
}}
.watermark strong {{ color: var(--ink); font-weight: 700; }}
.chip {{
  display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
  background: color-mix(in srgb, var(--acc) 15%, transparent); color: var(--acc); font-weight: 700;
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <div class="logo">MP</div>
      <div>
        <h1>MaxPreps Scraper</h1>
        <p class="sub">Scrape · Game Info · Export · Edit</p>
      </div>
    </div>
    <button type="button" class="theme-btn" id="themeToggle" aria-label="Toggle theme">
      <span id="themeIcon">🌙</span> <span id="themeLabel">Mode malam</span>
    </button>
  </div>

  <div class="tabs" role="tablist">
    <button type="button" class="tab-btn {tab_scrape}" data-tab="scrape" id="tabScrape">Scrape</button>
    <button type="button" class="tab-btn {tab_editor}" data-tab="editor" id="tabEditor">Edit TXT</button>
  </div>

  {err_html}{flash_html}

  <div id="panelScrape" class="panel {panel_scrape}">
  <form class="card" id="appForm" method="post" action="/">
    <div class="row">
      <div>
        <label class="field">Sport</label>
        <select name="sport">{sport_opts}</select>
      </div>
      <div>
        <label class="field">State</label>
        <select name="state">{state_opts}</select>
      </div>
    </div>

    <label class="field">📺 Watch live URL</label>
    <input type="url" name="watch" value="{htmllib.escape(watch)}" placeholder="https://prepwire.com/live/texas">
    <p class="mut">Link masuk ke baris watch live setiap match. Disimpan ke <code>watch_links.txt</code>.</p>

    <label class="check"><input type="checkbox" name="add_title" value="1" {title_chk}> Tambah <code>?title=TeamA%20Vs.%20TeamB</code> di watch live</label>
    <label class="check"><input type="checkbox" name="split_state" value="1" {split_chk}> File terpisah per state (<code>schedules/tx-football.txt</code>)</label>
    <label class="check"><input type="checkbox" name="overwrite" value="1" {over_chk}> Timpa file (hapus isi lama)</label>
    <label class="check"><input type="checkbox" name="mascots" value="1" {mascot_chk}> Ambil mascot dari halaman game (lebih lambat)</label>
    <p class="mut" style="margin-top:10px">Detail = <b>Game Info</b> MaxPreps. Format: Title → teams → 📺watch → MascotA @ MascotB → 🗒️ → hashtag. Tanggal hanya <span class="chip">hari ini &amp; mendatang</span>.</p>

    <input type="hidden" name="action" id="actionField" value="">
    <input type="hidden" name="tab" value="scrape">
    <div class="btns">
      <button class="secondary" type="submit" name="action_btn" value="dates" id="btnDates">Cek tanggal</button>
      <button class="primary" type="submit" name="action_btn" value="scrape" id="btnScrape">Scrape tanggal terpilih</button>
    </div>
    {date_block}
    {src_html}
  </form>
  {out_html}
  {prev_html}
  </div>

  <div id="panelEditor" class="panel {panel_editor}">
    {ed_msg}{ed_err}
    <form class="card" method="post" action="/" id="editorForm">
      <input type="hidden" name="action" value="editor_load">
      <input type="hidden" name="tab" value="editor">
      <label class="field">File schedules</label>
      <div class="row" style="align-items:end">
        <div>
          <select name="editor_file" id="editorFileSelect">{file_opts}</select>
        </div>
        <div class="btns" style="margin:0">
          <button class="secondary" type="submit" name="action_btn" value="editor_load">Buka file</button>
        </div>
      </div>
      <p class="mut">Pilih file hasil scrape, lalu find &amp; replace seperti Ctrl+H di VS Code.</p>
    </form>

    <form class="card" method="post" action="/" id="replaceForm">
      <input type="hidden" name="action" value="editor_replace">
      <input type="hidden" name="tab" value="editor">
      <input type="hidden" name="editor_file" value="{htmllib.escape(editor_file)}">
      <p class="lbl">Find &amp; Replace <span class="chip">semua kemunculan</span></p>
      <div class="replace-row">
        <div>
          <label class="field">Cari</label>
          <input type="text" name="find" value="{htmllib.escape(find_val)}" placeholder="kata atau kalimat">
        </div>
        <div>
          <label class="field">Ganti dengan</label>
          <input type="text" name="replace" value="{htmllib.escape(replace_val)}" placeholder="teks pengganti (boleh kosong)">
        </div>
        <div class="btns" style="margin:0">
          <button class="primary" type="submit" name="action_btn" value="editor_replace">Replace all</button>
        </div>
      </div>
      <label class="check"><input type="checkbox" name="case_sensitive" value="1"> Case sensitive</label>
      <p class="mut">Replace mengubah file di disk. Preview di bawah ikut ter-update.</p>

      <label class="field">Isi file {htmllib.escape(os.path.basename(editor_file) if editor_file else '')}</label>
      <textarea name="editor_content" id="editorTextarea" placeholder="Buka file dulu…">{htmllib.escape(ed_content)}</textarea>
      <div class="btns">
        <button class="secondary" type="submit" name="action_btn" value="editor_save" formaction="/" formmethod="post">Simpan manual</button>
      </div>
      <input type="hidden" name="action_save" id="actionSaveField" value="">
    </form>
  </div>

  <div class="watermark">
    scraper by <strong>Ridwan</strong> · MaxPreps UI
  </div>
</div>

<div id="overlay" aria-live="polite">
  <div class="loader">
    <div class="spin"></div>
    <p id="loadTitle">Sedang mengambil data</p>
    <p id="loadSub">Jangan tutup halaman ini</p>
    <div id="loadEta">Estimasi: <span class="eta-num" id="etaNum">—</span></div>
    <div class="progress-bar"><i id="progBar"></i></div>
    <ol class="steps" id="loadSteps"></ol>
  </div>
</div>

<script>
(function() {{
  // Theme
  var root = document.documentElement;
  var stored = localStorage.getItem('mp-theme');
  if (stored === 'light' || stored === 'dark') root.setAttribute('data-theme', stored);
  function syncThemeBtn() {{
    var dark = root.getAttribute('data-theme') !== 'light';
    document.getElementById('themeIcon').textContent = dark ? '🌙' : '☀️';
    document.getElementById('themeLabel').textContent = dark ? 'Mode malam' : 'Mode siang';
  }}
  syncThemeBtn();
  document.getElementById('themeToggle').addEventListener('click', function() {{
    var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    localStorage.setItem('mp-theme', next);
    syncThemeBtn();
  }});

  // Tabs
  function showTab(name) {{
    document.getElementById('panelScrape').classList.toggle('hidden', name !== 'scrape');
    document.getElementById('panelEditor').classList.toggle('hidden', name !== 'editor');
    document.getElementById('tabScrape').classList.toggle('active', name === 'scrape');
    document.getElementById('tabEditor').classList.toggle('active', name === 'editor');
  }}
  document.getElementById('tabScrape').addEventListener('click', function() {{ showTab('scrape'); }});
  document.getElementById('tabEditor').addEventListener('click', function() {{ showTab('editor'); }});

  // Loader for scrape form
  var form = document.getElementById('appForm');
  var overlay = document.getElementById('overlay');
  var title = document.getElementById('loadTitle');
  var sub = document.getElementById('loadSub');
  var stepsEl = document.getElementById('loadSteps');
  var etaNum = document.getElementById('etaNum');
  var progBar = document.getElementById('progBar');
  var timers = [];
  var etaTimer = null;

  function fmtSec(s) {{
    s = Math.max(0, Math.ceil(s));
    if (s < 60) return s + ' dtk';
    var m = Math.floor(s / 60), r = s % 60;
    return m + ' mnt' + (r ? ' ' + r + ' dtk' : '');
  }}

  function show(mode) {{
    timers.forEach(clearTimeout);
    timers = [];
    if (etaTimer) {{ clearInterval(etaTimer); etaTimer = null; }}

    var hidden = document.getElementById('actionField');
    if (hidden) hidden.value = mode;
    overlay.classList.add('show');
    document.querySelectorAll('button').forEach(function(b) {{ b.style.pointerEvents = 'none'; }});

    var all = (document.querySelector('select[name=state]') || {{}}).value === 'all';
    var mascotsOn = !!(document.querySelector('input[name=mascots]') || {{}}).checked;
    var stateSel = document.querySelector('select[name=state]');
    var nStates = all ? 52 : 1;

    // Estimasi kasar (detik) berdasarkan pengalaman request MaxPreps
    var etaTotal;
    if (mode === 'dates') {{
      etaTotal = all ? 8 : 4;
    }} else {{
      // Paralel: ~8 state workers; Game Info limit lebih ketat di all-states
      if (all) {{
        etaTotal = mascotsOn ? 180 : 120; // ~2–3 menit (paralel)
      }} else {{
        etaTotal = mascotsOn ? 40 : 22;
      }}
    }}
    etaTotal = Math.round(etaTotal);

    var datesPlan = [
      ['Menghubungi MaxPreps', 0],
      ['Mengambil kalender tanggal', 0.25],
      ['Filter hari ini & mendatang', 0.55],
      ['Menyusun daftar tanggal', 0.85]
    ];
    var scrapePlan = [
      ['Menghubungi MaxPreps', 0],
      ['Mengambil halaman skor', 0.12],
      ['Mem-parse kartu pertandingan', 0.28],
      ['Ambil Game Info per match', 0.45],
      ['Menulis file ke disk', 0.82],
      ['Selesai sebentar lagi', 0.95]
    ];
    var plan = mode === 'scrape' ? scrapePlan : datesPlan;

    title.textContent = mode === 'scrape'
      ? (all ? 'Scrape semua state & tulis file' : 'Scrape & tulis file')
      : 'Mengambil tanggal tersedia';
    sub.textContent = mode === 'scrape'
      ? (all
          ? ('~' + nStates + ' state · jeda antar state · ' + (mascotsOn ? 'mascot ON' : 'Game Info'))
          : (mascotsOn ? 'Mascot + Game Info aktif — lebih lama' : 'Game Info dari halaman game'))
      : 'Hanya tanggal hari ini & mendatang';

    etaNum.textContent = fmtSec(etaTotal);
    if (progBar) progBar.style.width = '2%';

    stepsEl.innerHTML = plan.map(function(p) {{ return '<li>' + p[0] + '</li>'; }}).join('');
    var lis = stepsEl.querySelectorAll('li');
    if (lis[0]) lis[0].className = 'on';

    var started = Date.now();
    etaTimer = setInterval(function() {{
      var elapsed = (Date.now() - started) / 1000;
      var left = Math.max(0, etaTotal - elapsed);
      etaNum.textContent = left <= 1 ? 'segera selesai…' : fmtSec(left);
      var pct = Math.min(96, (elapsed / etaTotal) * 100);
      if (progBar) progBar.style.width = pct + '%';
    }}, 400);

    plan.forEach(function(p, i) {{
      var at = Math.round(p[1] * etaTotal * 1000);
      timers.push(setTimeout(function() {{
        lis.forEach(function(li, j) {{
          li.className = j < i ? 'done' : (j === i ? 'on' : '');
        }});
        sub.textContent = p[0] + '…';
      }}, at));
    }});
  }}

  if (form) {{
    form.addEventListener('submit', function(e) {{
      var submitter = e.submitter || document.activeElement;
      var action = (submitter && submitter.value) ? submitter.value : 'dates';
      var hidden = document.getElementById('actionField');
      if (hidden) hidden.value = action;
      show(action);
    }});
  }}
  var bd = document.getElementById('btnDates');
  var bs = document.getElementById('btnScrape');
  if (bd) bd.addEventListener('click', function() {{ document.getElementById('actionField').value = 'dates'; }});
  if (bs) bs.addEventListener('click', function() {{ document.getElementById('actionField').value = 'scrape'; }});

  // Editor: switch action for save button
  var replaceForm = document.getElementById('replaceForm');
  if (replaceForm) {{
    replaceForm.addEventListener('submit', function(e) {{
      var submitter = e.submitter || document.activeElement;
      var val = submitter && submitter.value;
      var act = replaceForm.querySelector('input[name=action]');
      if (val === 'editor_save' && act) act.value = 'editor_save';
      if (val === 'editor_replace' && act) act.value = 'editor_replace';
    }});
  }}
}})();
</script>
</body>
</html>
"""




class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("UI " + (fmt % args) + "\n")

    def _send(self, body: str, code: int = 200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/schedules.txt", "/file"):
            qs = urllib.parse.parse_qs(parsed.query)
            req_path = (qs.get("path") or [""])[0] if parsed.path == "/file" else OUTPUT_FILE
            if parsed.path == "/schedules.txt":
                req_path = OUTPUT_FILE
            # only allow files inside BASE_DIR
            abs_path = os.path.abspath(req_path)
            if not abs_path.startswith(os.path.abspath(BASE_DIR)) or not os.path.isfile(abs_path):
                self.send_error(404)
                return
            with open(abs_path, "rb") as fh:
                raw = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'inline; filename="{os.path.basename(abs_path)}"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        mapping = load_watch_map()
        self._send(page_html({
            "watch": mapping.get("default", WATCH_LIVE_DEFAULT),
            "split_state": True,
            "overwrite": False,
            "add_title": True,
        }))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)
        get = lambda k, default="": (form.get(k) or [default])[0]
        action = get("action") or get("action_btn")
        tab = get("tab") or "scrape"
        sport = get("sport", "football")
        state = get("state", "tx").lower()
        watch = get("watch").strip()
        date = get("date").strip()
        mascots = get("mascots") == "1"
        overwrite = get("overwrite") == "1"
        add_title = get("add_title") == "1"
        # checkbox absent = off; default split on first load only
        split_state = "split_state" in form and get("split_state") == "1"
        if sport not in SPORTS:
            sport = "football"
        if state != "all" and state not in STATES:
            state = "tx"

        def selected_states():
            return list(STATES.keys()) if state == "all" else [state]

        ctx = {
            "sport": sport,
            "state": state,
            "watch": watch or load_watch_map().get(state) or load_watch_map().get("default") or WATCH_LIVE_DEFAULT,
            "date": date,
            "mascots": mascots,
            "add_title": add_title if action else True,
            "overwrite": overwrite,
            "split_state": split_state if action else True,
            "outfile": "",
            "checked": False,
            "dates": [],
            "flash": "",
            "error": "",
            "preview": "",
            "source": "",
            "tab": tab,
            "editor_file": get("editor_file"),
            "find_val": get("find"),
            "replace_val": get("replace"),
        }

        try:
            if action == "dates":
                if state == "all":
                    code, url, dates, probe = list_dates_with_fallback(sport)
                    who = f"ALL STATES (kalender acuan {STATES.get(probe, probe.upper())})"
                else:
                    code, url, dates = list_dates(state, sport)
                    probe = state
                    who = f"{STATES.get(state, state.upper())} / {SPORTS[sport][0]}"
                ctx["checked"] = True
                ctx["dates"] = dates
                ctx["source"] = f"HTTP {code} {url}"
                if code != 200:
                    ctx["error"] = (
                        f"Gagal ambil kalender (HTTP {code}). "
                        "Nyalakan VPN di perangkat yang menjalankan Python ini."
                    )
                elif dates:
                    extra = " Semua state akan pakai tanggal yang kamu pilih." if state == "all" else ""
                    ctx["flash"] = f"{len(dates)} tanggal punya game — {who}.{extra}"
                else:
                    ctx["error"] = "Kalender kosong. State/sport ini mungkin off-season, atau halaman diblokir."

            elif action == "scrape":
                if not date:
                    if state == "all":
                        code, url, dates, _probe = list_dates_with_fallback(sport)
                    else:
                        code, url, dates = list_dates(state, sport)
                    ctx["checked"] = True
                    ctx["dates"] = dates
                    ctx["source"] = f"HTTP {code} {url}"
                    ctx["error"] = "Pilih tanggal dulu. Klik Cek tanggal, lalu pilih salah satu."
                else:
                    if watch:
                        save_watch_link(watch, "default" if state == "all" else state, sport)
                    mdy = to_mdy(date)
                    targets = selected_states()
                    total_written = total_skipped = total_matches = 0
                    failed: List[str] = []
                    last_blocks: List[str] = []
                    last_dest = ""
                    last_code, last_url = 0, ""
                    all_blocks: List[str] = []
                    # All-states: paralel + Game Info lebih hemat (kecuali mascot ON)
                    is_all = state == "all"
                    gi_limit = (20 if mascots else 12) if is_all else (60 if mascots else 40)
                    do_gi = True  # tetap ambil Game Info, tapi limit lebih ketat di all
                    st_workers = 8 if is_all else 1
                    g_workers = 4 if is_all else 5

                    if is_all:
                        all_blocks, failed, last_code, last_url, last_blocks = scrape_states_parallel(
                            targets, sport, mdy, watch,
                            mascots, gi_limit, False, add_title,
                            with_game_info=do_gi, game_info_limit=gi_limit,
                            state_workers=st_workers, game_workers=g_workers,
                        )
                        total_matches = len(all_blocks)
                        dest = all_states_file(sport, mdy)
                        written, skipped = write_blocks(dest, all_blocks, overwrite=overwrite)
                        total_written, total_skipped = written, skipped
                        last_dest = dest
                        last_blocks = all_blocks
                    else:
                        st = targets[0]
                        wurl = resolve_watch_url(watch, load_watch_map(), st, sport)
                        code, url, blocks = scrape_state(
                            st, sport, mdy, wurl or WATCH_LIVE_DEFAULT,
                            mascots, gi_limit, False, add_title=add_title,
                            with_game_info=do_gi, game_info_limit=gi_limit,
                            game_workers=g_workers,
                        )
                        last_code, last_url = code, url
                        if code != 200:
                            failed.append(f"{st.upper()} HTTP {code}")
                        else:
                            total_matches = len(blocks)
                            dest = state_file(st, sport) if split_state else OUTPUT_FILE
                            written, skipped = write_blocks(dest, blocks, overwrite=overwrite)
                            total_written, total_skipped = written, skipped
                            last_blocks = blocks
                            last_dest = dest
                    # Jangan fetch kalender lagi (hemat waktu); pakai yang sudah ada jika ada
                    if is_all:
                        code, url, dates, _p = list_dates_with_fallback(sport)
                    else:
                        code, url, dates = list_dates(state, sport)
                    ctx["checked"] = True
                    ctx["dates"] = dates
                    ctx["date"] = mdy
                    ctx["source"] = f"HTTP {last_code} {last_url}"
                    ctx["outfile"] = last_dest
                    if state == "all":
                        mode = "TIMPA" if overwrite else "append"
                        ctx["flash"] = (
                            f"All states selesai → {os.path.basename(last_dest or '-')} ({mode}). "
                            f"{len(targets)} state, {total_matches} match, "
                            f"written {total_written}, skip {total_skipped}."
                        )
                        if failed:
                            ctx["error"] = "Gagal: " + ", ".join(failed[:12]) + ("…" if len(failed) > 12 else "")
                        if total_matches == 0 and not failed:
                            ctx["error"] = "Tidak ada match yang berhasil diambil."
                    elif last_code != 200:
                        ctx["error"] = f"Scrape gagal HTTP {last_code}. Pakai VPN di perangkat ini."
                    else:
                        mode = "TIMPA" if overwrite else "append"
                        ctx["flash"] = (
                            f"Selesai. {total_matches} match → {os.path.basename(last_dest or '-')} ({mode}). "
                            f"Written {total_written}, skip duplikat {total_skipped}."
                        )
                        if not last_blocks:
                            ctx["error"] = "Halaman terbuka tapi parser tidak menemukan game card."
                    ctx["preview"] = "".join(last_blocks[:8])
            elif action in ("editor_load", "editor_replace", "editor_save"):
                ctx["tab"] = "editor"
                epath = safe_schedule_path(get("editor_file"))
                ctx["editor_file"] = epath or get("editor_file")
                ctx["find_val"] = get("find")
                ctx["replace_val"] = get("replace")
                if action == "editor_load":
                    if not epath:
                        ctx["editor_err"] = "Pilih file schedules yang valid."
                    else:
                        with open(epath, encoding="utf-8") as fh:
                            ctx["editor_content"] = fh.read()
                        ctx["editor_msg"] = f"File dibuka: {os.path.basename(epath)} ({len(ctx['editor_content'])} karakter)."
                elif action == "editor_replace":
                    if not epath:
                        ctx["editor_err"] = "Pilih / buka file dulu."
                    else:
                        find = get("find")
                        repl = get("replace")
                        case_sens = get("case_sensitive") == "1"
                        with open(epath, encoding="utf-8") as fh:
                            content = fh.read()
                        if not find:
                            ctx["editor_err"] = "Isi kata/kalimat yang dicari."
                            ctx["editor_content"] = content
                        else:
                            if case_sens:
                                count = content.count(find)
                                new_content = content.replace(find, repl)
                            else:
                                # case-insensitive replace all
                                pattern = re.compile(re.escape(find), re.IGNORECASE)
                                count = len(pattern.findall(content))
                                new_content = pattern.sub(repl, content)
                            with open(epath, "w", encoding="utf-8") as fh:
                                fh.write(new_content)
                            ctx["editor_content"] = new_content
                            ctx["editor_msg"] = (
                                f"Replace all selesai di {os.path.basename(epath)}: "
                                f"{count} kemunculan diganti."
                            )
                elif action == "editor_save":
                    if not epath:
                        ctx["editor_err"] = "Pilih file dulu."
                    else:
                        # content may come from textarea
                        body = get("editor_content")
                        with open(epath, "w", encoding="utf-8") as fh:
                            fh.write(body)
                        ctx["editor_content"] = body
                        ctx["editor_msg"] = f"Disimpan: {os.path.basename(epath)}."
            else:
                ctx["error"] = (
                    "Aksi tidak terbaca (action kosong). "
                    "Refresh halaman, pastikan pakai file scraper terbaru, lalu coba lagi."
                )
        except Exception as ex:
            ctx["error"] = f"Error server: {type(ex).__name__}: {ex}"

        self._send(page_html(ctx))


def run_ui(host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"UI siap → http://{host}:{port}")
    print("Ctrl+C untuk stop. Jalankan di perangkat yang VPN-nya ON.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstop")
        httpd.server_close()


def main() -> int:
    ap = argparse.ArgumentParser(description="MaxPreps scraper")
    ap.add_argument("--ui", action="store_true", help="Buka UI web (default jika tanpa argumen)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--state", default="")
    ap.add_argument("--sport", default="football")
    ap.add_argument("--date", default="")
    ap.add_argument("--watch", default="")
    ap.add_argument("--check-dates", action="store_true")
    ap.add_argument("--mascots", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="Timpa file state (jangan append)")
    ap.add_argument("--combined", action="store_true", help="Tulis ke schedules.txt gabungan, bukan file per state")
    ap.add_argument("--all-states", action="store_true", help="Scrape semua state")
    ap.add_argument("--no-title", action="store_true", help="Jangan tambah ?title= di watch live")
    args = ap.parse_args()

    # no scrape args → UI
    if args.ui or (not args.state and not args.check_dates and not args.date):
        run_ui(args.host, args.port)
        return 0

    state = (args.state or "tx").lower()
    if args.check_dates:
        code, url, days = list_dates(state, args.sport)
        print(f"HTTP {code} {url}")
        for d, c in days:
            print(f"  {d:12} {c:4}  {format_tanggal(d)}")
        return 0 if code == 200 else 1

    watch = args.watch or resolve_watch_url("", load_watch_map(), state if state != "all" else "tx", args.sport)
    mdy = to_mdy(args.date or datetime.now().strftime("%Y-%m-%d"))
    if watch:
        save_watch_link(watch, state if state in STATES else "", args.sport)
    targets = list(STATES.keys()) if (args.all_states or state == "all") else [state]
    any_fail = False
    all_blocks: List[str] = []
    do_all = args.all_states or state == "all"
    gi_limit = (20 if args.mascots else 12) if do_all else (60 if args.mascots else 40)
    if do_all:
        all_blocks, failed, last_code, last_url, _ = scrape_states_parallel(
            targets, args.sport, mdy, watch,
            args.mascots, gi_limit, False, not args.no_title,
            with_game_info=True, game_info_limit=gi_limit,
            state_workers=8, game_workers=4,
        )
        for msg in failed:
            print("[fail]", msg)
            any_fail = True
        print(f"parallel done matches={len(all_blocks)} failed={len(failed)}")
    else:
        for st in targets:
            wurl = resolve_watch_url(watch, load_watch_map(), st, args.sport)
            code, url, blocks = scrape_state(
                st, args.sport, mdy, wurl, args.mascots, gi_limit,
                False, add_title=not args.no_title, with_game_info=True,
                game_info_limit=gi_limit, game_workers=5,
            )
            print(f"[{st}] HTTP {code} {url}  matches={len(blocks)}")
            if code != 200:
                any_fail = True
            else:
                dest = OUTPUT_FILE if args.combined else state_file(st, args.sport)
                w, s = write_blocks(dest, blocks, overwrite=args.overwrite)
                print(f"  written={w} skipped={s} file={dest}")
    if do_all:
        dest = all_states_file(args.sport, mdy)
        w, s = write_blocks(dest, all_blocks, overwrite=args.overwrite)
        print(f"ALL → written={w} skipped={s} file={dest} matches={len(all_blocks)}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
