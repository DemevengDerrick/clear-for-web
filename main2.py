# app.py
import datetime
import io
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Tuple

import numpy as np
import streamlit as st
import matplotlib.pyplot as plt
from scipy.interpolate import griddata  # replaces deprecated matplotlib.mlab.griddata
from scipy.spatial import QhullError

import pandas as pd
import pydeck as pdk
from pyproj import Transformer

# ------------------------
# Shared parsing utilities
# ------------------------

DISCARDED_PREFIXES = (
    "SETUP",
    "STN_NO",
    "END",
    "SLOPE(TgtNo, TgtID, CfgNo, Hz, Vz, SDist, RefHt, Date, Ppm, ApplType, Flags)",
    "SLOPE (TgtNo, TgtID, CfgNo, Hz, Vz, SDist, RefHt, Date, Ppm, ApplType, Flags)",
    "SLOPE",
)

# Total station model → IDX layout (1: comma-separated, 2: tab-separated)
APPARATUS_MODES = {
    "TCR300": 1,
    "TCR400": 1,
    "TCR700": 1,
    "TCR800": 1,
    "TS06plus": 2,
    "Builders": 2,
}

# SLOPE(TgtNo, TgtID, CfgNo, Hz, Vz, SDist, RefHt, Date, Ppm, ApplType, Flags)
# → keep TgtID, Hz, Vz, SDist, RefHt
SLOPE_FIELD_COUNT = 11
SLOPE_KEEP = (1, 3, 4, 5, 6)


def _split_fields(line: str) -> List[str]:
    """
    Split one IDX record into fields, keeping empty fields in place so column
    positions stay fixed (e.g. a missing Date doesn't shift Ppm into its slot).
    Records are comma-separated; lines without commas are split on tabs.
    """
    s = line.strip().rstrip(";").strip()
    if not s:
        return []
    parts = s.split(",") if "," in s else s.split("\t")
    return [p.strip() for p in parts]


def _start_index(lines: List[str], marker: str) -> int:
    """
    Index of the first line after `marker`. If the marker is missing, fall back
    to the first SETUP block so the file isn't silently skipped.
    """
    for i, line in enumerate(lines):
        if line.startswith(marker):
            return i + 1
    for i, line in enumerate(lines):
        if line.strip().startswith("SETUP"):
            return i
    return len(lines)


def _should_discard(line: str) -> bool:
    l = line.lstrip("\t ")
    return any(l.startswith(pfx) for pfx in DISCARDED_PREFIXES)


def _transform_columns(fields: List[str]) -> str:
    """
    Transform one raw record into a single, comma-separated record:
      - 2 fields (STN_ID / INST_HT): keep the value
      - SLOPE row: keep TgtID, Hz, Vz, SDist, RefHt
    Anything else is treated as malformed and dropped.
    """
    if len(fields) == 2:
        return fields[1]

    if len(fields) < SLOPE_FIELD_COUNT:
        return ""  # malformed / too short

    return ",".join(fields[i] for i in SLOPE_KEEP)


def _is_number_like(s: str) -> bool:
    try:
        float(s.strip().strip(",;"))
        return True
    except Exception:
        return False

def _prefix_codes(lines: Iterable[str], cs: str, cr: str, cm: str) -> Iterator[Tuple[str, str]]:
    """
    Same n/m rule, but with look-ahead:
      - Merge two consecutive 'short' lines into one station record (name + height)
      - After emitting the station record, set m = n + 1 so the *next* line is code 3
      - Otherwise, preserve original behavior
    Yields (kind, line) where kind is "station", "reference" or "measure".
    """
    # normalize input lines once
    buf = [ (ln or "").strip() for ln in lines if (ln or "").strip() ]
    n = 0
    m = 0
    i = 0
    L = len(buf)

    while i < L:
        lin = buf[i]
        n += 1

        # STATION: short line = name (original heuristic)
        if len(lin) < 25:
            out = f"{cs},{lin}".rstrip(",")
            # look ahead for height line (also short and numeric)
            if i + 1 < L:
                nxt = buf[i + 1].strip()
                if len(nxt) < 25 and _is_number_like(nxt):
                    # consume height line
                    i += 1
                    n += 1
                    out = f"{out},{nxt}".rstrip(",")

            # emit station (with trailing comma) and set m so next line is code 3
            yield "station", out + ",\n"
            m = n + 1
            i += 1
            continue

        # REFERENCE: when n == m
        if n == m:
            yield "reference", f"{cr},{lin}".rstrip(",") + ",\n"
            i += 1
            continue

        # MEASURE: everything else
        yield "measure", f"{cm},{lin}".rstrip(",") + ",\n"
        i += 1


def clean_idx_text(
    raw_text: str,
    station_mode: int,
    code_station: str,
    code_reference: str,
    code_measure: str,
) -> Tuple[str, Dict[str, int]]:
    """
    Pure function: IDX → cleaned text (emulates your original process()).
    Returns (cleaned_text, counts) where counts has station/reference/measure totals.
    """
    if not code_station:
        code_station = "1"
    if not code_reference:
        code_reference = "3"
    if not code_measure:
        code_measure = "4"

    lines = raw_text.splitlines()

    # Station-dependent header marker (from your original)
    marker = "\t\t1,\t" if station_mode == 1 else "\t1\t"
    start = _start_index(lines, marker)

    # First pass: filter + transform
    transformed = []
    for line in lines[start:]:
        if _should_discard(line):
            continue
        fields = _split_fields(line)
        if not fields:
            continue
        out = _transform_columns(fields)
        if out:
            transformed.append(out)

    # Second pass: prefix codes using your n/m logic
    counts = {"station": 0, "reference": 0, "measure": 0}
    final_lines = []
    for kind, line in _prefix_codes(transformed, code_station, code_reference, code_measure):
        counts[kind] += 1
        final_lines.append(line)
    return "".join(final_lines), counts

# ------------------------
# Visualization utilities
# ------------------------

def parse_points_for_plot(raw_text: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[str], int]:
    """
    Read the DATABASE → POINTS(PointNo, PointID, East, North, Elevation, ...) table.
    Rows with a missing or non-numeric East/North/Elevation are skipped.
    Returns: x, y, Z, labels, number of skipped rows
    """
    xs, ys, zs, labels = [], [], [], []
    skipped = 0
    in_points = False

    for line in raw_text.splitlines():
        s = line.strip()
        if not in_points:
            if s.startswith("POINTS"):
                in_points = True
            continue
        if s.startswith("END"):
            break

        fields = _split_fields(line)
        if not fields:
            continue
        if len(fields) < 5:
            skipped += 1
            continue

        try:
            x = float(fields[2])
            y = float(fields[3])
            z = float(fields[4])
        except ValueError:
            skipped += 1
            continue

        xs.append(x)
        ys.append(y)
        zs.append(z)
        labels.append(fields[1].strip('"'))

    if not xs:
        raise ValueError(
            "No points with East/North/Elevation coordinates were found in this file. "
            "Make sure it contains a DATABASE → POINTS table with coordinates."
        )
    return np.array(xs), np.array(ys), np.array(zs), labels, skipped


def _surface_unavailable(ax, title: str):
    ax.set_title(title)
    ax.text(0.5, 0.5, "Needs at least 3\nnon-collinear points",
            ha="center", va="center", transform=ax.transAxes, color="gray")
    ax.set_xticks([])
    ax.set_yticks([])


def make_plots(x: np.ndarray, y: np.ndarray, Z: np.ndarray, label_list: List[str], grid_n: int = 120):
    """
    Draws four subplots similar to your original:
      1) 2D scatter + point labels
      2) 3D trisurface
      3) Contours (lines only)
      4) Colored contours with colorbar
    Uses scipy.interpolate.griddata for gridding. Surface panels show a notice
    instead of failing when the points can't be triangulated.
    """
    fig = plt.figure(figsize=(11, 8))

    # 1) 2D scatter
    ax1 = fig.add_subplot(221)
    ax1.scatter(x, y, marker='o')
    for i, txt in enumerate(label_list):
        ax1.annotate(txt, (x[i], y[i]), fontsize=8)
    ax1.set_title("2D Preview")
    ax1.set_xlabel("East (m)")
    ax1.set_ylabel("North (m)")

    # Build grid; 'linear' can fail with too few points, so fallback to 'nearest'
    ZI = None
    try:
        xi = np.linspace(np.min(x), np.max(x), grid_n)
        yi = np.linspace(np.min(y), np.max(y), grid_n)
        XI, YI = np.meshgrid(xi, yi)
        ZI = griddata(points=(x, y), values=Z, xi=(XI, YI), method="linear")
        if np.isnan(ZI).all():
            ZI = griddata(points=(x, y), values=Z, xi=(XI, YI), method="nearest")
    except (QhullError, ValueError):
        ZI = None

    # 2) 3D surface via triangulation
    ax2 = fig.add_subplot(222, projection='3d')
    try:
        ax2.plot_trisurf(x, y, Z, edgecolor='none', alpha=0.8)
        ax2.set_title("MNT Preview")
        ax2.set_xlabel("East (m)")
        ax2.set_ylabel("North (m)")
        ax2.set_zlabel("Height (m)")
    except (QhullError, ValueError, RuntimeError):
        fig.delaxes(ax2)
        _surface_unavailable(fig.add_subplot(222), "MNT Preview")

    ax3 = fig.add_subplot(223)
    ax4 = fig.add_subplot(224)
    if ZI is None:
        _surface_unavailable(ax3, "Contours")
        _surface_unavailable(ax4, "Colored Contours")
    else:
        # 3) Contours (lines)
        CS_lines = ax3.contour(XI, YI, ZI, levels=15, linewidths=0.6)
        ax3.clabel(CS_lines, inline=True, fontsize=8, fmt='%1.0f')
        ax3.set_title("Contours")
        ax3.set_xlabel("East (m)")
        ax3.set_ylabel("North (m)")

        # 4) Colored contours
        ax4.contour(XI, YI, ZI, levels=15, linewidths=0.5)
        pcm = ax4.pcolormesh(XI, YI, ZI, shading="auto")
        fig.colorbar(pcm, ax=ax4)
        ax4.set_title("Colored Contours")
        ax4.set_xlabel("East (m)")
        ax4.set_ylabel("North (m)")

    fig.tight_layout()
    return fig

def _looks_like_lonlat(x: np.ndarray, y: np.ndarray) -> bool:
    # crude but effective check
    return (
        np.all(np.isfinite(x)) and np.all(np.isfinite(y)) and
        np.min(x) >= -180 and np.max(x) <= 180 and
        np.min(y) >= -90  and np.max(y) <= 90
    )

def _to_wgs84(x: np.ndarray, y: np.ndarray, src_epsg: str) -> Tuple[np.ndarray, np.ndarray]:
    """Project (x,y) from src_epsg to EPSG:4326 (lon, lat)."""
    if src_epsg in ("EPSG:4326", "4326"):
        return x, y
    tr = Transformer.from_crs(src_epsg, "EPSG:4326", always_xy=True)
    lon, lat = tr.transform(x, y)
    return np.array(lon), np.array(lat)

def _basemap_layer(style: str) -> pdk.Layer:
    """Return a TileLayer for the chosen basemap."""
    if style == "OpenStreetMap":
        return pdk.Layer(
            "TileLayer",
            data="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            min_zoom=0, max_zoom=19, tile_size=256, opacity=1.0
        )
    elif style == "Satellite (Esri WorldImagery)":
        return pdk.Layer(
            "TileLayer",
            data="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            min_zoom=0, max_zoom=19, tile_size=256, opacity=1.0
        )
    else:  # fallback to OSM
        return pdk.Layer(
            "TileLayer",
            data="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            min_zoom=0, max_zoom=19, tile_size=256, opacity=1.0
        )

def _render_map(lon: np.ndarray, lat: np.ndarray, Z: np.ndarray, labels: List[str], annotate: bool, basemap_style: str = "OpenStreetMap"):
    df = pd.DataFrame({
        "lon": lon, "lat": lat, "z": Z, "label": labels if annotate else [""] * len(labels)
    })

    # Basemap goes first so point layer draws on top
    base = _basemap_layer(basemap_style)

    points = pdk.Layer(
        "ScatterplotLayer",
        df,
        get_position=["lon", "lat"],
        get_radius=4,
        get_fill_color=[0, 128, 255],
        pickable=True,
        radius_min_pixels=2,
        radius_max_pixels=10,
    )

    view = pdk.ViewState(
        latitude=float(df["lat"].median()),
        longitude=float(df["lon"].median()),
        zoom=13,
        bearing=0, pitch=0,
    )
    tooltip = {"html": "<b>{label}</b><br>Z: {z}<br>Lon: {lon}<br>Lat: {lat}"}

    deck = pdk.Deck(
        layers=[base, points],
        initial_view_state=view,
        map_style=None,  # we provide our own basemap tiles
        tooltip=tooltip,
    )
    st.pydeck_chart(deck)


# ------------------------
# Streamlit UI
# ------------------------

st.set_page_config(page_title="IDX Cleaner & Viz", page_icon="🧹", layout="wide")
st.title("🧹 IDX Cleaner & Visualizer")
st.caption("Clean total-station IDX exports into coded records, and visualize surveyed points.")

with st.sidebar:
    st.markdown("### ⚙️ Options")
    st.caption("Interpolation grid (for contours)")
    grid_n = st.slider("Grid resolution (N×N)", min_value=50, max_value=300, value=120, step=10)

# One upload shared by the Clean and Visualize tabs
uploaded = st.file_uploader(
    "Upload IDX file",
    type=["idx", "txt", "log"],
    help="The IDX export from your total station. It is used by both the Clean and Visualize tabs.",
)
raw_text = uploaded.getvalue().decode("utf-8", errors="ignore") if uploaded else ""
file_stem = Path(uploaded.name).stem if uploaded else "cleaned_result"

# Drop results from a previous file when a different one is uploaded
file_key = (uploaded.name, uploaded.size) if uploaded else None
if st.session_state.get("file_key") != file_key:
    st.session_state.file_key = file_key
    st.session_state.pop("clean", None)
    st.session_state.pop("viz", None)

tabs = st.tabs(["🔧 Clean & Export", "📈 Visualize", "💾 Download Clear Desktop"])

# -------------- Tab 1: Cleaning --------------
with tabs[0]:
    if uploaded:
        with st.expander("Raw file preview"):
            st.code(raw_text[:20000] + ("\n… (truncated)" if len(raw_text) > 20000 else ""), language=None)

    with st.form("clean_form", clear_on_submit=False):
        colA, colB, colC, colD = st.columns([1, 1, 1, 1])
        with colA:
            apparatus = st.selectbox(
                "Total station model",
                options=list(APPARATUS_MODES),
                help="The instrument used for the survey. TCR300/400/700/800 exports are "
                     "comma-separated; TS06plus/Builders exports are tab-separated.",
            )
        with colB:
            code_station = st.number_input("Code of Station", value=1, step=1, min_value=0,
                                           help="Code written in front of each station record")
        with colC:
            code_reference = st.number_input("Code of Reference", value=3, step=1, min_value=0,
                                             help="Code written in front of the first sight after each station")
        with colD:
            code_measure = st.number_input("Code of Measure", value=4, step=1, min_value=0,
                                           help="Code written in front of every other measurement")

        preview_lines = st.slider("Preview first N lines of cleaned output", min_value=10, max_value=500, value=120, step=10)
        submitted_clean = st.form_submit_button("Preview & Process", type="primary")

    if submitted_clean:
        if not uploaded:
            st.warning("Please upload a file first.")
        else:
            try:
                result, counts = clean_idx_text(
                    raw_text=raw_text,
                    station_mode=APPARATUS_MODES[apparatus],
                    code_station=str(int(code_station)),
                    code_reference=str(int(code_reference)),
                    code_measure=str(int(code_measure)),
                )
                st.session_state.clean = {
                    "text": result,
                    "counts": counts,
                    "filename": f"{file_stem}_cleaned.txt",
                }
            except Exception as e:
                st.session_state.pop("clean", None)
                st.error(f"Processing failed: {e}")

    # Drawn outside the submit branch so it survives reruns (e.g. clicking download)
    clean = st.session_state.get("clean")
    if clean:
        if not clean["text"]:
            st.warning(
                "No station or measurement records were found. Check that this is a total-station "
                "IDX export with SETUP/SLOPE sections, and that the right model is selected."
            )
        else:
            c = clean["counts"]
            st.success(
                f"Processing complete: {c['station']} stations, "
                f"{c['reference']} references, {c['measure']} measurements."
            )
            st.text_area("Cleaned preview", value="\n".join(clean["text"].splitlines()[:preview_lines]), height=300)
            st.download_button(
                label="⬇️ Download cleaned file",
                data=clean["text"].encode("utf-8"),
                file_name=clean["filename"],
                mime="text/plain",
                type="primary",
            )

# -------------- Tab 2: Visualization --------------
with tabs[1]:
    with st.form("viz_form", clear_on_submit=False):
        annotate = st.checkbox("Annotate points with labels", value=True)
        submitted_viz = st.form_submit_button("Generate Plots", type="primary")

    if submitted_viz:
        if not uploaded:
            st.warning("Please upload a file first.")
        else:
            try:
                x, y, Z, labels, skipped = parse_points_for_plot(raw_text)
                fig = make_plots(x, y, Z, labels if annotate else [""] * len(labels), grid_n=grid_n)
                png = io.BytesIO()
                fig.savefig(png, format="png", dpi=120)
                plt.close(fig)

                # store results so the plots, summary and map survive reruns
                st.session_state.viz = {
                    "x": x, "y": y, "Z": Z, "labels": labels, "annotate": annotate,
                    "skipped": skipped, "png": png.getvalue(),
                }
            except Exception as e:
                st.session_state.pop("viz", None)
                st.error(f"Visualization failed: {e}")

    viz = st.session_state.get("viz")
    if viz:
        x, y, Z = viz["x"], viz["y"], viz["Z"]
        labels, annotate = viz["labels"], viz["annotate"]

        st.image(viz["png"], width="stretch")
        st.download_button(
            "⬇️ Download plots (PNG)",
            data=viz["png"],
            file_name=f"{file_stem}_plots.png",
            mime="image/png",
        )

        # Quick stats
        st.subheader("Summary")
        c1, c2, c3 = st.columns(3)
        c1.metric("Points", f"{len(x)}")
        c2.metric("Z min / max", f"{np.min(Z):.3f} / {np.max(Z):.3f}")
        c3.metric("X span / Y span", f"{(np.max(x)-np.min(x)):.2f} / {(np.max(y)-np.min(y)):.2f}")
        if viz["skipped"]:
            st.caption(f"{viz['skipped']} point(s) skipped because East, North or Elevation was missing.")

    # --- Mapping controls (always visible after first run) ---
    st.markdown("### 🗺️ Interactive Map")
    if not viz:
        st.info("Upload data and click **Generate Plots** first to enable the map.")
    else:
        show_map = st.checkbox(
            "Show interactive map",
            value=False,
            help="Plots points on a basemap (reprojects to WGS84)."
        )

        if show_map:
            auto_is_lonlat = _looks_like_lonlat(x, y)
            auto_label = "Auto-detect (→ " + ("WGS84 lon/lat" if auto_is_lonlat else "UTM 32N") + ")"
            crs_options = {
                auto_label: "EPSG:4326" if auto_is_lonlat else "EPSG:32632",
                "EPSG:4326 (lon/lat)": "EPSG:4326",
                "EPSG:32632 (UTM 32N)": "EPSG:32632",
                "EPSG:32633 (UTM 33N)": "EPSG:32633",
                "Custom EPSG code…": None,
            }
            col1, col2 = st.columns([1.2, 1])
            with col1:
                crs_choice = st.selectbox("Coordinate Reference System (CRS)", list(crs_options), index=0)
            with col2:
                custom_epsg = ""
                if crs_choice == "Custom EPSG code…":
                    custom_epsg = st.text_input("Enter EPSG code (e.g., 32736)", value="")

            basemap_style = st.selectbox(
                "Basemap",
                ["OpenStreetMap", "Satellite (Esri WorldImagery)"],
                index=0,
                help="OpenStreetMap is vector-like streets; Satellite uses Esri WorldImagery."
            )

            src_epsg = crs_options[crs_choice]
            if src_epsg is None:
                code = custom_epsg.strip().upper().removeprefix("EPSG:").strip()
                src_epsg = f"EPSG:{code}" if code else None

            if src_epsg is None:
                st.info("Enter an EPSG code to display the map.")
            else:
                try:
                    # Reproject and render
                    lon, lat = _to_wgs84(x, y, src_epsg)
                    if np.any(~np.isfinite(lon)) or np.any(~np.isfinite(lat)):
                        raise ValueError("Non-finite coordinates after reprojection.")
                    _render_map(lon, lat, Z, labels, annotate, basemap_style=basemap_style)
                except Exception as map_err:
                    st.warning(f"Couldn’t render map with CRS='{src_epsg}'. Tip: confirm your EPSG code. Details: {map_err}")


# -------------- Tab 3: Download Clear Desktop --------------
with tabs[2]:
    st.subheader("Download Clear (Desktop)")
    st.caption("Click the button to download the Clear desktop installer.")

    # Your shared link converted to a direct-download URL
    direct_url = "https://drive.google.com/uc?export=download&id=1JukA4QQy2akC-GKhMemc2RvRpk2rdspK"

    st.link_button("⬇️ Download Clear Desktop", direct_url, width="stretch")

# -------------- Footer --------------
st.divider()
st.markdown(
    f"<div style='text-align:center; color:gray; font-size:0.85em;'>"
    f"© {datetime.date.today().year} Derrick Demeveng. All rights reserved.</div>",
    unsafe_allow_html=True,
)
