# Radar maps

Radar overviews for the Premier map pool. They're used by the 2D viewer instead of the outline traced from player positions.

- `<map>.png` is the radar image (1024x1024), and `<map>_lower.png` is the lower level of a two-level map (Nuke).
- `<map>.txt` is Valve's overview file. `pos_x`/`pos_y` give the image's top-left corner in world units, and `scale` is world units per pixel. On Nuke, `verticalsections` gives the altitude where the lower level starts.

The images and overview files are Valve's, extracted from the CS2 game files by
[MurkyYT/cs2-map-icons](https://github.com/MurkyYT/cs2-map-icons) (`images/radars/` and `data/radar_info/`), downloaded 2026-10-28.

To add a map, drop its `<map>.png` and `<map>.txt` in this folder, named after the demo's map (for example `de_train`). Then reprocess its demos.
