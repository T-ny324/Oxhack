# Electric Nation raw data (not in git)

`GreenFlux3Minute.csv` (1.5 GB) and `GreenFlux15Minute.csv` (1.1 GB) are too large for
GitHub (100 MB file limit), so they are git-ignored. Get them from the team drive (ask
Hamid) or the Electric Nation data release (Western Power Distribution / National Grid
Electricity Distribution), and place them in this folder.

Only `GreenFlux3Minute.csv` is used. `data-explanation-meter-values.pdf` (kept in git)
describes the columns.

Rebuild the processed table (about 20 s, about 4.5 GB RAM):

    python -m v2g.ingest.electric_nation   # -> data/processed/sessions.parquet
