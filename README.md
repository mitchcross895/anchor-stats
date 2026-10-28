# Anchor Stats

Rates CS2 site anchors from demo files and builds a 2D replay viewer of every anchor fight.

## Quick start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/). During install, tick **"Add python.exe to PATH"**.
2. Put your `.dem` files in the `demos` folder.
3. Double-click **`run.bat`**.

The first run takes a minute while it sets up a private Python environment (`.venv`). When it finishes, the `output` folder opens:

```
output/
  <demo name>/
    review.html       open in a browser: ratings, fight list and 2D replays
    ratings.txt       text report
    engagements.csv   one row per anchor fight, for spreadsheets
```

Demos that already have output are skipped, so you can keep adding demos and just run it again. To redo everything, run `run.bat --force` from a terminal.

## Mac / Linux

```sh
./run.sh
```

## Command line

`run.bat` / `run.sh` pass any arguments through to the script:

```sh
python anchor-stats.py                          # every demo in demos/ -> output/
python anchor-stats.py --force                  # ...including ones already done
python anchor-stats.py match.dem --viewer review.html --out ratings.txt --csv fights.csv
python anchor-stats.py match.dem --player "Paladin"
python anchor-stats.py --extra-places "B:Bench,Market"   # count extra nav places as a site
```

## How the rating works

See the docstring at the top of [anchor-stats.py](anchor-stats.py). In short:

- An anchor is a CT who sits alone on a site during the round's setup.
- Each fight is scored on kills compared with what an average anchor gets at those odds, with small bonuses for chip damage, time held and surviving.
- **Anchor Rating = 1.00 + average fight score**, so 1.00 is roughly average.
