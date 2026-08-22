"""Fama-French factor returns from the Ken French data library.

Knows the library's CSV shapes and nothing else -- no SQL, no HTML. Factor
returns are a market-wide series with no ticker grain, so they do not belong
in `metrics` and are cached as their own gzipped CSV.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "ff_factors.csv.gz"

BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
FF5_URL = f"{BASE}/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
UMD_URL = f"{BASE}/F-F_Momentum_Factor_daily_CSV.zip"

# A data line is a bare YYYYMMDD followed by comma-separated numbers. The
# library wraps every file in a copyright preamble and sometimes appends a
# second block, so anchoring on this shape is what keeps the parser honest.
_DATA_RE = re.compile(r"^\s*(\d{8})\s*,(.*)$")


def parse_french_csv(text: str) -> pd.DataFrame:
    """Parse one Ken French CSV into decimals indexed by ISO date."""
    header: list[str] | None = None
    rows: list[list] = []

    for line in text.splitlines():
        match = _DATA_RE.match(line)
        if match is None:
            # The last comma-bearing line before the data is the header.
            if "," in line and not line.strip().startswith("Copyright"):
                parts = [p.strip() for p in line.split(",")]
                if parts and parts[0] == "":
                    header = parts[1:]
            continue
        stamp, rest = match.groups()
        values = [float(v) / 100.0 for v in rest.split(",") if v.strip() != ""]
        rows.append([f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"] + values)

    if header is None or not rows:
        raise ValueError("no Fama-French data rows found")

    width = len(rows[0]) - 1
    frame = pd.DataFrame(rows, columns=["date"] + header[:width])
    return frame.set_index("date").astype(float)


def _download(url: str) -> str:
    request = Request(url, headers={"User-Agent": "fundamentals_tracker"})
    with urlopen(request, timeout=60) as response:
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        return archive.read(name).decode("latin-1")


def fetch_factors(cache: Path | None = None) -> pd.DataFrame:
    """Download FF5 + momentum and join them on date."""
    ff5 = parse_french_csv(_download(FF5_URL))
    umd = parse_french_csv(_download(UMD_URL))
    umd.columns = ["UMD"] * len(umd.columns)
    joined = ff5.join(umd[["UMD"]], how="inner")

    path = CACHE_PATH if cache is None else Path(cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    joined.to_csv(path, compression="gzip")
    return joined


def load_cached(path: Path | None = None) -> pd.DataFrame:
    """Read the cached factor file. Round-trip precision, per repo convention."""
    target = CACHE_PATH if path is None else Path(path)
    return pd.read_csv(target, index_col=0, float_precision="round_trip")
