#!/usr/bin/env python3
"""
ani-ui: Local web player and catalog browser for ani-cli.
Uses only the Python standard library.
"""

import os
import re
import json
import time
import base64
import html
import datetime
import shutil
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import threading
import concurrent.futures
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = int(os.environ.get("PORT", 5050))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BASE_URL = "https://hianime.at"
STATE_DIR = os.path.expanduser(os.environ.get("ANI_CLI_HIST_DIR", "~/.local/state/ani-cli"))
HIST_FILE = os.path.join(STATE_DIR, "ani-hsts")
FAV_FILE = os.path.join(STATE_DIR, "favorites.txt")
FAV_CACHE_FILE = os.path.join(STATE_DIR, "favorites_cache.json")
FAV_LIST_CACHE_FILE = os.path.join(STATE_DIR, "favorites_list_cache.json")
RESOLVE_CACHE_FILE = os.path.join(STATE_DIR, "title_resolve_cache.json")
MAL_DESCRIPTIONS_CACHE_FILE = os.path.join(STATE_DIR, "mal_descriptions_cache.json")
THUMB_CACHE_DIR = os.path.join(STATE_DIR, "thumb_cache")
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
SECRETS_FILE = os.path.expanduser("~/.secrets.env")

# In-memory caches
CACHE = {}
AIRING_CACHE = {}
MAL_TOKEN_CACHE = {"access_token": None, "expires_at": 0}

# Persistent title resolution cache
RESOLVE_CACHE = {}
if os.path.exists(RESOLVE_CACHE_FILE):
    try:
        with open(RESOLVE_CACHE_FILE, "r") as f:
            RESOLVE_CACHE = json.load(f)
    except Exception:
        RESOLVE_CACHE = {}

def save_resolve_cache():
    try:
        with open(RESOLVE_CACHE_FILE, "w") as f:
            json.dump(RESOLVE_CACHE, f, indent=2)
    except Exception:
        pass

# Persistent MAL descriptions/synopsis cache
MAL_DESCRIPTIONS_CACHE = {}
if os.path.exists(MAL_DESCRIPTIONS_CACHE_FILE):
    try:
        with open(MAL_DESCRIPTIONS_CACHE_FILE, "r") as f:
            MAL_DESCRIPTIONS_CACHE = json.load(f)
    except Exception:
        MAL_DESCRIPTIONS_CACHE = {}

def save_mal_descriptions_cache():
    try:
        with open(MAL_DESCRIPTIONS_CACHE_FILE, "w") as f:
            json.dump(MAL_DESCRIPTIONS_CACHE, f, indent=2)
    except Exception:
        pass

MAL_MAP_FILE = "/Users/sampomerantz/MAL-Map/data-collection/data/min_metadata.json"
MAL_MAP_METADATA = {}
if os.path.exists(MAL_MAP_FILE):
    try:
        with open(MAL_MAP_FILE, "r") as f:
            MAL_MAP_METADATA = json.load(f)
    except Exception:
        MAL_MAP_METADATA = {}

def get_mal_map_url(mal_id=None, title=None, alt_title=None):
    if mal_id and str(mal_id) in MAL_MAP_METADATA:
        t = MAL_MAP_METADATA[str(mal_id)].get("title", "")
        if t:
            c = re.sub(r'[^a-z0-9]', '_', t.lower())
            return f"https://sampom100.github.io/MAL-Map/#show={c}"
    for cand in [alt_title, title]:
        if cand:
            c = re.sub(r'[^a-z0-9]', '_', cand.lower())
            return f"https://sampom100.github.io/MAL-Map/#show={c}"
    return "https://sampom100.github.io/MAL-Map/"

def get_secrets():
    secrets = {}
    if os.path.exists(SECRETS_FILE):
        try:
            with open(SECRETS_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        secrets[k.strip()] = v.strip("\"'")
        except Exception:
            pass
    return secrets

def update_secret(key, value):
    secrets = get_secrets()
    secrets[key] = value
    lines = []
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}="):
                    continue
                lines.append(line.rstrip("\n"))
    lines.append(f"{key}={value}")
    with open(SECRETS_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")

def get_mal_access_token():
    now = time.time()
    if MAL_TOKEN_CACHE["access_token"] and now < MAL_TOKEN_CACHE["expires_at"] - 60:
        return MAL_TOKEN_CACHE["access_token"]

    secrets = get_secrets()
    client_id = secrets.get("MAL_CLIENT_ID", "ea58d724802b654ee16451f5c377b6cf")
    client_secret = secrets.get("MAL_CLIENT_SECRET", "")
    refresh_token = secrets.get("MAL_REFRESH_TOKEN", "")

    if not refresh_token:
        raise ValueError("MAL_REFRESH_TOKEN not found in ~/.secrets.env")

    post_data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://myanimelist.net/v1/oauth2/token",
        data=post_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        token_res = json.loads(resp.read().decode())
        MAL_TOKEN_CACHE["access_token"] = token_res["access_token"]
        MAL_TOKEN_CACHE["expires_at"] = now + token_res.get("expires_in", 3600)
        
        new_refresh = token_res.get("refresh_token")
        if new_refresh and new_refresh != refresh_token:
            update_secret("MAL_REFRESH_TOKEN", new_refresh)
            
        return MAL_TOKEN_CACHE["access_token"]

def http_get(url, headers_extra=None):
    headers = {"User-Agent": USER_AGENT}
    if headers_extra:
        headers.update(headers_extra)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="ignore")

RESOLVED_MAL_IDS = {}

def clean_title(title):
    return re.sub(r'[^a-zA-Z0-9]', '', title).lower()

def clean_for_search(title):
    if not title:
        return ""
    t = re.sub(r'\(.*?\)', '', title)
    t = re.sub(r'\[.*?\]', '', t)
    t = re.sub(r'\b(lv|season\s*\d+|part\s*\d+)\b', '', t, flags=re.IGNORECASE)
    return t.strip()

def format_countdown_str(seconds, status=None, total_eps=None, next_ep=None):
    if status == "FINISHED":
        return f"Completed ({total_eps or '?'} eps)" if total_eps else "Completed"
    if status == "NOT_YET_RELEASED":
        return "Upcoming"
    if seconds is None:
        return status or None
    if seconds <= 0:
        return f"Ep {next_ep} Airing now" if next_ep else "Airing now"
    days = int(seconds) // 86400
    hours = (int(seconds) % 86400) // 3600
    mins = (int(seconds) % 3600) // 60
    if days > 0:
        cd = f"{days}d {hours}h"
    elif hours > 0:
        cd = f"{hours}h {mins}m"
    else:
        cd = f"{mins}m"
    return f"Ep {next_ep} in {cd}" if next_ep else f"in {cd}"

def get_anime_airing_info(title, alt_titles=None):
    if not title:
        return None
    c_title = clean_title(title)
    now_ts = time.time()

    # 1. In-memory cache
    if c_title in AIRING_CACHE:
        entry = AIRING_CACHE[c_title]
        if now_ts - entry.get("updated_at", 0) < 1800:
            airing_at = entry.get("airing_at")
            diff = int(airing_at - now_ts) if airing_at else (999999999 if entry.get("status") == "FINISHED" else 888888888)
            return {
                **entry,
                "time_until": diff,
                "countdown": format_countdown_str(diff if airing_at else None, status=entry.get("status"), total_eps=entry.get("episodes"), next_ep=entry.get("next_ep"))
            }

    # 2. Check favorites_cache.json
    fav_cache = {}
    if os.path.exists(FAV_CACHE_FILE):
        try:
            with open(FAV_CACHE_FILE, "r") as f:
                fav_cache = json.load(f)
        except Exception:
            pass

    candidates = [title]
    if alt_titles:
        for at in alt_titles:
            if at and at not in candidates:
                candidates.append(at)

    for cand in candidates:
        cand_c = clean_title(cand)
        for k, v in fav_cache.items():
            if clean_title(k) == cand_c or clean_title(v.get("display_title", "")) == cand_c:
                airing_at = v.get("airing_at")
                updated_at = v.get("updated_at", 0)
                if (airing_at and airing_at > now_ts) or v.get("status") == "FINISHED":
                    diff = int(airing_at - now_ts) if airing_at else (999999999 if v.get("status") == "FINISHED" else 888888888)
                    res = {
                        "next_ep": v.get("next_ep"),
                        "airing_at": airing_at,
                        "status": v.get("status"),
                        "episodes": v.get("episodes"),
                        "updated_at": updated_at,
                        "time_until": diff,
                        "countdown": format_countdown_str(diff if airing_at else None, status=v.get("status"), total_eps=v.get("episodes"), next_ep=v.get("next_ep"))
                    }
                    AIRING_CACHE[c_title] = res
                    return res

    # 3. AniList GraphQL lookup
    search_queries = []
    for cand in candidates:
        if cand and cand not in search_queries:
            search_queries.append(cand)
        cl = clean_for_search(cand)
        if cl and cl not in search_queries:
            search_queries.append(cl)

    query = """
    query ($search: String) {
      Media(search: $search, type: ANIME) {
        id
        title {
          english
          romaji
        }
        status
        episodes
        nextAiringEpisode {
          episode
          timeUntilAiring
          airingAt
        }
      }
    }
    """

    for sq in search_queries:
        try:
            req = urllib.request.Request(
                "https://graphql.anilist.co",
                data=json.dumps({"query": query, "variables": {"search": sq}}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                media = data.get("data", {}).get("Media")
                if media:
                    next_ep_obj = media.get("nextAiringEpisode")
                    next_ep = next_ep_obj.get("episode") if next_ep_obj else None
                    airing_at = next_ep_obj.get("airingAt") if next_ep_obj else None
                    status = media.get("status")
                    episodes = media.get("episodes")
                    diff = int(airing_at - now_ts) if airing_at else (999999999 if status == "FINISHED" else 888888888)

                    info = {
                        "next_ep": next_ep,
                        "airing_at": airing_at,
                        "status": status,
                        "episodes": episodes,
                        "updated_at": now_ts,
                        "time_until": diff,
                        "countdown": format_countdown_str(diff if airing_at else None, status=status, total_eps=episodes, next_ep=next_ep)
                    }
                    AIRING_CACHE[c_title] = info

                    if os.path.exists(FAV_CACHE_FILE):
                        try:
                            fav_cache[title.lower()] = {
                                "display_title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji") or title,
                                "status": status,
                                "episodes": episodes,
                                "next_ep": next_ep,
                                "airing_at": airing_at,
                                "updated_at": now_ts
                            }
                            with open(FAV_CACHE_FILE, "w") as fp:
                                json.dump(fav_cache, fp, indent=2)
                        except Exception:
                            pass

                    return info
        except Exception:
            continue

    fallback = {
        "next_ep": None,
        "airing_at": None,
        "status": "UNKNOWN",
        "episodes": None,
        "updated_at": now_ts,
        "time_until": 999999999,
        "countdown": None
    }
    AIRING_CACHE[c_title] = fallback
    return fallback

def resolve_mal_id_from_anilist(title):
    if not title:
        return None
    try:
        query = """
        query ($search: String) {
          Media (search: $search, type: ANIME) {
            idMal
          }
        }
        """
        req = urllib.request.Request(
            "https://graphql.anilist.co",
            data=json.dumps({"query": query, "variables": {"search": title}}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return data.get("data", {}).get("Media", {}).get("idMal")
    except Exception:
        return None

def resolve_mal_id_from_hianime(anime_id):
    if not anime_id:
        return None
    if anime_id in RESOLVED_MAL_IDS:
        return RESOLVED_MAL_IDS[anime_id]

    # Method 1: Check episode 1 stream servers (ZokoAnime embed contains true MAL ID in URL)
    try:
        num_id = anime_id.split("-")[-1]
        url = f"{BASE_URL}/api/theme/episode/list/{num_id}"
        raw = http_get(url)
        data = json.loads(raw)
        ep_ids = re.findall(r'data-id="([0-9]+)"', data.get("html", ""))
        if ep_ids:
            s_url = f"{BASE_URL}/api/theme/episode/servers?episodeId={ep_ids[0]}"
            s_raw = http_get(s_url)
            s_data = json.loads(s_raw)
            for h in re.findall(r'data-hash="([^"]+)"', s_data.get("html", "")):
                try:
                    dec = base64.b64decode(h).decode("utf-8", errors="ignore")
                    m = re.search(r'/stream/mal/([0-9]+)/', dec)
                    if m:
                        mal_id = int(m.group(1))
                        RESOLVED_MAL_IDS[anime_id] = mal_id
                        return mal_id
                except Exception:
                    pass
    except Exception:
        pass

    # Method 2: Check page title, Synonyms, and Japanese title
    main_title = None
    syns = []
    japs = []
    try:
        page_html = http_get(f"{BASE_URL}/{anime_id}")
        t_m = re.search(r'<h2[^>]*class="[^"]*film-name[^"]*"[^>]*>(?:<a[^>]*>)?([^<]+)', page_html)
        if t_m:
            main_title = html.unescape(t_m.group(1).strip())
        syns = re.findall(r'Synonyms:</span>\s*<span class="name">([^<]+)</span>', page_html)
        japs = re.findall(r'Japanese:</span>\s*<span class="name">([^<]+)</span>', page_html)
    except Exception:
        pass

    candidates = []
    if main_title:
        candidates.append(main_title)
    for s in syns:
        for it in s.split(","):
            it_s = it.strip()
            if it_s and it_s not in candidates:
                candidates.append(it_s)
    for j in japs:
        j_s = j.strip()
        if j_s and j_s not in candidates:
            candidates.append(j_s)

    slug_title = anime_id.rsplit("-", 1)[0].replace("-", " ")
    if slug_title and slug_title not in candidates:
        candidates.append(slug_title)

    secrets = get_secrets()
    client_id = secrets.get("MAL_CLIENT_ID", "ea58d724802b654ee16451f5c377b6cf")
    for cand in candidates:
        try:
            q = urllib.parse.quote(cand.strip())
            search_url = f"https://api.myanimelist.net/v2/anime?q={q}&limit=1&fields=id,title&nsfw=true"
            req = urllib.request.Request(search_url, headers={"X-MAL-CLIENT-ID": client_id})
            with urllib.request.urlopen(req, timeout=5) as resp:
                res = json.loads(resp.read().decode())
                entries = res.get("data", [])
                if entries:
                    mal_id = entries[0]["node"]["id"]
                    RESOLVED_MAL_IDS[anime_id] = mal_id
                    return mal_id
        except Exception:
            pass

    # Method 3: AniList search fallback for each candidate
    for cand in candidates:
        al_id = resolve_mal_id_from_anilist(cand)
        if al_id:
            RESOLVED_MAL_IDS[anime_id] = al_id
            return al_id

    return None

def get_user_status_sets():
    now = time.time()
    if "user_sets" in CACHE and (now - CACHE["user_sets"]["ts"]) < 300:
        c = CACHE["user_sets"]
        return c["favs"], c["watching"], c["completed"], c["all_by_clean"], c["all_by_id"]

    favs = []
    if os.path.exists(FAV_FILE):
        try:
            with open(FAV_FILE, "r") as f:
                favs = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        except Exception:
            pass

    watching = {}
    completed = {}
    all_by_clean = {}
    all_by_id = {}

    try:
        secrets = get_secrets()
        client_id = secrets.get("MAL_CLIENT_ID", "ea58d724802b654ee16451f5c377b6cf")
        username = secrets.get("MAL_USERNAME", "exodiaen")
        url = f"https://api.myanimelist.net/v2/users/{username}/animelist?limit=1000&fields=list_status,id,title"
        req = urllib.request.Request(url, headers={"X-MAL-CLIENT-ID": client_id})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            for item in data.get("data", []):
                node = item.get("node", {})
                st = item.get("list_status", {})
                mid = node.get("id")
                mtitle = node.get("title", "")
                status_val = st.get("status")
                score_val = st.get("score", 0)
                watched_eps = st.get("num_episodes_watched", 0)
                
                info = {
                    "id": mid,
                    "title": mtitle,
                    "status": status_val,
                    "score": score_val,
                    "watched": watched_eps
                }
                ct = clean_title(mtitle)
                all_by_clean[ct] = info
                if mid:
                    all_by_id[mid] = info
                if status_val == "completed":
                    completed[ct] = info
                elif status_val == "watching":
                    watching[ct] = info
    except Exception as e:
        print(f"[get_user_status_sets] Error fetching MAL user list: {e}")

    CACHE["user_sets"] = {
        "ts": now,
        "favs": favs,
        "watching": watching,
        "completed": completed,
        "all_by_clean": all_by_clean,
        "all_by_id": all_by_id
    }
    return favs, watching, completed, all_by_clean, all_by_id

def find_user_library_match(title, lookup_dict):
    if not title:
        return None
    ct = clean_title(title)
    if ct in lookup_dict:
        return lookup_dict[ct]
    if len(ct) >= 6:
        for k, v in lookup_dict.items():
            if len(k) >= 6:
                if k == ct:
                    return v
                if len(k) > 10 and len(ct) > 10 and (k in ct or ct in k):
                    return v
    return None

def tag_card_status(card, favs, watching, completed, all_by_id=None):
    title = card.get("title", "")
    ct = clean_title(title)
    mal_id = card.get("mal_id")

    matched_entry = None
    if mal_id and all_by_id and mal_id in all_by_id:
        matched_entry = all_by_id[mal_id]
    
    if not matched_entry:
        matched_entry = find_user_library_match(title, watching)
        if not matched_entry:
            matched_entry = find_user_library_match(title, completed)

    is_fav = any(clean_title(f) in ct or ct in clean_title(f) for f in favs) if ct else False
    card["is_favorite"] = is_fav

    if matched_entry:
        card["user_status"] = matched_entry["status"]
        card["user_score"] = matched_entry.get("score", 0)
        card["is_watching"] = (matched_entry["status"] == "watching")
        card["is_completed"] = (matched_entry["status"] == "completed")
        if "mal_id" not in card or not card["mal_id"]:
            card["mal_id"] = matched_entry["id"]
    else:
        card["is_watching"] = False
        card["is_completed"] = False
        card["user_status"] = None
        card["user_score"] = 0

    return card

def parse_anime_cards(page_html):
    cards = []
    items = page_html.split('<div class="flw-item')
    for item in items[1:]:
        img_m = re.search(r'class="film-poster-img"[^>]*src="([^"]+)"', item)
        if not img_m:
            img_m = re.search(r'src="([^"]+)"[^>]*class="film-poster-img"', item)
        title_m = re.search(r'class="film-name"[^>]*>.*?<a[^>]*title="([^"]+)"', item, re.DOTALL)
        id_m = re.search(r'<a href="[^"]*/watch/([^"?]+)', item)
        eps_m = re.search(r'class="tick-item tick-eps">([0-9?]+)<', item)
        sub_m = re.search(r'class="tick-item tick-sub">.*?([0-9?]+)<', item, re.DOTALL)
        dub_m = re.search(r'class="tick-item tick-dub">.*?([0-9?]+)<', item, re.DOTALL)
        type_m = re.search(r'class="fdi-item">([A-Za-z]+)<', item)

        if id_m and title_m:
            cards.append({
                "id": id_m.group(1),
                "title": html.unescape(title_m.group(1)),
                "poster": img_m.group(1) if img_m else "https://hianime.at/theme/images/no-image.jpg",
                "eps": eps_m.group(1) if eps_m else "?",
                "sub": sub_m.group(1) if sub_m else "-",
                "dub": dub_m.group(1) if dub_m else "-",
                "type": type_m.group(1) if type_m else "Anime",
                "score": "-"
            })
    return cards

def fetch_mal_score_and_synopsis(title):
    clean_k = title.lower().strip()
    cache_key = f"mal_info_{clean_k}"
    if cache_key in CACHE:
        return CACHE[cache_key]
    if clean_k in MAL_DESCRIPTIONS_CACHE:
        res = MAL_DESCRIPTIONS_CACHE[clean_k]
        CACHE[cache_key] = res
        return res

    secrets = get_secrets()
    client_id = secrets.get("MAL_CLIENT_ID", "ea58d724802b654ee16451f5c377b6cf")
    q = urllib.parse.quote(title)
    url = f"https://api.myanimelist.net/v2/anime?q={q}&limit=1&fields=mean,synopsis&nsfw=true"
    req = urllib.request.Request(url, headers={"X-MAL-CLIENT-ID": client_id})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
            entries = data.get("data", [])
            if entries:
                node = entries[0]["node"]
                score = node.get("mean")
                score_str = f"{score:.2f}" if score is not None else "-"
                synopsis = node.get("synopsis", "")
                res = {"score": score_str, "synopsis": synopsis}
                CACHE[cache_key] = res
                MAL_DESCRIPTIONS_CACHE[clean_k] = res
                save_mal_descriptions_cache()
                return res
    except Exception:
        pass
    res = {"score": "-", "synopsis": ""}
    CACHE[cache_key] = res
    return res

def get_latest_anime():
    cache_key = "latest_anime_enriched"
    if cache_key in CACHE and (time.time() - CACHE[cache_key]["ts"]) < 120:
        return CACHE[cache_key]["data"]

    raw = http_get(f"{BASE_URL}/recently-updated")
    cards = parse_anime_cards(raw)
    favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()

    def enrich_card(c):
        info = fetch_mal_score_and_synopsis(c["title"])
        c["score"] = info.get("score", "-")
        c["synopsis"] = info.get("synopsis", "")
        return tag_card_status(c, favs, watching, completed, all_by_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        enriched_cards = list(ex.map(enrich_card, cards))

    CACHE[cache_key] = {"ts": time.time(), "data": enriched_cards}
    return enriched_cards

def search_anime(query):
    encoded = urllib.parse.quote(query)
    raw = http_get(f"{BASE_URL}/search?keyword={encoded}")
    cards = parse_anime_cards(raw)
    favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()
    for c in cards:
        tag_card_status(c, favs, watching, completed, all_by_id)
    return cards

def resolve_title_to_anime(title):
    clean_k = title.lower().strip()
    cache_key = f"resolve_{clean_k}"
    if cache_key in CACHE:
        return CACHE[cache_key]["data"]
    if clean_k in RESOLVE_CACHE:
        res = RESOLVE_CACHE[clean_k]
        CACHE[cache_key] = {"ts": time.time(), "data": res}
        return res

    cards = search_anime(title)
    if cards:
        result = cards[0]
        CACHE[cache_key] = {"ts": time.time(), "data": result}
        RESOLVE_CACHE[clean_k] = result
        save_resolve_cache()
        return result
    return None

def get_anime_details_and_episodes(anime_id):
    num_id = anime_id.split("-")[-1]
    url = f"{BASE_URL}/api/theme/episode/list/{num_id}"
    raw = http_get(url)
    data = json.loads(raw)
    ep_html = data.get("html", "")

    episodes = []
    for match in re.finditer(r'<a[^>]*class="[^"]*ep-item[^"]*"[^>]*data-number="([^"]+)"[^>]*data-id="([^"]+)"[^>]*>', ep_html):
        num = match.group(1)
        internal_id = match.group(2)
        episodes.append({
            "number": num,
            "id": internal_id
        })
    
    # Resolve MAL ID and synopsis
    mal_id = resolve_mal_id_from_hianime(anime_id)
    mal_info = {}
    if mal_id:
        mal_data = get_mal_anime_status(mal_id=mal_id)
        if mal_data:
            mal_info = {
                "synopsis": mal_data.get("synopsis", ""),
                "score": str(mal_data.get("mean") or "-"),
                "poster": mal_data.get("poster", "")
            }
    
    if not mal_info.get("synopsis"):
        mal_info = fetch_mal_score_and_synopsis(anime_id.rsplit("-", 1)[0].replace("-", " "))
    synopsis = mal_info.get("synopsis", "")
    
    title = ""
    jp_title = ""
    poster = ""
    type_val = "Anime"
    try:
        info_html = http_get(f"{BASE_URL}/{anime_id}")
        title_m = re.search(r'<h2[^>]*class="[^"]*film-name[^"]*"[^>]*>(?:<a[^>]*>)?([^<]+)', info_html)
        if title_m:
            title = html.unescape(title_m.group(1).strip())
        jp_m = re.search(r'Japanese:</span>\s*<span class="name">([^<]+)</span>', info_html)
        if jp_m:
            jp_title = html.unescape(jp_m.group(1).strip())
        poster_m = re.search(r'class="film-poster-img"[^>]*src="([^"]+)"', info_html)
        if poster_m:
            poster = poster_m.group(1)
        type_m = re.search(r'>Type:</span>\s*<a[^>]*>([^<]+)</a>', info_html)
        if not type_m:
            type_m = re.search(r'>Type:</span>\s*<span[^>]*>([^<]+)</span>', info_html)
        if type_m:
            type_val = html.unescape(type_m.group(1).strip())
        overview_m = re.search(r'class="film-description text-justify">.*?<div class="text">([^<]+)</div>', info_html, re.DOTALL)
        genres_m = re.findall(r'<a href="/genre/[^"]+">([^<]+)</a>', info_html)
        score_m = re.search(r'MAL Score:</span>\s*<span[^>]*>([^<]+)</span>', info_html)
    except Exception:
        overview_m = None
        genres_m = []
        score_m = None

    if not title:
        title = anime_id.rsplit("-", 1)[0].replace("-", " ").title()

    final_overview = synopsis if synopsis else (html.unescape(overview_m.group(1).strip()) if overview_m else "No description available.")

    card = {
        "id": anime_id,
        "title": title,
        "jp": jp_title,
        "poster": poster or mal_info.get("poster") or "https://hianime.at/theme/images/no-image.jpg",
        "type": type_val,
        "mal_id": mal_id,
        "episodes": episodes,
        "overview": final_overview,
        "genres": [html.unescape(g) for g in genres_m],
        "score": score_m.group(1) if score_m else mal_info.get("score", "N/A"),
        "mal_map_url": get_mal_map_url(mal_id, title, jp_title)
    }

    airing = get_anime_airing_info(title, [jp_title] if jp_title else None)
    if airing:
        card["next_ep"] = airing.get("next_ep")
        card["airing_at"] = airing.get("airing_at")
        card["countdown"] = airing.get("countdown")
        card["airing_status"] = airing.get("status")
        card["time_until"] = airing.get("time_until")

    favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()
    card = tag_card_status(card, favs, watching, completed, all_by_id)
    return card

def fetch_mal_list(status):
    cache_key = f"mal_{status}"
    if cache_key in CACHE and (time.time() - CACHE[cache_key]["ts"]) < 180:
        return CACHE[cache_key]["data"]

    token = get_mal_access_token()
    secrets = get_secrets()
    username = secrets.get("MAL_USERNAME", "exodiaen")
    
    url = f"https://api.myanimelist.net/v2/users/{username}/animelist?status={status}&limit=100&fields=list_status,num_episodes,genres,mean,main_picture,synopsis"
    raw = http_get(url, {"Authorization": f"Bearer {token}"})
    data = json.loads(raw)

    favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()
    items = []
    for entry in data.get("data", []):
        node = entry.get("node", {})
        title = node.get("title", "")
        if not title:
            continue
        mal_id = node.get("id")
        num_eps = node.get("num_episodes") or "?"
        list_status = entry.get("list_status", {})
        watched = list_status.get("num_episodes_watched", 0)
        score = list_status.get("score") or node.get("mean") or "-"
        user_score = list_status.get("score", 0)
        current_status = list_status.get("status", status)
        genres_list = [g.get("name", "") for g in node.get("genres", []) if g.get("name")]
        genres = ", ".join(genres_list) if genres_list else "Unknown"
        poster = node.get("main_picture", {}).get("large") or node.get("main_picture", {}).get("medium") or "https://hianime.at/theme/images/no-image.jpg"
        synopsis = node.get("synopsis", "")

        ct = clean_title(title)
        is_fav = any(clean_title(f) in ct or ct in clean_title(f) for f in favs) if ct else False

        items.append({
            "mal_id": mal_id,
            "title": title,
            "poster": poster,
            "eps": f"{watched}/{num_eps}",
            "score": str(score),
            "user_score": user_score,
            "status": current_status,
            "genres": genres,
            "synopsis": synopsis,
            "watched_ep": watched,
            "total_eps": num_eps,
            "type": "MAL",
            "is_favorite": is_fav,
            "is_watching": True if status == "watching" else False,
            "is_completed": True if status == "completed" else False
        })

    CACHE[cache_key] = {"ts": time.time(), "data": items}
    return items

def get_mal_anime_status(title=None, mal_id=None, anime_id=None):
    token = get_mal_access_token()
    
    # 1. Resolve mal_id if missing but anime_id provided
    if not mal_id and anime_id:
        mal_id = resolve_mal_id_from_hianime(anime_id)
        
    # 2. Check user's own library first if title provided
    if not mal_id and title:
        favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()
        user_match = find_user_library_match(title, all_by_clean)
        if user_match:
            mal_id = user_match["id"]

    # 3. If mal_id is known (or was resolved)
    if mal_id:
        try:
            url = f"https://api.myanimelist.net/v2/anime/{mal_id}?fields=id,title,my_list_status,num_episodes,synopsis,mean"
            raw = http_get(url, {"Authorization": f"Bearer {token}"})
            data = json.loads(raw)
            if "id" in data:
                res_obj = {
                    "mal_id": data.get("id"),
                    "title": data.get("title"),
                    "num_episodes": data.get("num_episodes", "?"),
                    "synopsis": data.get("synopsis", ""),
                    "mean": data.get("mean"),
                    "my_list_status": data.get("my_list_status")
                }
                if data.get("synopsis"):
                    MAL_DESCRIPTIONS_CACHE[f"mal_id_{data['id']}"] = {
                        "synopsis": data.get("synopsis"),
                        "score": str(data.get("mean") or "-")
                    }
                    if data.get("title"):
                        MAL_DESCRIPTIONS_CACHE[clean_title(data["title"])] = {
                            "synopsis": data.get("synopsis"),
                            "score": str(data.get("mean") or "-")
                        }
                    save_mal_descriptions_cache()
                return res_obj
        except Exception:
            pass

    # 4. Search MAL by title as fallback (with sanitized title queries)
    if title:
        candidates = [title]
        cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", title).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned and cleaned != title:
            candidates.append(cleaned)
        words = cleaned.split()
        if len(words) > 3:
            candidates.append(" ".join(words[:4]))

        for cand in candidates:
            try:
                q = urllib.parse.quote(cand)
                url = f"https://api.myanimelist.net/v2/anime?q={q}&limit=5&fields=id,title,my_list_status,num_episodes,synopsis,mean&nsfw=true"
                raw = http_get(url, {"Authorization": f"Bearer {token}"})
                data = json.loads(raw)
                entries = data.get("data", [])
                if entries:
                    resolved_id = entries[0].get("node", {}).get("id")
                    if resolved_id:
                        det_url = f"https://api.myanimelist.net/v2/anime/{resolved_id}?fields=id,title,my_list_status,num_episodes,synopsis,mean"
                        det_raw = http_get(det_url, {"Authorization": f"Bearer {token}"})
                        det_data = json.loads(det_raw)
                        return {
                            "mal_id": det_data.get("id", resolved_id),
                            "title": det_data.get("title"),
                            "num_episodes": det_data.get("num_episodes", "?"),
                            "synopsis": det_data.get("synopsis", ""),
                            "mean": det_data.get("mean"),
                            "my_list_status": det_data.get("my_list_status")
                        }
            except Exception:
                continue

        # 5. AniList search fallback for mal_id
        for cand in candidates:
            al_mal_id = resolve_mal_id_from_anilist(cand)
            if al_mal_id:
                try:
                    det_url = f"https://api.myanimelist.net/v2/anime/{al_mal_id}?fields=id,title,my_list_status,num_episodes,synopsis,mean"
                    det_raw = http_get(det_url, {"Authorization": f"Bearer {token}"})
                    det_data = json.loads(det_raw)
                    return {
                        "mal_id": det_data.get("id", al_mal_id),
                        "title": det_data.get("title"),
                        "num_episodes": det_data.get("num_episodes", "?"),
                        "synopsis": det_data.get("synopsis", ""),
                        "mean": det_data.get("mean"),
                        "my_list_status": det_data.get("my_list_status")
                    }
                except Exception:
                    pass

    return None

def update_mal_anime_status(mal_id, status, score=None, num_watched_episodes=None):
    token = get_mal_access_token()
    fields = {"status": status}
    if score is not None:
        fields["score"] = int(score)
    if num_watched_episodes is not None:
        fields["num_watched_episodes"] = int(num_watched_episodes)
        
    post_data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.myanimelist.net/v2/anime/{mal_id}/my_list_status",
        data=post_data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        method="PUT"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())
        CACHE.pop("mal_watching", None)
        CACHE.pop("mal_plan_to_watch", None)
        CACHE.pop("user_sets", None)
        return result

def get_combined_recommendations(mal_id=None, anime_id=None, title=None, limit=30):
    if mal_id:
        try:
            mal_id = int(mal_id)
        except Exception:
            pass

    # 1. Resolve mal_id if missing
    if not mal_id and anime_id:
        mal_id = resolve_mal_id_from_hianime(anime_id)
        
    if not mal_id and title:
        favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()
        user_match = find_user_library_match(title, all_by_clean)
        if user_match:
            mal_id = user_match["id"]

    cache_key = f"recs_{mal_id}_{anime_id}_{title}_{limit}"
    if cache_key in CACHE and (time.time() - CACHE[cache_key]["ts"] < 600):
        return CACHE[cache_key]["data"]

    mal_recs = []
    anilist_recs = []

    def fetch_mal():
        nonlocal mal_recs
        if not mal_id:
            return
        try:
            token = get_mal_access_token()
            url = f"https://api.myanimelist.net/v2/anime/{mal_id}?fields=recommendations{{num_recommendations}}"
            raw = http_get(url, {"Authorization": f"Bearer {token}"})
            data = json.loads(raw)
            for r in data.get("recommendations", []):
                node = r.get("node", {})
                if node and node.get("id"):
                    m_id = node.get("id")
                    t = node.get("title")
                    mal_recs.append({
                        "mal_id": m_id,
                        "title": t,
                        "poster": node.get("main_picture", {}).get("large") or node.get("main_picture", {}).get("medium") or "",
                        "votes": r.get("num_recommendations", 0),
                        "source": "MAL",
                        "source_label": "MAL",
                        "mal_map_url": get_mal_map_url(m_id, t)
                    })
        except Exception as e:
            print(f"[get_combined_recommendations] MAL recs error: {e}")

        # Supplement with MAL-Map metadata recommendations to show 25-30+ shows
        if mal_id and str(mal_id) in MAL_MAP_METADATA:
            existing_ids = {r["mal_id"] for r in mal_recs if r.get("mal_id")}
            mm_recs = MAL_MAP_METADATA[str(mal_id)].get("recommendations", [])
            mm_sorted = sorted(mm_recs, key=lambda x: x.get("count", 0), reverse=True)
            for item in mm_sorted:
                rid = item.get("id")
                if rid and rid not in existing_ids:
                    s_rid = str(rid)
                    if s_rid in MAL_MAP_METADATA:
                        meta_node = MAL_MAP_METADATA[s_rid]
                        t = meta_node.get("englishTitle") or meta_node.get("title")
                        mal_recs.append({
                            "mal_id": rid,
                            "title": t,
                            "poster": meta_node.get("picture", ""),
                            "score": str(meta_node.get("score") or "-"),
                            "votes": item.get("count", 0),
                            "source": "MAL",
                            "source_label": "MAL",
                            "mal_map_url": get_mal_map_url(rid, meta_node.get("title"))
                        })
                        existing_ids.add(rid)

    def fetch_anilist():
        nonlocal anilist_recs
        try:
            query = """
            query ($idMal: Int, $search: String) {
              Media (idMal: $idMal, search: $search, type: ANIME) {
                recommendations (perPage: 35, sort: RATING_DESC) {
                  nodes {
                    rating
                    mediaRecommendation {
                      id
                      idMal
                      title { romaji english }
                      coverImage { large medium }
                      averageScore
                      episodes
                      format
                    }
                  }
                }
              }
            }
            """
            variables = {}
            if mal_id:
                variables["idMal"] = mal_id
            elif title:
                variables["search"] = title
            else:
                return

            req = urllib.request.Request(
                "https://graphql.anilist.co",
                data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                al_data = json.loads(resp.read().decode())
                nodes = al_data.get("data", {}).get("Media", {}).get("recommendations", {}).get("nodes", [])
                for n in nodes:
                    mr = n.get("mediaRecommendation")
                    if mr and (mr.get("idMal") or mr.get("title")):
                        t = mr.get("title", {})
                        chosen_title = t.get("english") or t.get("romaji") or ""
                        score_val = str(round(mr.get("averageScore", 0) / 10, 1)) if mr.get("averageScore") else "-"
                        m_id = mr.get("idMal")
                        anilist_recs.append({
                            "mal_id": m_id,
                            "anilist_id": mr.get("id"),
                            "title": chosen_title,
                            "alt_title": t.get("romaji") if chosen_title != t.get("romaji") else t.get("english"),
                            "poster": mr.get("coverImage", {}).get("large") or mr.get("coverImage", {}).get("medium") or "",
                            "score": score_val,
                            "eps": str(mr.get("episodes") or "?"),
                            "votes": n.get("rating", 0),
                            "source": "AniList",
                            "source_label": "AniList",
                            "mal_map_url": get_mal_map_url(m_id, chosen_title, t.get("romaji"))
                        })
        except Exception as e:
            print(f"[get_combined_recommendations] AniList recs error: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(fetch_mal)
        f2 = executor.submit(fetch_anilist)
        f1.result()
        f2.result()

    favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()

    for r in mal_recs:
        tag_card_status(r, favs, watching, completed, all_by_id)

    for r in anilist_recs:
        tag_card_status(r, favs, watching, completed, all_by_id)

    merged_map = {}
    for r in mal_recs:
        key = f"mal_{r['mal_id']}" if r.get("mal_id") else clean_title(r["title"])
        merged_map[key] = {
            "mal_id": r.get("mal_id"),
            "anilist_id": None,
            "title": r["title"],
            "alt_title": "",
            "poster": r["poster"],
            "score": "-",
            "eps": "?",
            "sources": ["MAL"],
            "votes": r.get("votes", 0),
            "source_label": "MAL",
            "mal_map_url": r.get("mal_map_url")
        }

    for r in anilist_recs:
        key = None
        if r.get("mal_id"):
            cand_key = f"mal_{r['mal_id']}"
            if cand_key in merged_map:
                key = cand_key
        if not key:
            ct = clean_title(r["title"])
            for existing_key, item in merged_map.items():
                if clean_title(item["title"]) == ct or (r.get("alt_title") and clean_title(r["alt_title"]) == clean_title(item["title"])):
                    key = existing_key
                    break

        if key and key in merged_map:
            item = merged_map[key]
            if "AniList" not in item["sources"]:
                item["sources"].append("AniList")
            item["source_label"] = "MAL & AniList"
            item["anilist_id"] = r.get("anilist_id")
            if r.get("score") and r["score"] != "-":
                item["score"] = r["score"]
            if r.get("eps") and r["eps"] != "?":
                item["eps"] = r["eps"]
            if not item.get("poster") and r.get("poster"):
                item["poster"] = r["poster"]
            item["votes"] += r.get("votes", 0)
        else:
            new_key = f"mal_{r['mal_id']}" if r.get("mal_id") else f"al_{r.get('anilist_id') or clean_title(r['title'])}"
            merged_map[new_key] = {
                "mal_id": r.get("mal_id"),
                "anilist_id": r.get("anilist_id"),
                "title": r["title"],
                "alt_title": r.get("alt_title", ""),
                "poster": r["poster"],
                "score": r.get("score", "-"),
                "eps": r.get("eps", "?"),
                "sources": ["AniList"],
                "votes": r.get("votes", 0),
                "source_label": "AniList",
                "mal_map_url": r.get("mal_map_url")
            }

    def sort_score(item):
        is_both = 1 if len(item["sources"]) > 1 else 0
        return (is_both, item.get("votes", 0))

    sorted_recs = sorted(merged_map.values(), key=sort_score, reverse=True)
    final_recs = sorted_recs[:limit]

    for rec in final_recs:
        tag_card_status(rec, favs, watching, completed, all_by_id)

    response_data = {
        "mal": mal_recs[:limit],
        "anilist": anilist_recs[:limit],
        "results": final_recs
    }

    CACHE[cache_key] = {"ts": time.time(), "data": response_data}
    return response_data

def get_favorites_list():
    if not os.path.exists(FAV_FILE):
        return []

    # 1. Check persistent disk cache
    now_ts = time.time()
    if os.path.exists(FAV_LIST_CACHE_FILE):
        try:
            fav_mtime = os.path.getmtime(FAV_FILE)
            cache_mtime = os.path.getmtime(FAV_LIST_CACHE_FILE)
            if cache_mtime > fav_mtime and (now_ts - cache_mtime) < 600:
                with open(FAV_LIST_CACHE_FILE, "r") as f:
                    cached_results = json.load(f)
                    if cached_results and isinstance(cached_results, list):
                        return cached_results
        except Exception:
            pass

    fav_titles = []
    with open(FAV_FILE, "r") as f:
        fav_titles = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not fav_titles:
        return []

    favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()

    def resolve_fav_item(title):
        card = resolve_title_to_anime(title)
        alts = [card.get("jp")] if card and card.get("jp") else []
        airing = get_anime_airing_info(title, alts)
        if card:
            c = {
                "id": card["id"],
                "title": title,
                "poster": card.get("poster") or "https://hianime.at/theme/images/no-image.jpg",
                "eps": card.get("eps", "?"),
                "sub": card.get("sub", "-"),
                "dub": card.get("dub", "-"),
                "type": "Favorite",
                "score": card.get("score", "-")
            }
            if airing:
                c["next_ep"] = airing.get("next_ep")
                c["airing_at"] = airing.get("airing_at")
                c["countdown"] = airing.get("countdown")
                c["airing_status"] = airing.get("status")
                tu = airing.get("time_until")
                c["time_until"] = tu if (tu is not None and isinstance(tu, (int, float))) else 888888888
            else:
                c["time_until"] = 999999999
            return tag_card_status(c, favs, watching, completed, all_by_id)
        else:
            c = {
                "id": "",
                "title": title,
                "poster": "https://hianime.at/theme/images/logo.png",
                "eps": "?",
                "sub": "-",
                "dub": "-",
                "type": "Favorite",
                "is_favorite": True,
                "is_watching": False,
                "is_completed": False,
                "score": "-"
            }
            if airing:
                c["next_ep"] = airing.get("next_ep")
                c["airing_at"] = airing.get("airing_at")
                c["countdown"] = airing.get("countdown")
                c["airing_status"] = airing.get("status")
                tu = airing.get("time_until")
                c["time_until"] = tu if (tu is not None and isinstance(tu, (int, float))) else 888888888
            else:
                c["time_until"] = 999999999
            return c

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(resolve_fav_item, fav_titles))

    def sort_fav(item):
        val = item.get("time_until")
        if val is None or not isinstance(val, (int, float)):
            return 888888888
        if val <= 0:
            return 0
        return val

    results.sort(key=sort_fav)

    # Save to disk cache
    try:
        with open(FAV_LIST_CACHE_FILE, "w") as f:
            json.dump(results, f, indent=2)
    except Exception:
        pass

    return results

def toggle_favorite(title):
    os.makedirs(STATE_DIR, exist_ok=True)
    favs = []
    if os.path.exists(FAV_FILE):
        with open(FAV_FILE, "r") as f:
            favs = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    
    norm_favs = [f.lower() for f in favs]
    is_now_fav = False
    if title.lower() in norm_favs:
        favs = [f for f in favs if f.lower() != title.lower()]
        is_now_fav = False
    else:
        favs.append(title)
        is_now_fav = True
        
    with open(FAV_FILE, "w") as fp:
        for item in favs:
            fp.write(f"{item}\n")

    # Invalidate cached favorites list and user sets
    if os.path.exists(FAV_LIST_CACHE_FILE):
        try:
            os.remove(FAV_LIST_CACHE_FILE)
        except Exception:
            pass
    CACHE.pop("user_sets", None)

    return is_now_fav

def resolve_schedule_poster_and_id(item):
    title = item.get("title", "")
    card = resolve_title_to_anime(title)
    if card:
        item["poster"] = card.get("poster")
        item["id"] = card.get("id")
    else:
        item["poster"] = "https://hianime.at/theme/images/no-image.jpg"
        item["id"] = ""
    return item

def get_schedule(selected_time=None):
    today = datetime.date.today()
    days = []
    for i in range(7):
        d = today + datetime.timedelta(days=i)
        ts = int(datetime.datetime(d.year, d.month, d.day, 0, 0, 0).timestamp())
        days.append({
            "time": str(ts),
            "date": d.strftime("%b %d"),
            "wday": d.strftime("%a"),
            "is_today": i == 0
        })

    target_time = selected_time if selected_time else days[0]["time"]
    cache_key = f"sched_{target_time}"
    if cache_key in CACHE and (time.time() - CACHE[cache_key]["ts"]) < 300:
        return {"days": days, "items": CACHE[cache_key]["items"], "selected_time": str(target_time)}

    url = f"https://anikoto.cz/ajax/schedule/date?tz=-360&time={target_time}"
    raw = http_get(url, {"X-Requested-With": "XMLHttpRequest"})
    data = json.loads(raw)
    raw_html = data.get("result", "")

    raw_items = []
    for item_m in re.finditer(r'<a class="item[^"]*"[^>]*href="([^"]+)">\s*<div class="time"[^>]*>([^<]+)</div>\s*<div class="ep">.*?<span>([^<]+)</span>\s*</div>\s*<div class="title[^"]*"[^>]*data-jp="([^"]*)">([^<]+)</div>', raw_html, re.DOTALL):
        raw_items.append({
            "url": item_m.group(1),
            "time": item_m.group(2).strip(),
            "ep": item_m.group(3).strip(),
            "jp": html.unescape(item_m.group(4).strip()),
            "title": html.unescape(item_m.group(5).strip())
        })

    favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        enriched_items = list(ex.map(resolve_schedule_poster_and_id, raw_items))

    for it in enriched_items:
        tag_card_status(it, favs, watching, completed, all_by_id)

    CACHE[cache_key] = {"ts": time.time(), "items": enriched_items}
    return {"days": days, "items": enriched_items, "selected_time": str(target_time)}

def resolve_stream(anime_id, episode_number, mode="sub"):
    num_id = anime_id.split("-")[-1]
    list_url = f"{BASE_URL}/api/theme/episode/list/{num_id}"
    raw = http_get(list_url)
    data = json.loads(raw)
    ep_html = data.get("html", "")
    
    internal_ep_id = None
    for match in re.finditer(r'<a[^>]*class="[^"]*ep-item[^"]*"[^>]*data-number="([^"]+)"[^>]*data-id="([^"]+)"', ep_html):
        if str(match.group(1)) == str(episode_number):
            internal_ep_id = match.group(2)
            break
            
    if not internal_ep_id:
        raise ValueError(f"Could not find internal episode ID for episode {episode_number}")

    servers_url = f"{BASE_URL}/api/theme/episode/servers?episodeId={internal_ep_id}"
    servers_raw = http_get(servers_url)
    servers_data = json.loads(servers_raw)
    servers_html = servers_data.get("html", "")

    hash_pattern = rf'data-type="{mode}"[^>]*data-server-name="ZokoAnime"[^>]*data-hash="([^"]+)"'
    hash_m = re.search(hash_pattern, servers_html)
    if not hash_m:
        hash_m = re.search(r'data-server-name="ZokoAnime"[^>]*data-hash="([^"]+)"', servers_html)
    
    if not hash_m:
        raise ValueError("ZokoAnime server not found for this episode.")

    embed_url = base64.b64decode(hash_m.group(1)).decode("utf-8")
    embed_page = http_get(embed_url)

    blob_m = re.search(r'window\.__P\s*=\s*"([^"]+)"', embed_page)
    if not blob_m:
        raise ValueError("Could not find stream payload blob.")

    raw_bytes = base64.b64decode(blob_m.group(1))
    key = b"otaku-embed-v1"
    decrypted = bytes([b ^ key[i % len(key)] for i, b in enumerate(raw_bytes)])
    stream_data = json.loads(decrypted.decode("utf-8", errors="ignore"))

    m3u8_url = stream_data.get("src")
    if not m3u8_url:
        raise ValueError("Stream playlist URL not present in decrypted payload.")

    return {
        "m3u8_url": m3u8_url,
        "embed_url": embed_url,
        "subtitles": stream_data.get("subtitles", [])
    }

def update_history_file(anime_id, anime_title, episode_number):
    try:
        os.makedirs(os.path.dirname(HIST_FILE), exist_ok=True)
        lines = []
        if os.path.exists(HIST_FILE):
            with open(HIST_FILE, "r") as f:
                lines = [line.strip() for line in f if line.strip() and anime_id not in line]
        lines.insert(0, f"{episode_number}\t{anime_id}\t{anime_title}")
        with open(HIST_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass

def play_in_mpv(anime_id, anime_title, episode_number, mode="sub"):
    resolved = resolve_stream(anime_id, episode_number, mode)
    m3u8_url = resolved["m3u8_url"]
    embed_url = resolved["embed_url"]
    subtitles = resolved["subtitles"]

    sub_file = None
    for s in subtitles:
        if s.get("default"):
            sub_file = s.get("src")
            break
    if not sub_file and subtitles:
        sub_file = subtitles[0].get("src")

    mpv_cmd = [
        "/opt/homebrew/bin/mpv",
        f"--referrer={embed_url}",
        f"--force-media-title={anime_title} Episode {episode_number}",
        "--geometry=75%",
        "--autofit=1280x720",
    ]
    if sub_file and mode == "sub":
        mpv_cmd.append(f"--sub-file={sub_file}")
    mpv_cmd.append(m3u8_url)

    subprocess.Popen(mpv_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    update_history_file(anime_id, anime_title, episode_number)
    return {"status": "ok", "stream": m3u8_url, "player": "mpv"}

def proxy_m3u8_content(url, ref):
    effective_ref = ref if ref else "https://zokoanime.video/"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Referer": effective_ref}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        content = resp.read().decode("utf-8", errors="ignore")

    lines = content.splitlines()
    rewritten = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if 'URI="' in stripped:
                def rewrite_uri(match):
                    sub_uri = match.group(1)
                    abs_sub = urllib.parse.urljoin(url, sub_uri)
                    if abs_sub.endswith(".m3u8") or ".m3u8?" in abs_sub:
                        new_uri = f"/api/m3u8?url={urllib.parse.quote(abs_sub)}&ref={urllib.parse.quote(effective_ref)}"
                    else:
                        new_uri = f"/api/proxy?url={urllib.parse.quote(abs_sub)}&ref={urllib.parse.quote(effective_ref)}"
                    return f'URI="{new_uri}"'
                rewritten.append(re.sub(r'URI="([^"]+)"', rewrite_uri, stripped))
            else:
                rewritten.append(stripped)
        else:
            abs_url = urllib.parse.urljoin(url, stripped)
            if abs_url.endswith(".m3u8") or ".m3u8?" in abs_url:
                rewritten.append(f"/api/m3u8?url={urllib.parse.quote(abs_url)}&ref={urllib.parse.quote(effective_ref)}")
            else:
                rewritten.append(f"/api/proxy?url={urllib.parse.quote(abs_url)}&ref={urllib.parse.quote(effective_ref)}")
    return "\n".join(rewritten)

class RequestHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        VALID_TABS = {"latest", "schedule", "favorites", "mal_watching", "mal_ptw", "history"}
        clean_p = path.strip("/")
        if path == "/" or path.startswith("/watch") or clean_p in VALID_TABS:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_path = Path(__file__).parent / "index.html"
            with open(html_path, "rb") as f:
                self.wfile.write(f.read())
            return

        if path == "/api/thumb":
            target_url = params.get("url", [""])[0]
            if not target_url:
                self.send_response(400)
                self.end_headers()
                return

            url_hash = hashlib.sha256(target_url.encode("utf-8")).hexdigest()[:24]
            parsed_url = urllib.parse.urlparse(target_url)
            ext = os.path.splitext(parsed_url.path)[1].lower()
            if ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"]:
                ext = ".jpg"
            cache_file = os.path.join(THUMB_CACHE_DIR, f"{url_hash}{ext}")

            inm = self.headers.get("If-None-Match")
            etag = f'"{url_hash}"'
            if inm == etag and os.path.exists(cache_file):
                self.send_response(304)
                self.end_headers()
                return

            content_type = "image/jpeg"
            if ext == ".png": content_type = "image/png"
            elif ext == ".webp": content_type = "image/webp"
            elif ext == ".gif": content_type = "image/gif"
            elif ext == ".avif": content_type = "image/avif"

            if os.path.exists(cache_file):
                try:
                    file_size = os.path.getsize(cache_file)
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(file_size))
                    self.send_header("Cache-Control", "public, max-age=2592000, immutable")
                    self.send_header("ETag", etag)
                    self.end_headers()
                    with open(cache_file, "rb") as f:
                        shutil.copyfileobj(f, self.wfile, 65536)
                    return
                except Exception:
                    pass

            try:
                req = urllib.request.Request(
                    target_url,
                    headers={"User-Agent": USER_AGENT, "Referer": BASE_URL}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = resp.read()
                    ct = resp.headers.get("Content-Type", content_type)
                    try:
                        with open(cache_file, "wb") as f:
                            f.write(data)
                    except Exception:
                        pass
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=2592000, immutable")
                    self.send_header("ETag", etag)
                    self.end_headers()
                    self.wfile.write(data)
            except Exception:
                self.send_response(302)
                self.send_header("Location", target_url)
                self.end_headers()
            return

        if path == "/api/m3u8":
            target_url = params.get("url", [""])[0]
            ref = params.get("ref", [""])[0]
            if not target_url:
                self.send_response(400)
                self.end_headers()
                return
            try:
                rewritten_m3u8 = proxy_m3u8_content(target_url, ref)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(rewritten_m3u8.encode("utf-8"))
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/proxy":
            target_url = params.get("url", [""])[0]
            ref = params.get("ref", ["https://zokoanime.video/"])[0]
            if not target_url:
                self.send_response(400)
                self.end_headers()
                return

            req_headers = {
                "User-Agent": USER_AGENT,
                "Referer": ref if ref else "https://zokoanime.video/"
            }
            range_header = self.headers.get("Range")
            if range_header:
                req_headers["Range"] = range_header

            try:
                upstream_req = urllib.request.Request(target_url, headers=req_headers)
                with urllib.request.urlopen(upstream_req, timeout=15) as upstream_resp:
                    code = upstream_resp.status
                    self.send_response(code)

                    ct = upstream_resp.headers.get("Content-Type")
                    if ct:
                        self.send_header("Content-Type", ct)
                    elif ".ts" in target_url:
                        self.send_header("Content-Type", "video/mp2t")
                    elif ".vtt" in target_url:
                        self.send_header("Content-Type", "text/vtt")

                    cl = upstream_resp.headers.get("Content-Length")
                    if cl:
                        self.send_header("Content-Length", cl)

                    cr = upstream_resp.headers.get("Content-Range")
                    if cr:
                        self.send_header("Content-Range", cr)

                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()

                    shutil.copyfileobj(upstream_resp, self.wfile, 65536)
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.end_headers()
            except Exception:
                pass
            return

        if path == "/api/stream/resolve":
            anime_id = params.get("id", [""])[0]
            anime_title = params.get("title", [anime_id])[0]
            episode = params.get("episode", ["1"])[0]
            mode = params.get("mode", ["sub"])[0]
            try:
                resolved = resolve_stream(anime_id, episode, mode)
                embed_ref = resolved.get("embed_url", "https://zokoanime.video/")
                proxy_url = f"/api/m3u8?url={urllib.parse.quote(resolved['m3u8_url'])}&ref={urllib.parse.quote(embed_ref)}"
                update_history_file(anime_id, anime_title, episode)

                proxied_subs = []
                for sub in resolved.get("subtitles", []):
                    src_url = sub.get("src", "")
                    proxied_subs.append({
                        "lang": sub.get("lang"),
                        "label": sub.get("label"),
                        "default": sub.get("default"),
                        "src": f"/api/proxy?url={urllib.parse.quote(src_url)}&ref={urllib.parse.quote(embed_ref)}" if src_url else ""
                    })

                self._send_json({
                    "status": "ok",
                    "stream_proxy": proxy_url,
                    "subtitles": proxied_subs
                })
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/latest":
            try:
                cards = get_latest_anime()
                self._send_json({"status": "ok", "results": cards})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/search":
            query = params.get("q", [""])[0]
            try:
                cards = search_anime(query) if query else []
                self._send_json({"status": "ok", "results": cards})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/resolve":
            title = params.get("title", [""])[0]
            try:
                card = resolve_title_to_anime(title)
                self._send_json({"status": "ok", "result": card})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/details":
            anime_id = params.get("id", [""])[0]
            try:
                info = get_anime_details_and_episodes(anime_id)
                self._send_json({"status": "ok", "data": info})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/mal":
            status = params.get("status", ["watching"])[0]
            try:
                items = fetch_mal_list(status)
                self._send_json({"status": "ok", "results": items})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/mal/status":
            title = params.get("title", [None])[0]
            mal_id = params.get("mal_id", [None])[0]
            anime_id = params.get("anime_id", [None])[0]
            try:
                data = get_mal_anime_status(title=title, mal_id=mal_id, anime_id=anime_id)
                self._send_json({"status": "ok", "data": data})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/recommendations":
            title = params.get("title", [None])[0]
            mal_id = params.get("mal_id", [None])[0]
            anime_id = params.get("anime_id", [None])[0]
            limit_str = params.get("limit", ["30"])[0]
            try:
                limit = int(limit_str)
            except Exception:
                limit = 30
            try:
                data = get_combined_recommendations(mal_id=mal_id, anime_id=anime_id, title=title, limit=limit)
                self._send_json({
                    "status": "ok",
                    "mal": data.get("mal", []),
                    "anilist": data.get("anilist", []),
                    "results": data.get("results", [])
                })
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/favorites":
            try:
                favs = get_favorites_list()
                self._send_json({"status": "ok", "results": favs})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/airing":
            title = params.get("title", [None])[0]
            if title:
                data = get_anime_airing_info(title)
                self._send_json({"status": "ok", "data": data})
            else:
                self._send_json({"status": "error", "message": "Missing title"}, 400)
            return

        if path == "/api/favorite":
            title = params.get("title", [None])[0]
            favs, watching, completed, all_by_clean, all_by_id = get_user_status_sets()
            ct = clean_title(title) if title else ""
            is_fav = any(clean_title(f) in ct or ct in clean_title(f) for f in favs) if ct else False
            self._send_json({"status": "ok", "is_favorite": is_fav})
            return

        if path == "/api/favorite/toggle":
            title = params.get("title", [None])[0]
            if title:
                is_fav = toggle_favorite(title)
                self._send_json({"status": "ok", "is_favorite": is_fav})
            else:
                self._send_json({"status": "error", "message": "Missing title"}, 400)
            return

        if path == "/api/schedule":
            t = params.get("time", [None])[0]
            try:
                data = get_schedule(t)
                self._send_json({"status": "ok", "data": data})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/history":
            try:
                hist = get_history()
                self._send_json({"status": "ok", "results": hist})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if path == "/api/shutdown":
            self._send_json({"status": "ok", "message": "Server shutting down..."})
            def _shutdown():
                time.sleep(0.5)
                os._exit(0)
            threading.Thread(target=_shutdown, daemon=True).start()
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        if self.path == "/api/shutdown":
            self._send_json({"status": "ok", "message": "Server shutting down..."})
            def _shutdown():
                time.sleep(0.5)
                os._exit(0)
            threading.Thread(target=_shutdown, daemon=True).start()
            return

        if self.path == "/api/play":
            try:
                payload = json.loads(body.decode("utf-8"))
                anime_id = payload.get("id")
                anime_title = payload.get("title", anime_id)
                episode_num = payload.get("episode", "1")
                mode = payload.get("mode", "sub")

                if not anime_id and anime_title:
                    resolved = resolve_title_to_anime(anime_title)
                    if resolved:
                        anime_id = resolved["id"]
                    else:
                        raise ValueError(f"Could not find anime matching '{anime_title}' on HiAnime")

                result = play_in_mpv(anime_id, anime_title, episode_num, mode)
                self._send_json(result)
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if self.path == "/api/favorite/toggle":
            try:
                payload = json.loads(body.decode("utf-8"))
                title = payload.get("title")
                is_fav = toggle_favorite(title)
                self._send_json({"status": "ok", "is_favorite": is_fav})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        if self.path == "/api/mal/update":
            try:
                payload = json.loads(body.decode("utf-8"))
                mal_id = payload.get("mal_id")
                anime_id = payload.get("anime_id")
                title = payload.get("title")
                status = payload.get("status")
                score = payload.get("score")
                num_watched = payload.get("num_watched_episodes")

                if not mal_id:
                    if anime_id:
                        mal_id = resolve_mal_id_from_hianime(anime_id)
                    if not mal_id and title:
                        mal_id = resolve_mal_id_from_anilist(title)
                if not mal_id:
                    raise ValueError("mal_id could not be resolved for this anime")

                result = update_mal_anime_status(mal_id, status, score, num_watched)
                self._send_json({"status": "ok", "result": result, "mal_id": mal_id})
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, 500)
            return

        self.send_response(404)
        self.end_headers()

    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

def main():
    print(f"Starting ani-ui server on http://localhost:{PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), RequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")

if __name__ == "__main__":
    main()
