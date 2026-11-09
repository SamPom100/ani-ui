# ani-ui

A sleek HiAnime-style web interface for `ani-cli` with in-browser HLS streaming, full MyAnimeList two-way synchronization, and release schedules.

## Features

- **In-Browser Streaming**: HTML5 video playback via `hls.js` with full segment & WebVTT subtitle proxying.
- **Native MPV Playback**: Launch playback directly in native `mpv` with subtitles.
- **MyAnimeList Integration**:
  - Live OAuth status updating (watching, completed, plan to watch, score, watched episodes).
  - Syncs library status across cards and schedules with `✓ WATCHED` and `📺 WATCHING` badges.
  - Automatic title and romaji mapping via HiAnime ZokoAnime stream metadata.
- **Latest Updates & Schedule**:
  - Live HiAnime recently updated feed with MAL scores and synopses.
  - 7-day schedule with local timezone synchronization.
- **Watch History & Favorites**:
  - Integrates with local `ani-cli` history (`~/.local/state/ani-cli/ani-hsts`) and favorites (`~/.local/state/ani-cli/favorites.txt`).
- **Zero Dependencies**: Built with Python 3 standard library and vanilla HTML/CSS/JavaScript.

## Running

```bash
python3 server.py
```

Then open `http://localhost:5050` in your browser.
