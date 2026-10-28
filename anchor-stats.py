"""
anchor-stats.py - Rate CS2 site anchors from a .dem file.

Usage:
    python anchor-stats.py match.dem
    python anchor-stats.py match.dem --player "s1mple" --csv anchors.csv
    python anchor-stats.py match.dem --extra-places "BombsiteB:Bench,Market"

Requires:  pip install demoparser2 pandas

How it works
------------
1. Anchor detection: for every CT-side round, each CT's position is sampled
   during the setup phase (SETUP_START..SETUP_END seconds after freeze time).
   A CT who spends most of that time on a bombsite, with no other CT on the
   same site, is that round's anchor for that site.

2. Engagement ("local area"): the engagement starts at the first damage
   exchanged between the anchor and a T while the anchor is still near their
   spot, or, if there was never any contact, at a bomb plant on their site.
   Ts and CTs within LOCAL_RADIUS units of the anchor around that moment are
   counted, which gives the situation (1v4, 2v3, ...).

3. Score: kills are compared with what an average anchor would get in that
   situation, and each kill is worth more the worse the odds. Chip damage,
   time bought and surviving add small bonuses, so a 1v4 with 2 kills scores
   well and a 1v4 with 0 kills scores badly.

   Anchor Rating = 1.00 + average engagement score (1.00 is roughly average).
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import pandas as pd
from demoparser2 import DemoParser

# ---------------------------------------------------------------- tunables --
TICKRATE = 64
SAMPLE_EVERY = 16             # sample positions every 16 ticks (0.25 s)
SETUP_START = 8               # seconds after freeze end to start judging setups
SETUP_END = 20                # seconds after freeze end to stop judging setups
SETUP_MIN_SHARE = 0.6         # share of setup samples that must be on the site
ANCHOR_LEASH = 1500           # units the anchor may stray from their spot and still count
LOCAL_RADIUS = 1300           # units around the anchor that count as "local"
LOCAL_WINDOW_BEFORE = 3       # seconds before contact to count local players
LOCAL_WINDOW_AFTER = 8        # seconds after contact to count local players
PRE_ROLL = 5                  # viewer: seconds shown before contact
POST_ROLL = 3                 # viewer: seconds shown after the anchor dies
MAX_CLIP = 45                 # viewer: longest clip, in seconds after contact

# Kills an average anchor gets, by number of local enemies (before teammate help).
EXPECTED_KILLS = {1: 0.50, 2: 0.60, 3: 0.65, 4: 0.70, 5: 0.75}
KILL_ODDS_BONUS = 0.20        # each kill is worth 1 + this * (odds - 1)
CHIP_DAMAGE_WEIGHT = 0.003    # per HP of damage on Ts the anchor did not kill
CHIP_DAMAGE_CAP = 0.30
TIME_HELD_WEIGHT = 0.02       # per second alive after contact
TIME_HELD_CAP = 0.30
SURVIVAL_BONUS = 0.20
ASSIST_VALUE = 0.25
NO_CONTACT_PENALTY = 0.25     # bomb planted on your site and you never fought

# Place names (from the map's nav mesh) that count as each site. Any place
# containing "BombsiteA"/"BombsiteB" is always included; add common anchor
# spots per map here or with --extra-places.
SITE_PLACES = {
    "A": {"BombsiteA"},
    "B": {"BombsiteB"},
}
MAP_EXTRA_PLACES = {
    # "de_mirage": {"B": {"Bench", "Market"}},
}

T_TEAM, CT_TEAM = 2, 3


# ----------------------------------------------------------------- helpers --
def site_of(place, site_places):
    if not isinstance(place, str):
        return None
    for site, places in site_places.items():
        if place in places or f"Bombsite{site}" in place:
            return site
    return None


def num(v):
    return 0 if v is None or pd.isna(v) else int(round(float(v)))


def dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def sid(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    return str(int(x)) if not isinstance(x, str) else x


def event_df(parser, name, **kw):
    try:
        df = parser.parse_event(name, **kw)
    except Exception:
        return pd.DataFrame()
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame(df)


def get_rounds(parser):
    """Return a list of (round_no, freeze_end_tick, end_tick, winner)."""
    freeze = event_df(parser, "round_freeze_end")
    ends = event_df(parser, "round_end")
    start = event_df(parser, "round_announce_match_start")
    if freeze.empty:
        sys.exit("No round_freeze_end events found - is this a CS2 demo?")

    match_start = int(start["tick"].max()) if not start.empty else 0
    freeze_ticks = sorted(int(t) for t in freeze["tick"] if t >= match_start)
    end_rows = sorted(
        ((int(r.tick), r.winner if "winner" in ends.columns else None) for r in ends.itertuples()),
        key=lambda x: x[0],
    ) if not ends.empty else []

    rounds = []
    for i, ft in enumerate(freeze_ticks):
        nxt = freeze_ticks[i + 1] if i + 1 < len(freeze_ticks) else float("inf")
        end = next(((t, w) for t, w in end_rows if ft < t <= nxt), None)
        if end is None:
            continue
        winner = end[1]
        if winner in (T_TEAM, "T", "2"):
            winner = "T"
        elif winner in (CT_TEAM, "CT", "3"):
            winner = "CT"
        rounds.append((len(rounds) + 1, ft, end[0], winner))
    return rounds


# -------------------------------------------------------------------- core --
def analyse(path, site_places):
    parser = DemoParser(path)
    header = parser.parse_header()
    map_name = header.get("map_name", "unknown")
    for site, extra in MAP_EXTRA_PLACES.get(map_name, {}).items():
        site_places.setdefault(site, set()).update(extra)

    rounds = get_rounds(parser)

    sample_ticks = sorted({t for _, ft, et, _ in rounds for t in range(ft, et + 1, SAMPLE_EVERY)})
    ticks = parser.parse_ticks(
        ["X", "Y", "Z", "yaw", "health", "last_place_name", "team_num", "is_alive"], ticks=sample_ticks
    )
    ticks["steamid"] = ticks["steamid"].map(sid)
    ticks = ticks[ticks["is_alive"] == True]  # noqa: E712
    names = dict(zip(ticks["steamid"], ticks["name"]))

    deaths = event_df(parser, "player_death")
    hurts = event_df(parser, "player_hurt")
    plants = event_df(parser, "bomb_planted")
    for df in (deaths, hurts, plants):
        for col in ("attacker_steamid", "user_steamid", "assister_steamid"):
            if col in df.columns:
                df[col] = df[col].map(sid)

    by_tick = {t: g for t, g in ticks.groupby("tick")}
    engagements, reviews = [], []

    for rnd, ft, et, winner in rounds:
        rticks = ticks[(ticks["tick"] >= ft) & (ticks["tick"] <= et)]
        if rticks.empty:
            continue
        team_of = dict(zip(rticks["steamid"], rticks["team_num"]))

        # ---- 1. who anchored which site?
        setup = rticks[
            (rticks["team_num"] == CT_TEAM)
            & (rticks["tick"] >= ft + SETUP_START * TICKRATE)
            & (rticks["tick"] <= ft + SETUP_END * TICKRATE)
        ]
        ct_site = {}
        for player, g in setup.groupby("steamid"):
            sites = Counter(site_of(p, site_places) for p in g["last_place_name"])
            site, n = sites.most_common(1)[0]
            if site and n / len(g) >= SETUP_MIN_SHARE:
                spot = (g["X"].mean(), g["Y"].mean(), g["Z"].mean())
                ct_site[player] = (site, spot)

        per_site = defaultdict(list)
        for p, (s, spot) in ct_site.items():
            per_site[s].append((p, spot))

        r_deaths = deaths[(deaths["tick"] >= ft) & (deaths["tick"] <= et)] if not deaths.empty else deaths
        r_hurts = hurts[(hurts["tick"] >= ft) & (hurts["tick"] <= et)] if not hurts.empty else hurts
        r_plants = plants[(plants["tick"] >= ft) & (plants["tick"] <= et)] if not plants.empty else plants

        for site, members in per_site.items():
            if len(members) != 1:
                continue  # stacked site, not an anchor
            anchor, spot = members[0]
            a_ticks = rticks[rticks["steamid"] == anchor].set_index("tick")
            death_row = r_deaths[r_deaths["user_steamid"] == anchor] if not r_deaths.empty else r_deaths
            death_tick = int(death_row["tick"].iloc[0]) if len(death_row) else None

            def anchor_pos(tick):
                near = a_ticks.index[a_ticks.index <= tick]
                if len(near) == 0:
                    return None
                r = a_ticks.loc[near[-1]]
                return (r["X"], r["Y"], r["Z"])

            # ---- 2. find contact on the anchor's site
            contact_tick, contact_kind = None, None
            if not r_hurts.empty:
                involved = r_hurts[
                    ((r_hurts["attacker_steamid"] == anchor) | (r_hurts["user_steamid"] == anchor))
                    & (r_hurts["tick"] >= ft + SETUP_START * TICKRATE)
                ]
                for h in involved.itertuples():
                    other = h.user_steamid if h.attacker_steamid == anchor else h.attacker_steamid
                    if team_of.get(other) != T_TEAM:
                        continue
                    pos = anchor_pos(h.tick)
                    if pos is not None and dist(pos, spot) <= ANCHOR_LEASH:
                        contact_tick, contact_kind = int(h.tick), "fight"
                    break

            plant_here = None
            for p in (r_plants.itertuples() if not r_plants.empty else []):
                planter = rticks[(rticks["steamid"] == p.user_steamid) & (rticks["tick"] <= p.tick)]
                if len(planter) and site_of(planter["last_place_name"].iloc[-1], site_places) == site:
                    plant_here = int(p.tick)

            if contact_tick is None:
                if plant_here is None or (death_tick is not None and death_tick < plant_here):
                    continue  # the hit never came to this anchor
                contact_tick, contact_kind = plant_here, "no contact"

            # ---- 3. who was in the local area?
            lo = contact_tick - LOCAL_WINDOW_BEFORE * TICKRATE
            hi = contact_tick + LOCAL_WINDOW_AFTER * TICKRATE
            if death_tick is not None:
                hi = min(hi, death_tick)
            hi = max(hi, contact_tick)
            enemies, mates = set(), set()
            for t in range(lo - lo % SAMPLE_EVERY, hi + 1, SAMPLE_EVERY):
                g = by_tick.get(t)
                pos = anchor_pos(t)
                if g is None or pos is None:
                    continue
                for r in g.itertuples():
                    if r.steamid == anchor:
                        continue
                    if dist(pos, (r.X, r.Y, r.Z)) <= LOCAL_RADIUS or site_of(r.last_place_name, site_places) == site:
                        (enemies if r.team_num == T_TEAM else mates).add(r.steamid)
            # teammates who were dead before contact don't count
            mates = {m for m in mates
                     if not ((r_deaths["user_steamid"] == m) & (r_deaths["tick"] < contact_tick)).any()}

            # ---- 4. what did the anchor do? (contact .. death / round end)
            end_tick = death_tick if death_tick is not None else et
            # whoever hurt the anchor was obviously local
            for h in (r_hurts.itertuples() if not r_hurts.empty else []):
                if h.user_steamid == anchor and lo <= h.tick <= end_tick and team_of.get(h.attacker_steamid) == T_TEAM:
                    enemies.add(h.attacker_steamid)
            kills_df = r_deaths[
                (r_deaths["attacker_steamid"] == anchor)
                & (r_deaths["tick"] >= lo) & (r_deaths["tick"] <= end_tick)
                & (r_deaths["user_steamid"].map(team_of) == T_TEAM)
            ]
            killed = set(kills_df["user_steamid"])
            kills = len(kills_df)
            assists = 0
            if "assister_steamid" in r_deaths.columns:
                assists = int(((r_deaths["assister_steamid"] == anchor)
                               & (r_deaths["tick"] >= lo) & (r_deaths["tick"] <= end_tick)).sum())
            dmg_df = r_hurts[
                (r_hurts["attacker_steamid"] == anchor)
                & (r_hurts["tick"] >= lo) & (r_hurts["tick"] <= end_tick)
                & (r_hurts["user_steamid"].map(team_of) == T_TEAM)
            ] if not r_hurts.empty else pd.DataFrame(columns=["dmg_health", "user_steamid"])
            damage = int(dmg_df["dmg_health"].sum())
            chip = int(dmg_df[~dmg_df["user_steamid"].isin(killed)]["dmg_health"].sum())
            # and so was anyone the anchor killed or hurt
            enemies |= killed | set(dmg_df["user_steamid"])
            n_enemies = max(1, min(5, len(enemies)))
            survived = death_tick is None
            held = max(0.0, ((death_tick or et) - contact_tick) / TICKRATE)

            # ---- 5. score it
            odds = n_enemies / (1 + len(mates))
            kill_value = 1 + KILL_ODDS_BONUS * (odds - 1)
            expected = EXPECTED_KILLS[n_enemies] / (1 + 0.5 * len(mates))
            situation = f"{1 + len(mates)}v{n_enemies}"
            parts = [
                ("Kills vs expected", (kills - expected) * kill_value,
                 f"({kills} - {expected:.2f} expected) x {kill_value:.2f} for {situation} odds"),
                ("Assists", assists * ASSIST_VALUE, f"{assists} x {ASSIST_VALUE}"),
                ("Chip damage", min(CHIP_DAMAGE_CAP, chip * CHIP_DAMAGE_WEIGHT),
                 f"{chip} dmg on Ts they didn't kill x {CHIP_DAMAGE_WEIGHT} (cap {CHIP_DAMAGE_CAP})"),
                ("Time held", min(TIME_HELD_CAP, held * TIME_HELD_WEIGHT) if contact_kind == "fight" else 0,
                 f"{held:.1f}s alive after contact x {TIME_HELD_WEIGHT} (cap {TIME_HELD_CAP})"),
                ("Survived", SURVIVAL_BONUS if survived and kills > 0 else 0, "alive at the end with a kill"),
                ("No contact", -NO_CONTACT_PENALTY if contact_kind == "no contact" else 0,
                 "bomb planted on their site without a fight"),
            ]
            score = sum(v for _, v, _ in parts)

            # ---- 6. plain-English reasons, for the viewer
            enemy_names = ", ".join(sorted(names.get(e, e) for e in enemies)) or "unknown"
            notes = [
                f"Anchored {site} " + ("alone" if not mates else
                                       "with " + ", ".join(names.get(m, m) for m in mates) + " nearby")
                + f"; {n_enemies} T{'s' if n_enemies > 1 else ''} were local ({enemy_names})."
            ]
            if contact_kind == "no contact":
                notes.append(f"Never traded damage with a T before the bomb went down on {site}.")
            if kills == 0:
                notes.append(f"No kills. An average anchor gets {expected:.2f} in a {situation}, "
                             "so this counts against them.")
            else:
                notes.append(f"{kills} kill{'s' if kills > 1 else ''} vs the {expected:.2f} an average anchor "
                             f"gets in a {situation}"
                             + (f"; each kill is worth x{kill_value:.2f} at these odds." if kill_value > 1 else "."))
            if survived:
                notes.append("Survived the round" + (" with a kill, which earns a bonus." if kills else ".")
                             if contact_kind == "fight" else "Survived, but never fought for the site.")
            else:
                notes.append(f"Died {held:.1f}s after first contact"
                             + (", too quickly to buy much time." if held < 3 else
                                f", buying time for rotations (+{parts[3][1]:.2f})." if contact_kind == "fight" else "."))
            if chip:
                notes.append(f"{chip} damage on Ts they didn't finish (+{parts[2][1]:.2f}"
                             + (", capped)." if chip * CHIP_DAMAGE_WEIGHT > CHIP_DAMAGE_CAP else ")."))
            if assists:
                notes.append(f"{assists} assist{'s' if assists > 1 else ''} (+{parts[1][1]:.2f}).")
            notes.append(("Ts planted on " if plant_here else "Ts never planted on ") + site
                         + f"; CTs {'won' if winner == 'CT' else 'lost'} the round (context only, not scored).")

            # ---- 7. playback clip for the viewer
            view_lo = max(ft, contact_tick - PRE_ROLL * TICKRATE)
            view_hi = min(et, (death_tick or et) + POST_ROLL * TICKRATE, contact_tick + MAX_CLIP * TICKRATE)
            clip = rticks[(rticks["tick"] >= view_lo) & (rticks["tick"] <= view_hi)]
            pids = list(dict.fromkeys(clip["steamid"]))
            pidx = {p: i for i, p in enumerate(pids)}
            frames = [{"t": int(t), "p": [[pidx[r.steamid], num(r.X), num(r.Y), num(r.yaw), num(r.health)]
                                           for r in g.itertuples()]}
                      for t, g in clip.groupby("tick")]
            events = [{"t": contact_tick, "type": "contact"}]
            for d in (r_deaths.itertuples() if not r_deaths.empty else []):
                if view_lo <= d.tick <= view_hi:
                    events.append({"t": int(d.tick), "type": "kill",
                                   "attacker": names.get(d.attacker_steamid, "world"),
                                   "victim": names.get(d.user_steamid, "?"),
                                   "attacker_id": pidx.get(d.attacker_steamid), "victim_id": pidx.get(d.user_steamid),
                                   "weapon": str(getattr(d, "weapon", "") or ""),
                                   "hs": bool(getattr(d, "headshot", False)),
                                   "by_anchor": d.attacker_steamid == anchor, "anchor_died": d.user_steamid == anchor})
            for h in (r_hurts.itertuples() if not r_hurts.empty else []):
                if view_lo <= h.tick <= view_hi and anchor in (h.attacker_steamid, h.user_steamid):
                    events.append({"t": int(h.tick), "type": "hurt",
                                   "attacker": names.get(h.attacker_steamid, "world"),
                                   "victim": names.get(h.user_steamid, "?"),
                                   "attacker_id": pidx.get(h.attacker_steamid), "victim_id": pidx.get(h.user_steamid),
                                   "dmg": int(h.dmg_health), "by_anchor": h.attacker_steamid == anchor})
            for p in (r_plants.itertuples() if not r_plants.empty else []):
                if view_lo <= p.tick <= view_hi:
                    events.append({"t": int(p.tick), "type": "plant", "planter": names.get(p.user_steamid, "?"),
                                   "planter_id": pidx.get(p.user_steamid)})
            events.sort(key=lambda e: e["t"])

            eid = len(engagements)
            reviews.append({
                "id": eid, "contact_tick": contact_tick, "anchor_id": pidx.get(anchor),
                "spot": [num(spot[0]), num(spot[1])],
                "players": [{"name": names.get(p, p), "team": "CT" if team_of.get(p) == CT_TEAM else "T",
                             "local": p in enemies or p in mates} for p in pids],
                "frames": frames, "events": events, "notes": notes,
                "breakdown": [{"label": l, "value": round(v, 2), "detail": d}
                              for l, v, d in parts if v or l == "Kills vs expected"],
            })

            engagements.append({
                "id": eid,
                "round": rnd,
                "player": names.get(anchor, anchor),
                "steamid": anchor,
                "site": site,
                "situation": situation,
                "enemies": n_enemies,
                "teammates": len(mates),
                "contact": contact_kind,
                "kills": kills,
                "assists": assists,
                "damage": damage,
                "survived": survived,
                "time_held_s": round(held, 1),
                "planted_here": plant_here is not None,
                "round_won": winner == "CT",
                "expected_kills": round(expected, 2),
                "score": round(score, 2),
            })

    viewer = {"map": map_name, "tickrate": TICKRATE, "local_radius": LOCAL_RADIUS,
              "background": map_background(ticks), "reviews": reviews}
    return map_name, pd.DataFrame(engagements), viewer


def map_background(ticks, cell=48):
    """Outline the map from everywhere players stood, and label it with nav place names."""
    cells = set(zip((ticks["X"] // cell).astype(int), (ticks["Y"] // cell).astype(int)))
    labels = ticks.groupby("last_place_name")[["X", "Y"]].median()
    return {
        "cell": cell,
        "cells": [[int(x), int(y)] for x, y in cells],
        "labels": [{"name": n, "x": num(r.X), "y": num(r.Y)} for n, r in labels.iterrows() if n],
    }


def write_viewer(path, viewer, eng, summary):
    template = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anchor-viewer-template.html")
    with open(template, encoding="utf-8") as f:
        html = f.read()
    keep = set(eng["id"])
    rows = {r["id"]: r for r in eng.drop(columns="steamid").to_dict("records")}
    viewer = dict(viewer,
                  ratings=summary.to_dict("records"),
                  reviews=[dict(rv, **rows[rv["id"]]) for rv in viewer["reviews"] if rv["id"] in keep])
    data = json.dumps(viewer, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html.replace("__DATA__", data.replace("</", "<\\/")))


def summarise(eng):
    g = eng.groupby(["steamid", "player"])
    summary = pd.DataFrame({
        "engagements": g.size(),
        "kills": g["kills"].sum(),
        "exp_kills": g["expected_kills"].sum().round(2),
        "avg_enemies": g["enemies"].mean().round(2),
        "solo_vs_3plus": g.apply(lambda d: ((d.teammates == 0) & (d.enemies >= 3)).sum()),
        "multi_kills": g.apply(lambda d: (d.kills >= 2).sum()),
        "zero_kill": g.apply(lambda d: (d.kills == 0).sum()),
        "adr_on_site": (g["damage"].sum() / g.size()).round(1),
        "survival_%": (g["survived"].mean() * 100).round(0),
        "avg_time_held": g["time_held_s"].mean().round(1),
        "site_held_%": (g.apply(lambda d: (~d.planted_here).mean()) * 100).round(0),
        "impact": g["score"].sum().round(2),
    })
    summary["anchor_rating"] = (1 + g["score"].mean()).round(2)
    return summary.reset_index().drop(columns="steamid").sort_values("anchor_rating", ascending=False)


def main():
    ap = argparse.ArgumentParser(description="Rate CS2 site anchors from a .dem file.")
    ap.add_argument("demo", help="path to the .dem file")
    ap.add_argument("--player", help="only show this player (name substring)")
    ap.add_argument("--csv", help="write every engagement to this CSV file")
    ap.add_argument("--out", help="also write the printed report to this text file")
    ap.add_argument("--viewer", help="write a 2D replay viewer of every engagement to this .html file")
    ap.add_argument("--extra-places", action="append", default=[],
                    help='treat extra nav place names as a site, e.g. "BombsiteB:Bench,Market"')
    args = ap.parse_args()

    site_places = {k: set(v) for k, v in SITE_PLACES.items()}
    for spec in args.extra_places:
        site, _, places = spec.partition(":")
        site = site.replace("Bombsite", "").strip().upper()
        site_places.setdefault(site, set()).update(p.strip() for p in places.split(",") if p.strip())

    map_name, eng, viewer = analyse(args.demo, site_places)
    if eng.empty:
        sys.exit("No anchor engagements found. Try --extra-places if the map uses other place names.")
    if args.player:
        eng = eng[eng["player"].str.contains(args.player, case=False)]
        if eng.empty:
            sys.exit(f"No anchor engagements for '{args.player}'.")

    cols = ["round", "player", "site", "situation", "contact", "kills", "assists",
            "damage", "survived", "time_held_s", "planted_here", "round_won", "score"]
    summary = summarise(eng)
    report = "\n".join([
        f"Map: {map_name}   anchor engagements: {len(eng)}",
        "",
        "=== Anchor ratings ===",
        summary.to_string(index=False),
        "",
        "=== Engagements ===",
        eng[cols].to_string(index=False),
    ])
    print("\n" + report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\nWrote report to {args.out}")

    if args.viewer:
        write_viewer(args.viewer, viewer, eng, summary)
        print(f"\nWrote 2D review viewer to {args.viewer} - open it in a browser")

    if args.csv:
        eng.to_csv(args.csv, index=False)
        print(f"\nWrote {len(eng)} engagements to {args.csv}")


if __name__ == "__main__":
    main()
