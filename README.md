# ani-ui

A local web player and catalog browser for ani-cli.

You can stream episodes in the browser with hls.js or send them to mpv. It reads your local ani-cli watch history and favorites, tracks upcoming release countdowns through AniList, and syncs episode progress and scores with MyAnimeList.

The backend uses only the Python standard library. The frontend is plain HTML, CSS, and JavaScript. No npm, frameworks, or extra pip packages needed.

## Running

Start the server:

```bash
python3 server.py
```

Then open http://localhost:5050 in your browser, or launch it straight from ani-cli:

```bash
ani-cli -W
```

Click the Kill Server button in the top bar to stop the server when you are done.
