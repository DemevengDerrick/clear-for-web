# app.py
import io
from pathlib import Path
from typing import Iterable, List, Tuple, Optional

import numpy as np
import streamlit as st
import matplotlib.pyplot as plt
from scipy.interpolate import griddata  # replaces deprecated matplotlib.mlab.griddata

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

def _skip_until_marker(lines: Iterable[str], marker: str) -> Iterable[str]:
    """Skip until a line startswith(marker), then return remaining lines iterator."""
    for line in lines:
        if line.startswith(marker):
            break
    return lines

def _should_discard(line: str) -> bool:
    l = line.lstrip("\t ")
    return any(l.startswith(pfx) for pfx in DISCARDED_PREFIXES)

def _transform_columns(tokens: List[str], station_mode: int) -> str:
    """
    Transform one raw line into a single, comma-separated record (no leading/trailing blanks),
    and remove stray commas/semicolons that were causing empty fields.
    """
    # clean each token once up front
    tokens = [t.strip().strip(",;") for t in tokens if t.strip().strip(",;") != ""]

    if len(tokens) == 2:
        # Example: ['1,"S1",', '1.635000;,'] → keep the second, cleaned (no semicolon)
        return tokens[1]

    if len(tokens) < 11:
        return ""  # malformed / too short

    t = tokens[:]
    try:
        del t[7:11]
        del t[2]
        del t[0]
    except IndexError:
        return ""

    # Always comma-join; we already removed empties/stray punctuation
    joined = ",".join(t).strip()
    return joined


def _is_number_like(s: str) -> bool:
    try:
        float(s.strip().strip(",;"))
        return True
    except Exception:
        return False

def _prefix_codes(lines: Iterable[str], cs: str, cr: str, cm: str) -> Iterable[str]:
    """
    Same n/m rule, but with look-ahead:
      - Merge two consecutive 'short' lines into one station record (name + height)
      - After emitting the station record, set m = n + 1 so the *next* line is code 3
      - Otherwise, preserve original behavior
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
            merged = False
            if i + 1 < L:
                nxt = buf[i + 1].strip()
                if len(nxt) < 25 and _is_number_like(nxt):
                    # consume height line
                    i += 1
                    n += 1
                    out = f"{out},{nxt}".rstrip(",")
                    merged = True

            # emit station (with trailing comma) and set m so next line is code 3
            yield out + ",\n"
            m = n + 1
            i += 1
            continue

        # REFERENCE: when n == m
        if n == m:
            yield f"{cr},{lin}".rstrip(",") + ",\n"
            i += 1
            continue

        # MEASURE: everything else
        yield f"{cm},{lin}".rstrip(",") + ",\n"
        i += 1


def clean_idx_text(
    raw_text: str,
    station_mode: int,
    code_station: str,
    code_reference: str,
    code_measure: str,
) -> str:
    """Pure function: IDX → cleaned text (emulates your original process())."""
    if not code_station:
        code_station = "1"
    if not code_reference:
        code_reference = "3"
    if not code_measure:
        code_measure = "4"

    src = io.StringIO(raw_text)

    # Station-dependent header marker (from your original)
    marker = "\t\t1,\t" if station_mode == 1 else "\t1\t"
    remaining = _skip_until_marker(src, marker)

    # First pass: filter + transform
    transformed = []
    for line in remaining:
        if _should_discard(line):
            continue
        tokens = line.split()
        if not tokens:
            continue
        out = _transform_columns(tokens, station_mode)
        if out:
            transformed.append(out)

    # Second pass: prefix codes using your n/m logic
    final_lines = list(_prefix_codes(transformed, code_station, code_reference, code_measure))
    return "".join(final_lines)

# ------------------------
# Visualization utilities
# ------------------------

def parse_points_for_plot(raw_text: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[str]]:
    """
    Re-implements your Tkinter plot() parsing:
      - Skip until a line starts with "DATABASE"
      - Then read lines until a line starts with "\t\tTHEMINFO"
      - Skip lines starting with "\tPOINTS("
      - Clean, split, strip commas, then:
          del [5:9], del [0:1]  -> keeping columns as you did
          x = float(liste[1]), y = float(liste[2]), Z = float(liste[3]), label = liste[0]
    Returns: x, y, Z, labels
    """
    src = io.StringIO(raw_text)

    # Skip to "DATABASE"
    for line in src:
        if line.startswith("DATABASE"):
            break

    xs, ys, zs, labels = [], [], [], []

    for line in src:
        if line.startswith("\t\tTHEMINFO"):
            break
        if line.startswith("\tPOINTS("):
            continue

        line = line.strip().strip(" ")
        if not line:
            continue
        parts = line.split()

        # strip trailing commas around tokens
        cleaned = [p.strip(", ") for p in parts]
        if len(cleaned) < 9:
            # needs to be at least long enough for deletions below
            continue

        # Your original deletions:
        # del liste[5:9], del liste[0:1]
        t = cleaned[:]
        try:
            del t[5:9]
            del t[0:1]
        except Exception:
            continue

        # Expect at least 4 items for indexing [0..3]
        if len(t) < 4:
            continue

        # t[0] is label, t[1]=x, t[2]=y, t[3]=Z (from your code)
        lab, sx, sy, sz = t[0], t[1], t[2], t[3]
        if sx == "" or sy == "":
            continue
        try:
            x = float(sx)
            y = float(sy)
            z = float(sz)
        except ValueError:
            continue

        xs.append(x)
        ys.append(y)
        zs.append(z)
        labels.append(lab.strip('"'))

    if not xs:
        raise ValueError("No valid points parsed from IDX content. Check file structure or parsing rules.")
    return np.array(xs), np.array(ys), np.array(zs), labels

def make_plots(x: np.ndarray, y: np.ndarray, Z: np.ndarray, label_list: List[str], grid_n: int = 120):
    """
    Draws four subplots similar to your original:
      1) 2D scatter + point labels
      2) 3D trisurface
      3) Contours (lines only)
      4) Colored contours with colorbar
    Uses scipy.interpolate.griddata for gridding.
    """
    # Build grid
    xi = np.linspace(np.min(x), np.max(x), grid_n)
    yi = np.linspace(np.min(y), np.max(y), grid_n)
    XI, YI = np.meshgrid(xi, yi)

    # Interpolate; 'linear' can fail with too few points, so fallback to 'nearest'
    ZI = griddata(points=(x, y), values=Z, xi=(XI, YI), method="linear")
    if np.isnan(ZI).all():
        ZI = griddata(points=(x, y), values=Z, xi=(XI, YI), method="nearest")

    fig = plt.figure(figsize=(11, 8))

    # 1) 2D scatter
    ax1 = fig.add_subplot(221)
    ax1.scatter(x, y, marker='o')
    for i, txt in enumerate(label_list):
        ax1.annotate(txt, (x[i], y[i]), fontsize=8)
    ax1.set_title("2D Preview")
    ax1.set_xlabel("East (m)")
    ax1.set_ylabel("North (m)")

    # 2) 3D surface via triangulation
    ax2 = fig.add_subplot(222, projection='3d')
    ax2.plot_trisurf(x, y, Z, edgecolor='none', alpha=0.8)
    ax2.set_title("MNT Preview")
    ax2.set_xlabel("East (m)")
    ax2.set_ylabel("North (m)")
    ax2.set_zlabel("Height (m)")

    # 3) Contours (lines)
    ax3 = fig.add_subplot(223)
    CS_lines = ax3.contour(XI, YI, ZI, levels=15, linewidths=0.6)
    ax3.clabel(CS_lines, inline=True, fontsize=8, fmt='%1.0f')
    ax3.set_title("Contours")
    ax3.set_xlabel("East (m)")
    ax3.set_ylabel("North (m)")

    # 4) Colored contours
    ax4 = fig.add_subplot(224)
    CS = ax4.contour(XI, YI, ZI, levels=15, linewidths=0.5)
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
    st.pydeck_chart(deck, use_container_width=True)


# ------------------------
# Streamlit UI
# ------------------------

st.set_page_config(page_title="IDX Cleaner & Viz", page_icon="🧹", layout="wide")
st.title("🧹 IDX Cleaner & Visualizer")

tabs = st.tabs(["🔧 Clean & Export", "📈 Visualize", "💾 Download Clear Desktop"])

with st.sidebar:
    st.markdown("### ⚙️ Options")
    st.caption("Configure defaults used across tabs.")
    default_codes = {
        "station": st.text_input("Default CodeStation", value="1"),
        "reference": st.text_input("Default CodeReference", value="3"),
        "measure": st.text_input("Default CodeMesur", value="4"),
    }
    st.divider()
    st.caption("Interpolation grid (for contours)")
    grid_n = st.slider("Grid resolution (N×N)", min_value=50, max_value=300, value=120, step=10)

# Keep last results for re-download
if "last_output" not in st.session_state:
    st.session_state.last_output = ""
if "last_filename" not in st.session_state:
    st.session_state.last_filename = "cleaned_result.txt"

# -------------- Tab 1: Cleaning --------------
with tabs[0]:
    with st.form("clean_form", clear_on_submit=False):
        uploaded_clean = st.file_uploader("Upload IDX/TXT file", type=["idx", "txt", "log"], key="u_clean")
        colA, colB, colC, colD = st.columns([1, 1, 1, 1])
        with colA:
            station_mode = st.radio("Code of Apparatus (Total Station Model)", options=[1, 2], index=0, horizontal=True,
                                    help="For TCR300/400/700/800, TS → 1; Builders/TS06plus → 2")
        with colB:
            code_station = st.text_input("Code of Station", value=default_codes["station"])
        with colC:
            code_reference = st.text_input("Code of Reference", value=default_codes["reference"])
        with colD:
            code_measure = st.text_input("Code of Measure", value=default_codes["measure"])

        preview_lines = st.slider("Preview first N lines of cleaned output", min_value=10, max_value=500, value=120, step=10)
        submitted_clean = st.form_submit_button("Preview & Process")

    if submitted_clean:
        if not uploaded_clean:
            st.warning("Please upload a file first.")
        else:
            try:
                text = uploaded_clean.read().decode("utf-8", errors="ignore")
                result = clean_idx_text(
                    raw_text=text,
                    station_mode=int(station_mode),
                    code_station=code_station.strip(),
                    code_reference=code_reference.strip(),
                    code_measure=code_measure.strip(),
                )
                st.session_state.last_output = result
                st.session_state.last_filename = f"{Path(uploaded_clean.name).stem}_cleaned.txt"

                st.text_area("Cleaned preview", value="\n".join(result.splitlines()[:preview_lines]), height=300)
                st.download_button(
                    label="⬇️ Download cleaned file",
                    data=result.encode("utf-8"),
                    file_name=st.session_state.last_filename,
                    mime="text/plain",
                )
                st.success("Processing complete.")
            except Exception as e:
                st.error(f"Processing failed: {e}")

    if st.session_state.last_output:
        with st.expander("Last cleaned output (cached)"):
            txt = st.session_state.last_output
            st.code(txt[:4000] + ("\n… (truncated)" if len(txt) > 4000 else ""))

# -------------- Tab 2: Visualization --------------
with tabs[1]:
    with st.form("viz_form", clear_on_submit=False):
        uploaded_viz = st.file_uploader("Upload IDX/TXT file for visualization", type=["idx", "txt", "log"], key="u_viz")
        annotate = st.checkbox("Annotate points with labels", value=True)
        submitted_viz = st.form_submit_button("Generate Plots")

    if submitted_viz:
        if not uploaded_viz:
            st.warning("Please upload a file first.")
        else:
            try:
                text = uploaded_viz.read().decode("utf-8", errors="ignore")
                x, y, Z, labels = parse_points_for_plot(text)
                fig = make_plots(x, y, Z, labels if annotate else [""] * len(labels), grid_n=grid_n)
                st.pyplot(fig, clear_figure=True)

                # ✅ store arrays so the map UI (outside) can use them later
                st.session_state.viz = {
                    "x": x, "y": y, "Z": Z, "labels": labels, "annotate": annotate
                }

                # Quick stats
                st.subheader("Summary")
                c1, c2, c3 = st.columns(3)
                c1.metric("Points", f"{len(x)}")
                c2.metric("Z min / max", f"{np.min(Z):.3f} / {np.max(Z):.3f}")
                c3.metric("X span / Y span", f"{(np.max(x)-np.min(x)):.2f} / {(np.max(y)-np.min(y)):.2f}")

            except Exception as e:
                st.error(f"Visualization failed: {e}")
    
    # --- Mapping controls (outside submitted_viz; always visible after first run) ---
    st.markdown("### 🗺️ Interactive Map")
    if "viz" not in st.session_state:
        st.info("Upload data and click **Generate Plots** first to enable the map.")
    else:
        x = st.session_state.viz["x"]
        y = st.session_state.viz["y"]
        Z = st.session_state.viz["Z"]
        labels = st.session_state.viz["labels"]
        annotate = st.session_state.viz["annotate"]

        show_map = st.checkbox(
            "Show interactive map",
            value=False,
            help="Plots points on a basemap (reprojects to WGS84)."
        )

        if show_map:
            auto_is_lonlat = _looks_like_lonlat(x, y)
            col1, col2 = st.columns([1.2, 1])
            with col1:
                crs_choice = st.selectbox(
                    "Coordinate Reference System (CRS)",
                    [
                        "Auto-detect (use EPSG:4326 if it looks like lon/lat)",
                        "EPSG:4326 (lon/lat)",
                        "EPSG:32632 (UTM 32N)",
                        "EPSG:32633 (UTM 33N)",
                        "Custom EPSG code…",
                    ],
                    index=0 if auto_is_lonlat else 2
                )
            with col2:
                custom_epsg = ""
                if crs_choice == "Custom EPSG code…":
                    custom_epsg = st.text_input("Enter EPSG code (e.g., 32736)", value="")

            try:
                if crs_choice == "Auto-detect (use EPSG:4326 if it looks like lon/lat)":
                    src_epsg = "EPSG:4326" if auto_is_lonlat else "EPSG:32632"
                elif crs_choice == "EPSG:4326 (lon/lat)":
                    src_epsg = "EPSG:4326"
                elif crs_choice == "EPSG:32632 (UTM 32N)":
                    src_epsg = "EPSG:32632"
                elif crs_choice == "EPSG:32633 (UTM 33N)":
                    src_epsg = "EPSG:32633"
                else:
                    src_epsg = f"EPSG:{custom_epsg.strip()}" if custom_epsg.strip() else "EPSG:4326"

                # Basemap choice
                basemap_style = st.selectbox(
                    "Basemap",
                    ["OpenStreetMap", "Satellite (Esri WorldImagery)"],
                    index=0,
                    help="OpenStreetMap is vector-like streets; Satellite uses Esri WorldImagery."
                )

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

    st.link_button("⬇️ Download Clear Desktop", direct_url, use_container_width=True)

