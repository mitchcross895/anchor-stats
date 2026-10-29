"""
anchor-stats.py - Rate CS2 site anchors from a .dem file.

Usage:
    python anchor-stats.py                # open the viewer app; upload demos there
    python anchor-stats.py --batch        # every new .dem in demos/ -> output/<demo name>/
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
   Ts and CTs within LOCAL_RADIUS units of the anchor (MAP_LOCAL_RADIUS per
   map) around that moment are counted, which gives the situation (1v4, 2v3, ...).

3. Score, modelled on HLTV Rating 3.0: each fight gets six sub-ratings where
   1.00 is an average anchor fight in that situation:
     Kills        eco-adjusted kills vs what an average anchor gets at those odds
     Damage       eco-adjusted damage vs the same expectation
     Survival     alive at the end after fighting for the site
     KAST         got a kill or assist, survived, or was traded
     Multi-kill   2+ kill fights
     Round swing  how much the anchor's kills (shared by damage dealt) and death
                  moved the CTs' chance of winning the round
   The eco adjustment makes kills on worse-equipped Ts worth less and kills on
   better-equipped Ts worth more. Sub-ratings are weighted (see COMPONENTS);
   fight score = weighted sum of (sub-rating - 1).

   Anchor Rating = 1.00 + average fight score (1.00 is roughly average).
"""

import argparse
import base64
import functools
import gzip
import json
import math
import os
import queue
import re
import sys
import threading
import time
import uuid
import webbrowser
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

# ---------------------------------------------------------------- tunables --
TICKRATE = 64
SAMPLE_EVERY = 16             # the analysis samples positions every 16 ticks (0.25 s)
CLIP_SAMPLE_EVERY = 8         # viewer clips sample every 8 ticks (0.125 s) for smoother movement
SETUP_START = 8               # seconds after freeze end to start judging setups
SETUP_END = 20                # seconds after freeze end to stop judging setups
SETUP_MIN_SHARE = 0.6         # share of setup samples that must be on the site
ANCHOR_LEASH = 1500           # units the anchor may stray from their spot and still count
LOCAL_RADIUS = 1000           # units around the anchor that count as "local" (the viewer's dashed circle)
MAP_LOCAL_RADIUS = {          # per-map override: tight sites get a smaller circle, open ones a larger one
    "de_nuke": 750, "de_inferno": 850, "de_anubis": 900, "de_ancient": 950,
    "de_cache": 1000, "de_mirage": 1050, "de_dust2": 1250,
}
LOCAL_WINDOW_BEFORE = 3       # seconds before contact to count local players
LOCAL_WINDOW_AFTER = 8        # seconds after contact to count local players
PRE_ROLL = 5                  # viewer: seconds shown before contact
POST_ROLL = 3                 # viewer: seconds shown after the anchor dies
MAX_CLIP = 45                 # viewer: longest clip, in seconds after contact

# Kills an average anchor gets, by number of local enemies (before teammate help).
EXPECTED_KILLS = {1: 0.50, 2: 0.60, 3: 0.65, 4: 0.70, 5: 0.75}
DAMAGE_PER_EXPECTED_KILL = 120  # an average anchor's damage = this x expected kills
TRADE_WINDOW = 5              # seconds; a death avenged within this counts as traded
TRADED_DEATH_SHARE = 0.5      # share of the round-swing cost a traded death still carries
NO_CONTACT_PLANT_SHARE = 0.5  # share of the plant's round swing charged to an anchor who never fought
MULTI_KILL_VALUE = {0: 0, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4}

# HLTV 3.0-style sub-ratings. Each is 1 + (fight value - average) / spread, so
# 1.00 is an average anchor fight; the weights mirror 3.0's emphasis on kills
# and round swing (HLTV has not published its exact weights).
#   name: (label, report column, weight, average, spread)
COMPONENTS = {
    "kills":    ("Kills", "kill_r", 0.30, None, 0.80),         # eco-adjusted; average = EXPECTED_KILLS
    "damage":   ("Damage", "dmg_r", 0.15, None, 120),          # eco-adjusted; average = DAMAGE_PER_EXPECTED_KILL x expected
    "survival": ("Survival", "surv_r", 0.10, 0.30, 0.70),
    "kast":     ("KAST", "kast_r", 0.10, 0.60, 0.80),          # kill, assist, survived or traded
    "multi":    ("Multi-kill", "multi_r", 0.05, 0.12, 0.80),   # MULTI_KILL_VALUE
    "swing":    ("Round swing", "swing_r", 0.30, 0.00, 0.20),  # change in the CTs' round win probability
}

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

# Events read from the demo, with the columns the analysis relies on (an event
# that never happens comes back missing, so it gets these columns empty).
EVENTS = {
    "round_freeze_end": ["tick"],
    "round_end": ["tick", "winner"],
    "round_announce_match_start": ["tick"],
    "player_death": ["tick", "user_steamid", "attacker_steamid", "assister_steamid", "weapon", "headshot"],
    "player_hurt": ["tick", "user_steamid", "attacker_steamid", "dmg_health"],
    "bomb_planted": ["tick", "user_steamid"],
    # the bomb and utility, for the viewer
    "bomb_pickup": ["tick", "user_steamid"],
    "bomb_dropped": ["tick", "user_steamid"],
    "bomb_defused": ["tick"],
    "bomb_exploded": ["tick"],
    "smokegrenade_detonate": ["tick", "entityid", "x", "y", "z"],
    "smokegrenade_expired": ["tick", "entityid"],
    "inferno_startburn": ["tick", "entityid", "x", "y", "z"],
    "inferno_expire": ["tick", "entityid"],
    "flashbang_detonate": ["tick", "entityid", "x", "y", "z"],
    "hegrenade_detonate": ["tick", "entityid", "x", "y", "z"],
    "decoy_started": ["tick", "entityid", "x", "y", "z"],
    "decoy_detonate": ["tick", "entityid"],
    "player_blind": ["tick", "entityid", "user_steamid", "attacker_steamid", "blind_duration"],
}

# Thrown grenades: projectile class -> (kind, event where it goes off, event where
# its effect ends, seconds the effect lasts if that event is missing). A molotov's
# fire is its own entity, so it's matched to the projectile by time and place.
GRENADES = {
    "CSmokeGrenadeProjectile": ("smoke", "smokegrenade_detonate", "smokegrenade_expired", 20),
    "CFlashbangProjectile": ("flash", "flashbang_detonate", None, 0),
    "CHEGrenadeProjectile": ("he", "hegrenade_detonate", None, 0),
    "CMolotovProjectile": ("molotov", None, "inferno_expire", 7),
    "CIncendiaryGrenadeProjectile": ("molotov", None, "inferno_expire", 7),
    "CDecoyProjectile": ("decoy", "decoy_started", "decoy_detonate", 15),
}
GRENADE_PATH_EVERY = 8        # viewer: keep every 8th tick of a grenade's flight

T_TEAM, CT_TEAM = 2, 3

HERE = os.path.dirname(os.path.abspath(__file__))
DEMOS_DIR = os.path.join(HERE, "demos")
OUTPUT_DIR = os.path.join(HERE, "output")
MAPS_DIR = os.path.join(HERE, "maps")  # <map>.png radar + <map>.txt overview, from the game files
TEMPLATE = os.path.join(HERE, "anchor-viewer-template.html")


# ----------------------------------------------------------------- helpers --
def site_of(place, site_places):
    if not isinstance(place, str):
        return ""
    for site, places in site_places.items():
        if place in places or f"Bombsite{site}" in place:
            return site
    return ""


def num(v):
    return 0 if v is None or pd.isna(v) else int(round(float(v)))


def dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def sid(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    return str(int(x)) if not isinstance(x, str) else x


def ct_win_prob(ct, t, planted):
    """Rough chance the CTs win from ct-vs-t alive, before or after the plant."""
    if ct <= 0:
        return 0.0
    if t <= 0:
        return 0.9 if planted else 1.0  # post-plant they still have to defuse
    x = 0.6 * (ct - t) + math.log(ct / t) - (1.0 if planted else 0.0)
    return 1 / (1 + math.exp(-x))


def econ_factor(victim_value, anchor_value):
    """Kills on worse-equipped Ts are worth less, on better-equipped Ts more (HLTV 3.0 eco adjustment)."""
    return min(1.3, max(0.6, ((victim_value + 500) / (anchor_value + 500)) ** 0.35))


def parse_events(parser):
    """Every event in EVENTS from one pass over the demo."""
    found = dict(parser.parse_events(list(EVENTS)))
    events = {}
    for name, cols in EVENTS.items():
        df = found.get(name, pd.DataFrame())
        df = df.reindex(columns=list(dict.fromkeys([*df.columns, *cols])))
        for col in ("attacker_steamid", "user_steamid", "assister_steamid"):
            if col in df.columns:
                df[col] = df[col].map(sid)
        events[name] = df
    return events


def by_entity(df, cols):
    """{entity id: rows of cols sorted by tick} for one event."""
    out = defaultdict(list)
    for row in df.sort_values("tick")[["entityid", *cols]].itertuples(index=False):
        if not pd.isna(row[0]):
            out[int(row[0])].append(row[1:])
    return out


def grenade_throws(parser, events):
    """Every thrown grenade: flight path, where and when it went off, when its effect ended, who it blinded."""
    try:
        g = parser.parse_grenades()
    except Exception:  # utility is a viewer extra; never fail a demo over it
        return []
    g = g[g["grenade_type"].isin(list(GRENADES)) & g["x"].notna()]
    if g.empty:
        return []
    g = g.sort_values(["grenade_entity_id", "tick"], kind="stable")
    ent, tick = g["grenade_entity_id"].to_numpy(), g["tick"].to_numpy()
    xyz = np.nan_to_num(g[["x", "y", "z"]].to_numpy(dtype=float)).round().astype(int)
    kinds, owners = g["grenade_type"].to_numpy(), g["steamid"].to_numpy()
    # entity ids get reused during a match, so a gap in ticks starts a new throw
    starts = np.flatnonzero(np.r_[True, (ent[1:] != ent[:-1]) | (np.diff(tick) > 32)])

    went_off = {name: by_entity(events[name], ["tick", "x", "y", "z"])
                for name in {v[1] for v in GRENADES.values() if v[1]}}
    ended = {name: by_entity(events[name], ["tick"]) for name in {v[2] for v in GRENADES.values() if v[2]}}
    blinds = by_entity(events["player_blind"], ["tick", "user_steamid", "blind_duration"])
    fires = events["inferno_startburn"].sort_values("tick")
    fire_tick, fire_xyz, fire_id = (fires["tick"].to_numpy(), fires[["x", "y", "z"]].to_numpy(dtype=float),
                                    fires["entityid"].to_numpy())

    throws = []
    for a, b in zip(starts, np.r_[starts[1:], len(g)]):
        kind, off_event, end_event, lasts = GRENADES[kinds[a]]
        entity, first, last = int(ent[a]), int(tick[a]), int(tick[b - 1])
        det, (x, y, z), effect = last, xyz[b - 1], entity
        if off_event:
            hit = next((e for e in went_off[off_event].get(entity, ()) if first <= e[0] <= last + 64), None)
            if hit:
                det, x, y, z = int(hit[0]), num(hit[1]), num(hit[2]), num(hit[3])
        else:  # a molotov's fire: the nearest one that starts burning as the projectile lands
            i, j = np.searchsorted(fire_tick, [last - 16, last + 65])
            if j > i:
                gap = ((fire_xyz[i:j, :2] - (x, y)) ** 2).sum(axis=1)
                k = int(np.nanargmin(gap)) if not np.isnan(gap).all() else None
                if k is not None and gap[k] <= 300 ** 2:
                    det, effect = int(fire_tick[i + k]), int(fire_id[i + k])
                    x, y, z = (num(v) for v in fire_xyz[i + k])
        end = det
        if end_event:
            later = [e[0] for e in ended[end_event].get(effect, ()) if e[0] > det]
            end = int(later[0]) if later else det + lasts * TICKRATE

        n = int(np.searchsorted(tick[a:b], det, side="right"))  # flight samples up to the detonation
        keep = list(range(0, n, GRENADE_PATH_EVERY))
        if n and keep[-1] != n - 1:
            keep.append(n - 1)
        path = [[int(tick[a + i]), int(xyz[a + i, 0]), int(xyz[a + i, 1])] for i in keep]
        blinded = []
        if kind == "flash":
            blinded = [(who, round(float(dur), 1)) for t, who, dur in blinds.get(entity, ()) if abs(t - det) <= 16]
        throws.append({"kind": kind, "by": sid(owners[a]), "t0": first, "td": det, "te": end,
                       "x": int(x), "y": int(y), "z": int(z), "path": path or [[det, int(x), int(y)]],
                       "blinded": blinded})
    return throws


def weapon_label(name):
    """Short label for a held weapon; knife skins all read as "Knife"."""
    if not isinstance(name, str) or not name:
        return ""
    low = name.lower()
    if any(k in low for k in ("knife", "dagger", "bayonet", "karambit")):
        return "Knife"
    return {"C4 Explosive": "C4", "High Explosive Grenade": "HE Grenade", "Incendiary Grenade": "Incendiary"}.get(
        name, name)


def bomb_log(events):
    """Every change of who has the bomb: (tick, carrier steamid or None, dropper steamid or None), by tick."""
    log = [(int(r.tick), r.user_steamid, None) for r in events["bomb_pickup"].itertuples()]
    log += [(int(r.tick), None, r.user_steamid) for r in events["bomb_dropped"].itertuples()]
    log += [(int(r.tick), None, None) for r in events["bomb_planted"].itertuples()]
    return sorted(log, key=lambda e: e[0])


def get_rounds(events):
    """Return a list of (round_no, freeze_end_tick, end_tick, winner)."""
    freeze, ends, start = events["round_freeze_end"], events["round_end"], events["round_announce_match_start"]
    if freeze.empty:
        raise ValueError("No round_freeze_end events found - is this a CS2 demo?")

    match_start = int(start["tick"].max()) if not start.empty else 0
    freeze_ticks = sorted(int(t) for t in freeze["tick"] if t >= match_start)
    end_rows = sorted((int(r.tick), r.winner) for r in ends.itertuples())

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


def clip_frames(clip, pidx, levels):
    """A clip's position frames, plus each player's held weapon as changes only.

    frames: [{"t": tick, "p": [[player, x, y, yaw, hp(, z)], ...]}, ...]
    held:   {player: [[tick, weapon index], ...]}, weapons: [weapon label, ...]
    """
    if clip.empty:
        return [], {}, []
    cols = ["X", "Y", "yaw", "health"] + (["Z"] if levels else [])
    vals = np.nan_to_num(clip[cols].to_numpy(dtype=float)).round().astype(int)
    pid = clip["steamid"].map(pidx).to_numpy()
    rows = np.column_stack([pid, vals])
    tick = clip["tick"].to_numpy()
    cut = np.flatnonzero(np.diff(tick)) + 1
    frames = [{"t": int(t), "p": chunk.tolist()} for t, chunk in zip(tick[np.r_[0, cut]], np.split(rows, cut))]

    labels = clip["active_weapon_name"].map(weapon_label).to_numpy()
    weapons = sorted(set(labels) - {""})
    windex = {w: i for i, w in enumerate(weapons)}
    held = {}
    for p in set(pid.tolist()):
        mine = np.flatnonzero(pid == p)
        changes = [i for k, i in enumerate(mine) if k == 0 or labels[i] != labels[mine[k - 1]]]
        held[int(p)] = [[int(tick[i]), windex.get(labels[i])] for i in changes]
    return frames, held, weapons


# -------------------------------------------------------------------- core --
def analyse(path, site_places):
    parser = DemoParser(path)
    map_name = os.path.basename(parser.parse_header().get("map_name", "unknown"))
    radius = MAP_LOCAL_RADIUS.get(map_name, LOCAL_RADIUS)
    for site, extra in MAP_EXTRA_PLACES.get(map_name, {}).items():
        site_places.setdefault(site, set()).update(extra)

    events = parse_events(parser)
    deaths, hurts, plants = events["player_death"], events["player_hurt"], events["bomb_planted"]
    rounds = get_rounds(events)
    throws = grenade_throws(parser, events)
    bombs = bomb_log(events)
    bomb_ends = sorted([(int(t), "defused") for t in events["bomb_defused"]["tick"]]
                       + [(int(t), "exploded") for t in events["bomb_exploded"]["tick"]])
    levels = bool((radar(map_name) or {}).get("lower"))  # clips only need height on two-level maps

    # the viewer's clips want CLIP_SAMPLE_EVERY; reading the extra ticks costs next to nothing
    sample_ticks = sorted({t for _, ft, et, _ in rounds for t in range(ft, et + 1, CLIP_SAMPLE_EVERY)})
    all_ticks = parser.parse_ticks(
        ["X", "Y", "Z", "yaw", "health", "last_place_name", "team_num", "is_alive", "current_equip_value",
         "active_weapon_name"],
        ticks=sample_ticks,
    )
    all_ticks = all_ticks[all_ticks["is_alive"] == True].sort_values("tick", kind="stable")  # noqa: E712
    all_ticks["steamid"] = all_ticks["steamid"].map(sid)
    # the analysis itself runs on every SAMPLE_EVERY-th tick of each round
    round_of = np.searchsorted([ft for _, ft, _, _ in rounds], all_ticks["tick"].to_numpy(), side="right") - 1
    round_start = np.array([ft for _, ft, _, _ in rounds])[np.maximum(round_of, 0)]
    ticks = all_ticks[(all_ticks["tick"].to_numpy() - round_start) % SAMPLE_EVERY == 0].copy()
    sites = {p: site_of(p, site_places) for p in ticks["last_place_name"].unique()}
    ticks["site"] = ticks["last_place_name"].map(sites)
    names = dict(zip(ticks["steamid"], ticks["name"]))

    engagements, reviews, match = [], [], []
    prev_end = 0
    for rnd, ft, et, winner in rounds:
        rticks = ticks[(ticks["tick"] >= ft) & (ticks["tick"] <= et)]
        round_prev_end, prev_end = prev_end, et
        if rticks.empty:
            continue
        r_all = all_ticks[(all_ticks["tick"] >= ft) & (all_ticks["tick"] <= et)]
        team_of = dict(zip(rticks["steamid"], rticks["team_num"]))
        # what each player bought (equipment value once the buy is done)
        buy = rticks[rticks["tick"] <= ft + SETUP_END * TICKRATE].groupby("steamid")["current_equip_value"].max()

        # ---- 1. who anchored which site?
        setup = rticks[
            (rticks["team_num"] == CT_TEAM)
            & (rticks["tick"] >= ft + SETUP_START * TICKRATE)
            & (rticks["tick"] <= ft + SETUP_END * TICKRATE)
        ]
        per_site = defaultdict(list)
        for player, g in setup.groupby("steamid"):
            site, n = Counter(g["site"]).most_common(1)[0]
            if site and n / len(g) >= SETUP_MIN_SHARE:
                per_site[site].append((player, (g["X"].mean(), g["Y"].mean(), g["Z"].mean())))

        r_deaths = deaths[(deaths["tick"] >= ft) & (deaths["tick"] <= et)].sort_values("tick")
        r_hurts = hurts[(hurts["tick"] >= ft) & (hurts["tick"] <= et)]
        r_plants = plants[(plants["tick"] >= ft) & (plants["tick"] <= et)]
        plant_tick = int(r_plants["tick"].min()) if not r_plants.empty else None

        # CT win probability before and after every death, for round swing
        totals = {team: sum(1 for v in team_of.values() if v == team) for team in (CT_TEAM, T_TEAM)}
        alive = dict(totals)
        swings = []  # (death row, ct win prob before, after)
        for d in r_deaths.itertuples():
            planted = plant_tick is not None and plant_tick <= d.tick
            before = ct_win_prob(alive[CT_TEAM], alive[T_TEAM], planted)
            if team_of.get(d.user_steamid) in alive:
                alive[team_of[d.user_steamid]] -= 1
            swings.append((d, before, ct_win_prob(alive[CT_TEAM], alive[T_TEAM], planted)))

        # dead players watching also get "blinded", so only keep those alive when the flash went off
        alive_at = lambda t: set(rticks.loc[(rticks["tick"] - t).abs() <= SAMPLE_EVERY, "steamid"])

        def clip(view_lo, view_hi, anchor=None):
            """Playback for the viewer from view_lo to view_hi: positions, kills, hits (all of them, or only
            the anchor's), the plant, utility, the bomb and the clock. Returns (clip, player ids)."""
            part = r_all[(r_all["tick"] >= view_lo) & (r_all["tick"] <= view_hi)]
            pids = list(dict.fromkeys(part["steamid"]))
            pidx = {p: i for i, p in enumerate(pids)}
            frames, holding, weapons = clip_frames(part, pidx, levels)
            # who has the bomb: the state when the clip starts, then every change during it
            before = [e for e in bombs if round_prev_end < e[0] <= view_lo]
            bomb = [[view_lo, pidx.get(before[-1][1]), None]] if before and before[-1][1] else []
            bomb += [[t, pidx.get(c), pidx.get(d)] for t, c, d in bombs if view_lo < t <= view_hi]
            bomb_end = next(((t, k) for t, k in bomb_ends if ft <= t <= et), None)
            in_view = lambda df: df[(df["tick"] >= view_lo) & (df["tick"] <= view_hi)].itertuples()
            events = []
            for d in in_view(r_deaths):
                events.append({"t": int(d.tick), "type": "kill",
                               "attacker": names.get(d.attacker_steamid, "world"),
                               "victim": names.get(d.user_steamid, "?"),
                               "attacker_id": pidx.get(d.attacker_steamid), "victim_id": pidx.get(d.user_steamid),
                               "weapon": str(d.weapon) if isinstance(d.weapon, str) else "",
                               "hs": d.headshot == True,  # noqa: E712 (NaN when missing)
                               "by_anchor": anchor is not None and d.attacker_steamid == anchor,
                               "anchor_died": anchor is not None and d.user_steamid == anchor})
            for h in in_view(r_hurts):
                if anchor is None or anchor in (h.attacker_steamid, h.user_steamid):
                    events.append({"t": int(h.tick), "type": "hurt",
                                   "attacker": names.get(h.attacker_steamid, "world"),
                                   "victim": names.get(h.user_steamid, "?"),
                                   "attacker_id": pidx.get(h.attacker_steamid),
                                   "victim_id": pidx.get(h.user_steamid),
                                   "dmg": int(h.dmg_health),
                                   "by_anchor": anchor is not None and h.attacker_steamid == anchor})
            for p in in_view(r_plants):
                events.append({"t": int(p.tick), "type": "plant", "planter": names.get(p.user_steamid, "?"),
                               "planter_id": pidx.get(p.user_steamid)})
            utility = [dict(u, by=names.get(u["by"], "?"), team="CT" if team_of.get(u["by"]) == CT_TEAM else "T",
                            blinded=[[names.get(p, "?"), s] for p, s in u["blinded"] if p in alive_at(u["td"])])
                       for u in throws if ft <= u["t0"] <= view_hi and u["te"] >= view_lo]
            return {"round": rnd, "frames": frames, "events": events, "utility": utility,
                    "weapons": weapons, "held": holding, "bomb": bomb, "round_start": ft, "plant_tick": plant_tick,
                    "bomb_end": list(bomb_end) if bomb_end else None}, pids

        for site, members in per_site.items():
            if len(members) != 1:
                continue  # stacked site, not an anchor
            anchor, spot = members[0]
            a_ticks = rticks[rticks["steamid"] == anchor]
            a_tick, a_xyz = a_ticks["tick"].to_numpy(), a_ticks[["X", "Y", "Z"]].to_numpy()
            death_row = r_deaths[r_deaths["user_steamid"] == anchor]
            death_tick = int(death_row["tick"].iloc[0]) if len(death_row) else None

            def anchor_pos(tick):
                i = a_tick.searchsorted(tick, side="right") - 1
                return a_xyz[i] if i >= 0 else None

            # ---- 2. find contact on the anchor's site
            contact_tick, contact_kind = None, None
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
            for p in r_plants.itertuples():
                planter = rticks[(rticks["steamid"] == p.user_steamid) & (rticks["tick"] <= p.tick)]
                if len(planter) and planter["site"].iloc[-1] == site:
                    plant_here = int(p.tick)

            if contact_tick is None:
                if plant_here is None or (death_tick is not None and death_tick < plant_here):
                    continue  # the hit never came to this anchor
                contact_tick, contact_kind = plant_here, "no contact"

            # ---- 3. who was in the local area? (near the anchor, or on their site)
            lo = contact_tick - LOCAL_WINDOW_BEFORE * TICKRATE
            hi = contact_tick + LOCAL_WINDOW_AFTER * TICKRATE
            if death_tick is not None:
                hi = min(hi, death_tick)
            hi = max(hi, contact_tick)
            near = rticks[(rticks["tick"] >= lo - lo % SAMPLE_EVERY) & (rticks["tick"] <= hi)
                          & (rticks["steamid"] != anchor)]
            if len(a_tick):
                idx = a_tick.searchsorted(near["tick"].to_numpy(), side="right") - 1
                seen = idx >= 0  # samples before the anchor's first one have no anchor position
                near, idx = near[seen], idx[seen]
                gap = ((near[["X", "Y", "Z"]].to_numpy() - a_xyz[idx]) ** 2).sum(axis=1) ** 0.5
                near = near[(gap <= radius) | (near["site"] == site).to_numpy()]
            else:
                near = near.iloc[0:0]
            enemies = set(near.loc[near["team_num"] == T_TEAM, "steamid"])
            mates = set(near.loc[near["team_num"] != T_TEAM, "steamid"])
            # teammates who were dead before contact don't count
            mates -= set(r_deaths.loc[r_deaths["tick"] < contact_tick, "user_steamid"])

            # ---- 4. what did the anchor do? (contact .. death / round end)
            end_tick = death_tick if death_tick is not None else et
            in_window = lambda df: df[(df["tick"] >= lo) & (df["tick"] <= end_tick)]
            w_hurts, w_deaths = in_window(r_hurts), in_window(r_deaths)
            # whoever hurt the anchor was obviously local
            enemies |= set(w_hurts.loc[(w_hurts["user_steamid"] == anchor)
                                       & (w_hurts["attacker_steamid"].map(team_of) == T_TEAM), "attacker_steamid"])
            kills_df = w_deaths[(w_deaths["attacker_steamid"] == anchor)
                                & (w_deaths["user_steamid"].map(team_of) == T_TEAM)]
            kills = len(kills_df)
            assists = int((w_deaths["assister_steamid"] == anchor).sum())
            dmg_df = w_hurts[(w_hurts["attacker_steamid"] == anchor)
                             & (w_hurts["user_steamid"].map(team_of) == T_TEAM)]
            damage = int(dmg_df["dmg_health"].sum())
            # and so was anyone the anchor killed or hurt
            enemies |= set(kills_df["user_steamid"]) | set(dmg_df["user_steamid"])
            n_enemies = max(1, min(5, len(enemies)))
            survived = death_tick is None
            held = max(0.0, ((death_tick or et) - contact_tick) / TICKRATE)

            # ---- 5. score it, HLTV 3.0 style
            expected = EXPECTED_KILLS[n_enemies] / (1 + 0.5 * len(mates))
            situation = f"{1 + len(mates)}v{n_enemies}"
            fought = contact_kind == "fight"
            eco = {v: econ_factor(buy.get(v, 0), buy.get(anchor, 0)) for v in enemies}
            eco_kills = sum(eco.get(v, 1.0) for v in kills_df["user_steamid"])
            eco_damage = sum(h.dmg_health * eco.get(h.user_steamid, 1.0) for h in dmg_df.itertuples())
            traded = False
            if death_tick is not None:
                killer = death_row["attacker_steamid"].iloc[0]
                traded = bool(((r_deaths["user_steamid"] == killer) & (r_deaths["tick"] > death_tick)
                               & (r_deaths["tick"] <= death_tick + TRADE_WINDOW * TICKRATE)).any())
            kast = kills > 0 or assists > 0 or (survived and fought) or traded

            # round swing: the anchor's share of each kill's win-probability change, minus their death's.
            # A kill's credit is half to the killer and half split by damage dealt to the victim.
            swing = 0.0
            for d, before, after in swings:
                if not lo <= d.tick <= end_tick:
                    continue
                if d.user_steamid == anchor:
                    share = TRADED_DEATH_SHARE if traded else 1.0
                elif team_of.get(d.user_steamid) == T_TEAM:
                    hits = r_hurts[(r_hurts["user_steamid"] == d.user_steamid) & (r_hurts["tick"] <= d.tick)
                                   & (r_hurts["attacker_steamid"].map(team_of) == CT_TEAM)]
                    total = hits["dmg_health"].sum()
                    mine = hits.loc[hits["attacker_steamid"] == anchor, "dmg_health"].sum()
                    share = (0.5 if d.attacker_steamid == anchor else 0) + (0.5 * mine / total if total else 0)
                else:
                    continue
                swing += share * (after - before)
            if not fought and plant_here is not None:
                dead = r_deaths.loc[r_deaths["tick"] < plant_here, "user_steamid"].map(team_of)
                ct_n = totals[CT_TEAM] - int((dead == CT_TEAM).sum())
                t_n = totals[T_TEAM] - int((dead == T_TEAM).sum())
                swing += NO_CONTACT_PLANT_SHARE * (ct_win_prob(ct_n, t_n, True) - ct_win_prob(ct_n, t_n, False))
            swing = float(swing)

            values = {  # name: (fight value, average for this fight or None for the COMPONENTS average)
                "kills": (eco_kills, expected),
                "damage": (eco_damage, DAMAGE_PER_EXPECTED_KILL * expected),
                "survival": (float(survived and fought), None),
                "kast": (float(kast), None),
                "multi": (MULTI_KILL_VALUE[min(kills, 5)], None),
                "swing": (swing, None),
            }
            details = {
                "kills": f"{eco_kills:.2f} eco-adjusted kills ({kills} raw) vs {expected:.2f} expected in a {situation}",
                "damage": f"{eco_damage:.0f} eco-adjusted damage ({damage} raw) vs "
                          f"{DAMAGE_PER_EXPECTED_KILL * expected:.0f} expected",
                "survival": "survived after fighting" if survived and fought else
                            "survived without fighting (no credit)" if survived else "died",
                "kast": "kill, assist, survived or traded" if kast else "no kill, assist, survival or trade",
                "multi": f"{kills}-kill fight",
                "swing": f"{swing * 100:+.1f}% CT round win probability",
            }
            sub, parts = {}, []
            for name, (label, _, weight, avg, spread) in COMPONENTS.items():
                x, fight_avg = values[name]
                sub[name] = 1 + (x - (avg if fight_avg is None else fight_avg)) / spread
                parts.append((label, weight * (sub[name] - 1),
                              f"{details[name]} -> {sub[name]:.2f} rating x {weight:.2f} weight"))
            score = sum(v for _, v, _ in parts)

            # ---- 6. plain-English reasons, for the viewer
            enemy_names = ", ".join(sorted(names.get(e, e) for e in enemies)) or "unknown"
            notes = [
                f"Anchored {site} " + ("alone" if not mates else
                                       "with " + ", ".join(names.get(m, m) for m in mates) + " nearby")
                + f"; {n_enemies} T{'s' if n_enemies > 1 else ''} were local ({enemy_names})."
            ]
            if not fought:
                notes.append(f"Never traded damage with a T before the bomb went down on {site}.")
            if kills == 0:
                notes.append(f"No kills. An average anchor gets {expected:.2f} in a {situation}.")
            else:
                notes.append(f"{kills} kill{'s' if kills > 1 else ''} vs the {expected:.2f} an average anchor "
                             f"gets in a {situation}"
                             + (f", worth {eco_kills:.2f} after adjusting for the Ts' equipment vs theirs."
                                if abs(eco_kills - kills) >= 0.05 else "."))
            if survived:
                notes.append("Survived the round." if fought else
                             "Survived, but never fought for the site, so it gets no survival credit.")
            else:
                notes.append(f"Died {held:.1f}s after first contact"
                             + (f", but was traded within {TRADE_WINDOW}s (counts for KAST, halves the swing cost)."
                                if traded else ", untraded."))
            if assists:
                notes.append(f"{assists} assist{'s' if assists > 1 else ''}, which counts for KAST.")
            notes.append(f"Round swing {swing * 100:+.1f}%: how much their kills"
                         + ("" if survived else " and death") + " moved the CTs' chance of winning the round"
                         + (", including the plant they let through." if not fought and plant_here else "."))
            notes.append(("Ts planted on " if plant_here else "Ts never planted on ") + site
                         + f"; CTs {'won' if winner == 'CT' else 'lost'} the round.")

            # ---- 7. playback clip for the viewer
            view_lo = max(ft, contact_tick - PRE_ROLL * TICKRATE)
            view_hi = min(et, (death_tick or et) + POST_ROLL * TICKRATE, contact_tick + MAX_CLIP * TICKRATE)
            review, pids = clip(view_lo, view_hi, anchor)
            review["events"] = sorted(review["events"] + [{"t": contact_tick, "type": "contact"}],
                                      key=lambda e: e["t"])

            eid = len(engagements)
            reviews.append(dict(
                review, id=eid, contact_tick=contact_tick, anchor_id=pids.index(anchor) if anchor in pids else None,
                spot=[num(spot[0]), num(spot[1])],
                players=[{"name": names.get(p, p), "team": "CT" if team_of.get(p) == CT_TEAM else "T",
                          "local": p in enemies or p in mates} for p in pids],
                notes=notes,
                breakdown=[{"label": l, "value": round(v, 2), "detail": d} for l, v, d in parts],
            ))

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
                "eco_kills": round(eco_kills, 2),
                "assists": assists,
                "damage": damage,
                "survived": survived,
                "traded": traded,
                "kast": kast,
                "swing_pct": round(swing * 100, 1),
                "time_held_s": round(held, 1),
                "planted_here": plant_here is not None,
                "round_won": winner == "CT",
                "expected_kills": round(expected, 2),
                "score": round(score, 2),
                **{f"r_{n}": round(v, 2) for n, v in sub.items()},
            })

        # the whole round, for watching the full match
        whole, pids = clip(ft, et)
        match.append(dict(whole, winner=winner, events=sorted(whole["events"], key=lambda e: e["t"]),
                          players=[{"name": names.get(p, p), "team": "CT" if team_of.get(p) == CT_TEAM else "T",
                                    "local": True} for p in pids],
                          anchors=[e["id"] for e in engagements if e["round"] == rnd]))

    viewer = {"map": map_name, "tickrate": TICKRATE, "local_radius": radius,
              "background": map_background(ticks, outline=not radar_files(map_name)), "reviews": reviews,
              "match": match}
    return map_name, pd.DataFrame(engagements), viewer


# ------------------------------------------------------------------- maps --
RADAR_TYPES = {".webp": "image/webp", ".png": "image/png"}


def radar_files(map_name):
    """(overview .txt, radar image) for this map from maps/, or None. WebP is preferred; it's much smaller."""
    base = os.path.join(MAPS_DIR, os.path.basename(map_name))
    image = next((base + ext for ext in RADAR_TYPES if os.path.isfile(base + ext)), None)
    return (base + ".txt", image) if image and os.path.isfile(base + ".txt") else None


@functools.lru_cache(maxsize=None)
def radar(map_name, embed=False):
    """The map's radar calibration and image (a /maps/ URL, or embedded for offline files), or None."""
    files = radar_files(map_name)
    if not files:
        return None
    with open(files[0], encoding="utf-8", errors="replace") as f:
        overview = f.read()
    value = lambda key: float(re.search(rf'"{key}"\s+"([-\d.]+)"', overview).group(1))

    def image(path):
        if not embed:
            return "/maps/" + os.path.basename(path)
        with open(path, "rb") as f:
            return f"data:{RADAR_TYPES[os.path.splitext(path)[1]]};base64," + base64.b64encode(f.read()).decode()

    info = {"pos_x": value("pos_x"), "pos_y": value("pos_y"), "scale": value("scale"), "image": image(files[1])}
    stem, ext = os.path.splitext(files[1])
    lower = stem + "_lower" + ext
    split = re.search(r'"lower".*?"AltitudeMax"\s+"([-\d.]+)"', overview, re.S)
    if os.path.isfile(lower) and split:  # two-level maps like Nuke
        info["lower"] = {"image": image(lower), "below": float(split.group(1))}
    return info


def map_background(ticks, outline=True, cell=48):
    """Map bounds and nav place labels, plus an outline traced from where players stood (for maps without a radar)."""
    labels = ticks.groupby("last_place_name")[["X", "Y", "Z"]].median()
    background = {
        "bounds": [num(ticks["X"].min()), num(ticks["Y"].min()), num(ticks["X"].max()), num(ticks["Y"].max())],
        "labels": [{"name": n, "x": num(r.X), "y": num(r.Y), "z": num(r.Z)} for n, r in labels.iterrows() if n],
    }
    if outline:
        cells = set(zip((ticks["X"] // cell).astype(int), (ticks["Y"] // cell).astype(int)))
        background.update(cell=cell, cells=[[int(x), int(y)] for x, y in cells])
    return background


# ----------------------------------------------------------------- output --
def viewer_data(viewer, eng):
    rows = {r["id"]: r for r in eng.drop(columns="steamid").to_dict("records")}
    return dict(viewer, reviews=[dict(rv, **rows[rv["id"]]) for rv in viewer["reviews"] if rv["id"] in rows])


def engagement_key(round_no, player):
    """Names an engagement across reprocessing (ids can shift; an anchor holds one site a round)."""
    return f"{int(round_no)}:{player}"


def to_json(data):
    return json.dumps(data, default=lambda o: o.item() if hasattr(o, "item") else str(o))


def viewer_html(data=None, match=None):
    """The viewer page; with data embedded it works offline, without it talks to the app server.
    match (every round, for watching the full match) is embedded too when given."""
    if data is not None:
        data = dict(data, radar=radar(data["map"], embed=True), **({"match": match} if match else {}))
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read().replace("__DATA__", to_json(data).replace("</", "<\\/"))


def summarise(eng):
    eng = eng[~eng["excluded"]]
    g = eng.groupby(["steamid", "player"])
    summary = pd.DataFrame({
        "engagements": g.size(),
        "kills": g["kills"].sum(),
        "exp_kills": g["expected_kills"].sum().round(2),
        "adr_on_site": g["damage"].mean().round(1),
        "kast_%": (g["kast"].mean() * 100).round(0),
        "survival_%": (g["survived"].mean() * 100).round(0),
        "swing_%": g["swing_pct"].mean().round(1),
        **{column: g[f"r_{n}"].mean().round(2) for n, (_, column, *_) in COMPONENTS.items()},
    })
    summary["anchor_rating"] = (1 + g["score"].mean()).round(2)
    return summary.reset_index().drop(columns="steamid").sort_values("anchor_rating", ascending=False)


def run(demo, site_places, player=None, out=None, viewer_path=None, csv=None, data_path=None, excluded=(),
        match_path=None):
    map_name, eng, viewer = analyse(demo, {k: set(v) for k, v in site_places.items()})
    match = viewer.pop("match")
    if match_path:
        write_json(match_path, match)
    if eng.empty:
        raise ValueError("No anchor engagements found. Try --extra-places if the map uses other place names.")
    if player:
        eng = eng[eng["player"].str.contains(player, case=False)].copy()
        if eng.empty:
            raise ValueError(f"No anchor engagements for '{player}'.")
    eng["excluded"] = [engagement_key(r, p) in excluded for r, p in zip(eng["round"], eng["player"])]
    write_results(map_name, eng, viewer_data(viewer, eng), out, viewer_path, csv, data_path, match=match)
    return map_name, len(eng)


def write_json(path, data):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(to_json(data))
    os.replace(path + ".tmp", path)  # the app may be reading it


def write_results(map_name, eng, data, out=None, viewer_path=None, csv=None, data_path=None, quiet=False,
                  match=None):
    """Rate the engagements that aren't excluded, then write the report, viewer data and CSV."""
    say = (lambda *a: None) if quiet else print
    cols = ["round", "player", "site", "situation", "contact", "kills", "assists",
            "damage", "survived", "traded", "swing_pct", "planted_here", "round_won", "score", "excluded"]
    summary = summarise(eng)
    n_out = int(eng["excluded"].sum())
    report = "\n".join([
        f"Map: {map_name}   anchor engagements: {len(eng)}" + (f" ({n_out} excluded from the ratings)" if n_out else ""),
        "",
        "=== Anchor ratings ===",
        summary.to_string(index=False),
        "",
        "=== Engagements ===",
        eng[cols].to_string(index=False),
    ])
    say("\n" + report)

    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        say(f"\nWrote report to {out}")

    data["ratings"] = summary.to_dict("records")
    excluded = dict(zip(eng["id"], eng["excluded"]))
    for rv in data["reviews"]:
        rv["excluded"] = bool(excluded.get(rv["id"], False))
    if viewer_path:
        with open(viewer_path, "w", encoding="utf-8") as f:
            f.write(viewer_html(data, match))
        say(f"\nWrote 2D review viewer to {viewer_path} - open it in a browser")
    if data_path:
        write_json(data_path, data)

    if csv:
        eng.to_csv(csv, index=False)
        say(f"\nWrote {len(eng)} engagements to {csv}")
    return data


DELETE_DEMOS = False  # --delete-demos: remove each .dem once its results are saved


def process_demo(path, site_places, player=None):
    """Analyse one demo into output/<demo name>/ and return that name."""
    stem = os.path.splitext(os.path.basename(path))[0]
    out_dir = os.path.join(OUTPUT_DIR, stem)
    os.makedirs(out_dir, exist_ok=True)
    meta_path = os.path.join(out_dir, "meta.json")
    if os.path.exists(meta_path):
        os.remove(meta_path)  # meta.json marks a finished demo; drop it while redoing one
    map_name, n = run(path, site_places, player, out=os.path.join(out_dir, "ratings.txt"),
                      csv=os.path.join(out_dir, "engagements.csv"), data_path=os.path.join(out_dir, "data.json"),
                      excluded=read_excluded(out_dir), match_path=os.path.join(out_dir, "match.json"))
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"name": stem, "map": map_name, "engagements": n, "processed": time.time()}, f)
    if DELETE_DEMOS:
        os.remove(path)
        print(f"Deleted {os.path.basename(path)} (its results are saved; it can't be reprocessed now)")
    return stem


def read_excluded(out_dir):
    """Engagements taken out of the ratings in the viewer; they stay out when the demo is reprocessed."""
    try:
        with open(os.path.join(out_dir, "excluded.json"), encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


EXCLUDE_LOCK = threading.Lock()


def set_excluded(name, review_id, exclude):
    """Take an engagement out of (or back into) a processed demo's ratings; returns the new viewer data."""
    data = load_demo(name)
    if data is None:
        return None
    out_dir = os.path.join(OUTPUT_DIR, name)
    with EXCLUDE_LOCK:
        eng = pd.read_csv(os.path.join(out_dir, "engagements.csv"))
        row = eng[eng["id"] == review_id]
        if row.empty:
            return None
        keys = read_excluded(out_dir)
        key = engagement_key(row["round"].iloc[0], row["player"].iloc[0])
        (keys.add if exclude else keys.discard)(key)
        with open(os.path.join(out_dir, "excluded.json"), "w", encoding="utf-8") as f:
            json.dump(sorted(keys), f)
        eng["excluded"] = [engagement_key(r, p) in keys for r, p in zip(eng["round"], eng["player"])]
        return write_results(data["map"], eng, data, out=os.path.join(out_dir, "ratings.txt"),
                             csv=os.path.join(out_dir, "engagements.csv"),
                             data_path=os.path.join(out_dir, "data.json"), quiet=True)


def pending_demos(force=False):
    """Demos in demos/ that have no finished output yet (or all of them with force)."""
    if not os.path.isdir(DEMOS_DIR):
        return []
    return [os.path.join(DEMOS_DIR, f) for f in sorted(os.listdir(DEMOS_DIR))
            if f.lower().endswith(".dem")
            and (force or not os.path.exists(os.path.join(OUTPUT_DIR, os.path.splitext(f)[0], "meta.json")))]


def batch(args, site_places):
    """Process every .dem in demos/ into output/<demo name>/."""
    demos = pending_demos(args.force)
    if not demos:
        sys.exit(f"No new .dem files found in:\n  {DEMOS_DIR}\n(use --force to redo finished ones)")
    done = 0
    for path in demos:
        print(f"\n### {os.path.basename(path)}")
        try:
            process_demo(path, site_places, args.player)
            done += 1
        except Exception as e:  # keep going so one bad demo doesn't stop the rest
            print(f"Failed on {os.path.basename(path)}: {e}")
    print(f"\nProcessed {done} demo(s). Results are in:\n  {OUTPUT_DIR}\nRun without --batch to review them.")


# ------------------------------------------------------------------ server --
JOBS = {}                  # job id -> {"id", "name", "status", "message", "demo"}
JOB_QUEUE = queue.Queue()  # demos are parsed one at a time; parsing is memory hungry


def queue_job(path):
    job = {"id": uuid.uuid4().hex[:8], "name": os.path.basename(path), "status": "queued", "message": "", "demo": None}
    JOBS[job["id"]] = job
    JOB_QUEUE.put((job, path))
    return job


def worker(site_places):
    while True:
        job, path = JOB_QUEUE.get()
        job["status"] = "parsing"
        print(f"\n### {job['name']}")
        try:
            job["demo"] = process_demo(path, site_places)
            job["status"] = "done"
        except ValueError as e:
            job.update(status="error", message=str(e))
        except Exception as e:  # report it in the viewer and keep serving
            job.update(status="error", message=f"Couldn't read this demo - is it a complete CS2 .dem file? ({e})")
        if job["status"] == "error":
            print(f"Failed on {job['name']}: {job['message']}")


def list_demos():
    demos = []
    for d in os.listdir(OUTPUT_DIR) if os.path.isdir(OUTPUT_DIR) else []:
        try:
            with open(os.path.join(OUTPUT_DIR, d, "meta.json"), encoding="utf-8") as f:
                demos.append(json.load(f))
        except (OSError, ValueError):
            continue
    return sorted(demos, key=lambda m: m.get("processed", 0), reverse=True)


def load_demo(name):
    """A processed demo's viewer data, or None for anything that isn't one."""
    path = os.path.join(OUTPUT_DIR, name, "data.json")
    if name != os.path.basename(name) or name in ("", ".", "..") or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_match(name):
    """A processed demo's full-match playback, or None (demos processed before it existed have none)."""
    try:
        with open(os.path.join(OUTPUT_DIR, name, "match.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class App(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json", headers=()):
        if not isinstance(body, (str, bytes)):
            body = to_json(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        squeeze = (len(body) > 4096 and not ctype.startswith("image/")
                   and "gzip" in self.headers.get("Accept-Encoding", ""))
        if squeeze:  # viewer data compresses to about a fifth
            body = gzip.compress(body, 6)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if squeeze:
            self.send_header("Content-Encoding", "gzip")
        if not any(h[0] == "Cache-Control" for h in headers):
            self.send_header("Cache-Control", "no-store")
        for header in headers:
            self.send_header(*header)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self.reply(200, viewer_html(), "text/html; charset=utf-8")
        if path == "/api/demos":
            return self.reply(200, list_demos())
        if path == "/api/jobs":
            return self.reply(200, list(JOBS.values()))
        if path.startswith("/maps/"):
            file = os.path.join(MAPS_DIR, os.path.basename(path))
            ctype = RADAR_TYPES.get(os.path.splitext(file)[1])
            if ctype and os.path.isfile(file):
                with open(file, "rb") as f:
                    return self.reply(200, f.read(), ctype, [("Cache-Control", "max-age=86400")])
        if path.startswith("/api/demos/"):
            name, _, action = path[len("/api/demos/"):].partition("/")
            data = load_demo(name)
            if data is not None and action == "":
                return self.reply(200, dict(data, radar=radar(data["map"])))
            if data is not None and action == "match":  # every round, loaded when the full match is watched
                match = os.path.join(OUTPUT_DIR, name, "match.json")
                if os.path.isfile(match):
                    with open(match, "rb") as f:
                        return self.reply(200, f.read())
            if data is not None and action == "export":  # standalone copy that works offline
                return self.reply(200, viewer_html(data, load_match(name)), "text/html; charset=utf-8",
                                  [("Content-Disposition", f'attachment; filename="{name}.html"')])
        self.reply(404, {"error": "Not found."})

    def do_POST(self):
        url = urlparse(self.path)
        m = re.fullmatch(r"/api/demos/([^/]+)/exclude", unquote(url.path))
        if m:
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                data = set_excluded(m.group(1), int(body["id"]), bool(body["excluded"]))
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "Bad request."})
            if data is None:
                return self.reply(404, {"error": "Not found."})
            return self.reply(200, {"ratings": data["ratings"],
                                    "excluded": [rv["id"] for rv in data["reviews"] if rv["excluded"]]})
        if url.path != "/api/upload":
            return self.reply(404, {"error": "Not found."})
        name = re.sub(r"[^\w.\- ]", "_", os.path.basename(parse_qs(url.query).get("name", [""])[0]))
        if not name.lower().endswith(".dem") or name.startswith("."):
            return self.reply(400, {"error": "Only .dem files can be uploaded."})
        if any(j["name"] == name and j["status"] in ("queued", "parsing") for j in list(JOBS.values())):
            return self.reply(409, {"error": f"{name} is already being processed."})
        remaining = int(self.headers.get("Content-Length") or 0)
        if not remaining:
            return self.reply(400, {"error": "The file is empty."})

        os.makedirs(DEMOS_DIR, exist_ok=True)
        dest = os.path.join(DEMOS_DIR, name)
        part = dest + ".part"
        with open(part, "wb") as f:
            while remaining:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            os.remove(part)
            return self.reply(400, {"error": "The upload was cut off."})
        os.replace(part, dest)
        self.reply(200, queue_job(dest))


def serve(site_places, port, browser=True):
    threading.Thread(target=worker, args=(site_places,), daemon=True).start()
    for path in pending_demos():  # demos dropped into demos/ by hand still get picked up
        queue_job(path)

    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), App)
            break
        except OSError:
            continue
    else:
        sys.exit(f"Couldn't find a free port from {port} to {port + 19}.")
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Anchor Review is running at {url}\nUpload demos in the browser. Press Ctrl+C (or close this window) to stop.")
    if browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Rate CS2 site anchors from a .dem file.")
    ap.add_argument("demo", nargs="?",
                    help="path to the .dem file (leave out to open the viewer app and upload demos there)")
    ap.add_argument("--batch", action="store_true",
                    help="process every new demo in the demos/ folder without opening the viewer")
    ap.add_argument("--force", action="store_true", help="--batch: also redo demos that already have output")
    ap.add_argument("--port", type=int, default=8765, help="viewer app port (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="start the viewer app without opening a browser")
    ap.add_argument("--player", help="only show this player (name substring)")
    ap.add_argument("--csv", help="write every engagement to this CSV file")
    ap.add_argument("--out", help="also write the printed report to this text file")
    ap.add_argument("--viewer", help="write a standalone 2D replay viewer of every engagement to this .html file")
    ap.add_argument("--delete-demos", action="store_true",
                    help="delete each .dem after processing to save space (it can't be reprocessed afterwards)")
    ap.add_argument("--extra-places", action="append", default=[],
                    help='treat extra nav place names as a site, e.g. "BombsiteB:Bench,Market"')
    args = ap.parse_args()
    global DELETE_DEMOS
    DELETE_DEMOS = args.delete_demos

    site_places = {k: set(v) for k, v in SITE_PLACES.items()}
    for spec in args.extra_places:
        site, _, places = spec.partition(":")
        site = site.replace("Bombsite", "").strip().upper()
        site_places.setdefault(site, set()).update(p.strip() for p in places.split(",") if p.strip())

    if args.demo:
        try:
            run(args.demo, site_places, args.player, args.out, args.viewer, args.csv)
        except ValueError as e:
            sys.exit(str(e))
    elif args.batch:
        batch(args, site_places)
    else:
        serve(site_places, args.port, not args.no_browser)


if __name__ == "__main__":
    main()
