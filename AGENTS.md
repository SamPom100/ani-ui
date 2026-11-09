# AGENTS.md

Operational manual, architectural invariants, and engineering lessons for autonomous agents working in `ani-ui`.

---

## 1. Project Architecture & Philosophy

Ani UI is a zero-dependency, local anime streaming portal and catalog viewer. It combines a HiAnime dark interface with local `ani-cli` history, MyAnimeList synchronization, AniList GraphQL metadata/countdowns, and MAL-Map franchise relationship graphs.

* **Repository:** `https://github.com/SamPom100/ani-ui`
* **Default Listening Port:** `http://localhost:5050`
* **Zero External Backend Dependencies:** `server.py` relies strictly on the Python 3.10+ standard library (`http.server`, `urllib.request`, `urllib.parse`, `json`, `re`, `base64`, `concurrent.futures`, `shutil`, `pathlib`). Do not introduce Flask, FastAPI, requests, or aiohttp.
* **Single-File Vanilla Frontend:** `index.html` contains all HTML, CSS, and client-side JavaScript. External resources are limited to standard CDNs: `hls.js` (v1.5.8) for HLS media playback and FontAwesome 6 for icons. Do not introduce npm, bundlers, or frontend frameworks.
* **Single Page Application Routing:** The frontend uses the HTML5 History API (`pushState` and `popstate`) for `/watch/:id?ep=:ep`. `server.py` routes `/` and `/watch/*` to `index.html`. On page reload or direct link navigation, `restoreWatchFromUrl()` parses window pathname and query strings to restore the active anime and episode without state loss.

---

## 2. Local State, Filesystem Paths & Credentials

Ani UI shares state with `ani-cli` and local developer tools. Never alter these file formats without adhering to their exact specifications.

* **Watch History:** `~/.local/state/ani-cli/ani-hsts`
  * Format: Tab-delimited text (`<episode_number>\t<hianime_id>\t<anime_title>`).
  * Behavior: New watches are prepended to the top of the file. Existing entries for the same show are pruned to avoid duplicate rows.
* **Favorites List:** `~/.local/state/ani-cli/favorites.txt`
  * Format: Plain UTF-8 text file containing one anime title per line. Lines starting with `#` are treated as comments.
* **Favorites Airing Cache:** `~/.local/state/ani-cli/favorites_cache.json`
  * Format: JSON object keyed by lowercase title.
  * Schema:
    ```json
    {
      "display_title": "Frieren: Beyond Journey's End",
      "status": "FINISHED",
      "episodes": 28,
      "next_ep": null,
      "airing_at": null,
      "updated_at": 1731139200.0
    }
    ```
  * Purpose: Shared with CLI scripts like `ani-favorites` to preserve next episode broadcast times and total episode counts.
* **Secrets File:** `~/.secrets.env`
  * Contains key-value pairs (`KEY=VALUE` or `KEY="VALUE"`).
  * Keys used:
    * `MAL_CLIENT_ID`: MyAnimeList OAuth client ID.
    * `MAL_CLIENT_SECRET`: MyAnimeList OAuth client secret.
    * `MAL_ACCESS_TOKEN`: Current OAuth bearer token.
    * `MAL_REFRESH_TOKEN`: OAuth refresh token.
    * `MAL_USERNAME`: MAL account handle.
  * Invariant: When refreshing expired MAL tokens, `server.py` writes the refreshed `MAL_REFRESH_TOKEN` back into `~/.secrets.env` in place, preserving existing keys and comments.
* **Franchise Knowledge Graph Metadata:** `/Users/sampomerantz/MAL-Map/data-collection/data/min_metadata.json`
  * Static JSON export from the local MAL-Map project.
  * Keyed by MAL ID string. Provides canonical Romaji titles and franchise nodes.

---

## 3. Streaming Engine & Reverse-Engineered Decryption

Ani UI streams directly in the browser by reverse-engineering HiAnime video providers.

### Decryption Pipeline
1. **Fetch Episode List:** Retrieve episode internal IDs from `https://hianime.at/api/theme/episode/list/:numericId`.
2. **Retrieve Server List:** Request available video sources from `https://hianime.at/api/theme/episode/servers?episodeId=:internalEpId`.
3. **Select ZokoAnime:** Targets the `ZokoAnime` video server (supporting `sub` and `dub`). The server HTML contains a base64 encoded URL payload inside `data-hash="..."`.
4. **Decrypt Embed Payload:**
   * Decode the base64 URL to get the embed player URL.
   * Fetch the embed HTML.
   * Extract the obfuscated payload string matching `window.__P = "..."`.
   * Decode the payload with base64.
   * Decrypt using XOR cipher with key `b"otaku-embed-v1"`:
     ```python
     key = b"otaku-embed-v1"
     decrypted = bytes([b ^ key[i % len(key)] for i, b in enumerate(raw_bytes)])
     stream_data = json.loads(decrypted.decode("utf-8", errors="ignore"))
     ```
   * Extract master `.m3u8` playlist URL from `stream_data["src"]` and subtitles from `stream_data["subtitles"]`.

### CORS & Video Proxy Mechanics
Upstream CDNs (such as `hls1.drama1.cfd`) block cross-origin requests and require specific `Referer` headers (`https://zokoanime.video/`). Requesting video files directly from a browser triggers Cloudflare 403 Forbidden errors and `hls.js` `fragLoadError`.

To eliminate playback failures:
1. **Playlist Rewriter (`/api/m3u8`):**
   * Parses the upstream master `.m3u8` and media playlists.
   * Rewrites nested playlist URIs to route through `/api/m3u8?url=...&ref=...`.
   * Rewrites video chunks (`.ts`, `.ts.jpg`), `#EXT-X-KEY`, and `#EXT-X-MAP` to route through `/api/proxy?url=...&ref=...`.
2. **Segment & Subtitle Proxy (`/api/proxy`):**
   * Injects `Referer: https://zokoanime.video/` (or the specific embed host) on all upstream requests.
   * Forwards client `Range` headers to support seeking and buffered chunks.
   * Returns upstream HTTP status code (supporting 200 OK and 206 Partial Content).
   * Streams binary data using `shutil.copyfileobj(upstream_resp, self.wfile, 65536)`.
   * Enforces accurate MIME headers: `video/mp2t` for `.ts` files, `text/vtt` for subtitles.
   * Adds permissive CORS headers (`Access-Control-Allow-Origin: *`, `Accept-Ranges: bytes`).

---

## 4. Critical Engineering Invariants & Bug Lessons

Autonomous agents modifying `ani-ui` must enforce the following hard rules:

### Invariant 1: File Handle Variable Shadowing (File Truncation Guard)
* **The Bug:** In `toggle_favorite()`, opening `FAV_FILE` using `with open(FAV_FILE, "w") as f:` while iterating with `for f in favs: f.write(...)` caused immediate data loss. The file was truncated to 0 bytes upon opening, and the first iteration rebound `f` to a string, throwing `AttributeError: 'str' object has no attribute 'write'`.
* **The Rule:** Never reuse file handle names as loop targets. Always use distinct descriptors such as `fp` or `file_desc`:
  ```python
  # CORRECT
  with open(FAV_FILE, "w") as fp:
      for item in favs:
          fp.write(item + "\n")
  ```

### Invariant 2: MyAnimeList API v2 NSFW Suppression
* **The Bug:** MyAnimeList API v2 silently hides all ecchi, mature, and R18 entries from search queries unless `&nsfw=true` is explicitly appended. Searching for *Gushing Over Magical Girls* (*Mahou Shoujo ni Akogarete*) returned zero results, preventing MAL ID resolution and blocking list updates.
* **The Rule:** Every search request sent to `https://api.myanimelist.net/v2/anime` MUST include `&nsfw=true`.

### Invariant 3: Multi-Tier MAL ID Resolution Hierarchy
HiAnime anime IDs (e.g., `gushing-over-magical-girls-1-9557`) do not match MyAnimeList numerical IDs directly. `server.py` implements a 3-tier resolution fallback in `resolve_mal_id_from_hianime()`:
1. **Direct Link Extraction:** Scrape the HiAnime anime detail page for outbound links to `myanimelist.net/anime/(\d+)`.
2. **Title & Synonyms MAL Search:** Extract main title, Japanese title, and all synonym tags from HiAnime HTML, plus the slug title, and query MAL search with `&nsfw=true`.
3. **AniList GraphQL Bridge:** If MAL search yields no match, query AniList GraphQL (`Media(search: $title, type: ANIME) { idMal }`) to fetch the canonical `idMal`.
* **Save Fallback:** When saving MAL watch status, client and server accept both `mal_id` and fallback parameters (`anime_id`, `title`). If `mal_id` is null, the backend resolves the ID before attempting the update.

### Invariant 4: AniList GraphQL `type: ANIME` Guard
* **The Bug:** AniList shares numerical IDs across anime, manga, light novels, and one-shots. Querying AniList search without restricting media type can resolve a show to its manga counterpart, corrupting episode counts and recommendations.
* **The Rule:** Every AniList GraphQL query must specify `type: ANIME`:
  ```graphql
  query ($search: String) {
    Media(search: $search, type: ANIME) {
      id
      idMal
      title { english romaji }
      status
      episodes
      nextAiringEpisode { episode airingAt timeUntilAiring }
    }
  }
  ```

### Invariant 5: Countdown Timers & Airing Cache
* **Airing Calculation:** AniList provides `nextAiringEpisode.airingAt` as a Unix timestamp. Storing fixed relative seconds creates drift. `server.py` stores absolute `airingAt` and computes `time_until = int(airing_at - now)` dynamically on each response.
* **Multi-Tier Caching:**
  1. `AIRING_CACHE` in memory with an expiration TTL of 1800 seconds (30 minutes).
  2. `favorites_cache.json` persisted to disk for cross-tool synchronization.
  3. Live AniList GraphQL queries only on cache miss.
* **Client-Side Live Interval:** The frontend runs a `setInterval(..., 1000)` ticker that decrements active countdown timestamps each second and updates card badges and watch page banners in the DOM without issuing network requests. Badges highlight shows airing within 24 hours with an animated gold pulse dot.
* **Auto-Sorting:** The Favorites view automatically sorts titles by soonest airing episode first, followed by shows without scheduled air dates, and finished series last.

### Invariant 6: MAL-Map Canonical Hash Links
* Links to the external franchise visualizer at `https://sampom100.github.io/MAL-Map/` must use hash navigation: `#show=<canonical_title>`.
* **Formatting Rule:** Lowercase the title and replace all non-alphanumeric characters with single underscores:
  ```javascript
  title.toLowerCase().replace(/[^a-z0-9]/g, '_')
  ```
* **Metadata Normalization:** MAL-Map is indexed primarily on Romaji titles. `server.py` queries `/Users/sampomerantz/MAL-Map/data-collection/data/min_metadata.json` using `mal_id` to retrieve the exact indexed title before computing the hash.

### Invariant 7: User Status Badges Across Catalog Views
Every anime card rendered across Search, Latest, Schedule, Favorites, and Recommendations must reflect user library membership:
* If in Favorites: Displays gold badge (`⭐ FAV`).
* If in Watching: Displays cyan play badge (`📺 WATCHING`).
* If in Completed: Displays green check badge (`✓ WATCHED`).
* Backend helper `tag_card_status()` decorates cards using cached sets from `get_user_status_sets()`.

---

## 5. API Endpoints Reference

### GET Endpoints
* `GET /` and `GET /watch/*`: Serves `index.html`.
* `GET /api/m3u8?url=<url>&ref=<ref>`: Proxies and rewrites HLS playlist contents.
* `GET /api/proxy?url=<url>&ref=<ref>`: Proxies binary segments and subtitles with upstream Referer and byte ranges.
* `GET /api/stream/resolve?id=<anime_id>&episode=<ep>&mode=<sub|dub>`: Decrypts and returns stream playlist and subtitle metadata.
* `GET /api/latest?page=<n>`: Scrapes latest updated episodes from HiAnime with MAL scores and synopsis.
* `GET /api/search?q=<query>&page=<n>`: Searches HiAnime catalog.
* `GET /api/details?id=<anime_id>`: Retrieves anime metadata, episode lists, and user library status.
* `GET /api/schedule?time=<today|yesterday|tomorrow>`: Scrapes release schedule with thumbnail resolution.
* `GET /api/favorites`: Returns user favorites list with next episode countdowns.
* `GET /api/history`: Returns watched episode history from `ani-hsts`.
* `GET /api/mal?status=<watching|plan_to_watch|completed>`: Fetches user MAL anime lists using OAuth.
* `GET /api/mal/status?anime_id=<id>&title=<title>&mal_id=<id>`: Retrieves specific anime status in user MAL library.
* `GET /api/recommendations?mal_id=<id>&anime_id=<id>&title=<title>&limit=<n>`: Concurrently queries MAL API v2 and AniList GraphQL recommendations. Returns separated lists (`mal`, `anilist`) and merged ranked results (`results`). Default limit is 30.

### POST Endpoints
* `POST /api/favorite/toggle`: Toggles favorite state in `favorites.txt`. Body: `{"title": "..."}`.
* `POST /api/mal/update`: Updates user MAL entry. Body:
  ```json
  {
    "mal_id": 12345,
    "anime_id": "optional-hianime-slug",
    "title": "Optional Title",
    "status": "watching",
    "score": 9,
    "num_watched_episodes": 4
  }
  ```
* `POST /api/play`: Launches playback in local `mpv` player process (optional desktop integration).

---

## 6. Verification & Quality Assurance Commands

Run these smoke tests whenever touching backend routes or streaming logic:

```bash
# 1. Check server health
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5050/

# 2. Verify favorites and countdown calculation
curl -s http://localhost:5050/api/favorites | jq '.results[0] | {title, countdown, next_ep, airing_status}'

# 3. Test NSFW MAL resolution (Gushing Over Magical Girls)
curl -s "http://localhost:5050/api/mal/status?anime_id=gushing-over-magical-girls-1-9557" | jq '{mal_id: .data.mal_id, title: .data.title, my_list_status: .data.my_list_status}'

# 4. Test separated and combined recommendations
curl -s "http://localhost:5050/api/recommendations?mal_id=1535&limit=5" | jq '{mal_count: (.mal | length), anilist_count: (.anilist | length), combined_count: (.results | length)}'

# 5. Verify streaming decryption endpoint
curl -s "http://localhost:5050/api/stream/resolve?id=i-m-dating-a-dark-summoner-10504&episode=1&mode=sub" | jq '{has_stream: (.stream_proxy != null), sub_count: (.subtitles | length)}'
```
