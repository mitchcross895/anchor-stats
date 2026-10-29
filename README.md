# Anchor Stats

Rates CS2 site anchors from demo files and builds a 2D replay viewer of every anchor fight.

## Quick start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/). During install, tick **"Add python.exe to PATH"**.
2. Double-click **`run.bat`**. Anchor Review opens in your browser.
3. Click **Upload** (or drag a `.dem` file onto the page). The demo is parsed on your computer, which takes a few seconds, then it opens.

The first run takes a minute longer while it sets up a private Python environment (`.venv`). Keep the `run.bat` window open while you use the viewer; close it to stop.

Every demo you've processed is listed in the picker at the top left. You can also put `.dem` files in the `demos` folder; they're processed the next time the app starts. Each demo's results are saved in its own folder:

```
output/
  <demo name>/
    ratings.txt       text report
    engagements.csv   one row per anchor fight, for spreadsheets
    data.json         what the viewer shows (meta.json marks the demo as finished)
    match.json        every round, for watching the full match
    excluded.json     fights you took out of the ratings in the viewer (only if you did)
```

## Mac / Linux

```sh
./run.sh
```

## Command line

`run.bat` / `run.sh` pass any arguments through to the script:

```sh
python anchor-stats.py                          # open the viewer app (default port 8765)
python anchor-stats.py --no-browser --port 9000 # ...without opening a browser, on another port
python anchor-stats.py --batch                  # process new demos in demos/ without the viewer
python anchor-stats.py --batch --force          # ...including ones already done
python anchor-stats.py match.dem --viewer review.html --out ratings.txt --csv fights.csv
python anchor-stats.py match.dem --player "Paladin"
python anchor-stats.py --extra-places "B:Bench,Market"   # count extra nav places as a site
python anchor-stats.py --delete-demos           # delete each .dem once it's processed, to save space
```

Demos are big (200-400 MB each) and the results are tiny, so `--delete-demos` (for example `run.bat --delete-demos`) deletes each `.dem` once its results are saved. You can't reprocess a deleted demo, for example after changing a setting, unless you upload it again.

The viewer app only listens on this computer (`127.0.0.1`), so nobody else on your network can reach it.

## Maps

The viewer draws the real radar for the Premier maps: Inferno, Mirage, Dust 2, Cache, Nuke, Anubis and Ancient. On Nuke, the upper and lower levels are shown side by side and each player appears on the level they're on. Use the **Level** button to show one level at a time, either following the anchor (`auto`) or picked yourself. Any other map falls back to an outline traced from where players stood. See [maps/README.md](maps/README.md) to add more maps.

Each player shows:
- the direction they're looking, as a short line and a faint view cone
- a fading trail of where they've been over the last 2 seconds
- their health bar and the weapon in their hand
- a white dot while they're flashed
- a small red tag if they're carrying the bomb (a dropped bomb shows where it fell)

The clock in the top left shows the round time, and the bomb timer once it's planted. Players and markers keep their size relative to the map as you zoom.

Utility is shown too:
- **Grenades in flight** leave a trail in the thrower's team colour.
- **Smokes and molotovs** show as clouds and fire patches for as long as they last.
- **Flashes and HE grenades** show a quick burst.
- **The event list** says who each flash blinded and for how long, highlighted when it caught the anchor.

Press **U**, or click **Utility**, to hide it. Utility is display-only and doesn't affect the rating.

To leave a fight out of the ratings (a misread anchor, a bugged round, ...), select it and click **Exclude from rating**. The ratings, `ratings.txt` and `engagements.csv` update straight away. The fight stays in the list, struck through, and **Include in rating** puts it back. Exclusions are saved in the demo's `excluded.json`, so they stay put if the demo is reprocessed.

To watch the whole demo, click **Full match** above the list. Every round is listed with its winner, kills and bomb result; pick one to watch it, and playback runs on from round to round. The right-hand panel lists that round's kills and utility, and links to any anchor fights in it. **Anchor fights** takes you back.

To share a review, open it and click **Download offline copy**. That gives you a single `.html` file that works without the app, radar included.

## How the rating works

See the docstring at the top of [anchor-stats.py](anchor-stats.py). In short:

- An anchor is a CT who sits alone on a site during the round's setup.
- Each fight gets six sub-ratings modelled on HLTV Rating 3.0, where 1.00 is an average anchor fight at those odds:

  | Sub-rating | Weight | What it measures |
  |---|---|---|
  | Kills | 30% | eco-adjusted kills vs what an average anchor gets in that situation (1v2, 1v4, ...) |
  | Round swing | 30% | how much the anchor's kills and death moved the CTs' chance of winning the round |
  | Damage | 15% | eco-adjusted damage vs the same expectation |
  | Survival | 10% | alive at the end after fighting for the site |
  | KAST | 10% | got a kill or assist, survived, or was traded |
  | Multi-kill | 5% | 2+ kill fights |

- The eco adjustment counts kills on worse-equipped Ts for less and kills on better-equipped Ts for more.
- HLTV hasn't published its exact weights, so these are an approximation. They can all be tuned in `COMPONENTS` at the top of the script.
- **Anchor Rating = 1.00 + average fight score**, so 1.00 is roughly average.
