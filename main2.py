import datetime
import io
import json
import re
import urllib.parse
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
    "SLOPE",  # also covers the SLOPE(...) column header
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


# ------------------------
# Survey network utilities
# ------------------------

def _angle_full_circle(raw_text: str) -> float:
    """Full-circle value of the file's ANGULAR unit (GRADS → 400, DEGREES → 360)."""
    for line in raw_text.splitlines():
        s = line.strip().upper()
        if s.startswith("ANGULAR"):
            if "DEG" in s:
                return 360.0
            if "MIL" in s:
                return 6400.0
            return 400.0
    return 400.0


def _to_float(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _point_coordinates(raw_text: str) -> Tuple[Dict[str, Tuple[float, float]], Dict[str, Tuple[float, float]]]:
    """
    Read East/North for every POINTS row that has them.
    Returns (by PointNo, by PointID). For a PointID measured several times,
    a FIX row wins over MEAS rows; otherwise the last valid row is kept.
    """
    by_no, by_id, id_is_fix = {}, {}, {}
    in_points = False
    for line in raw_text.splitlines():
        s = line.strip()
        if not in_points:
            if s.startswith("POINTS"):
                in_points = True
            continue
        if s.startswith("END"):
            break
        f = _split_fields(line)
        if len(f) < 4:
            continue
        x, y = _to_float(f[2]), _to_float(f[3])
        if x is None or y is None:
            continue
        no, pid = f[0], f[1].strip('"').strip()
        is_fix = f[-1].upper() == "FIX"
        by_no[no] = (x, y)
        if is_fix or not id_is_fix.get(pid, False):
            by_id[pid] = (x, y)
            id_is_fix[pid] = id_is_fix.get(pid, False) or is_fix
    return by_no, by_id


def parse_network(raw_text: str) -> Dict:
    """
    Build the station/reference network from the SETUP and SLOPE sections.
      - nodes: {point_id: {"x", "y", "role": "station" | "reference"}}
      - edges: one per measured direction (from_id → to_id) between network
        points, with mean horizontal distance, Hz reading and the horizontal
        angle measured from the setup's reference direction
      - details: (from_id, to_id) sights to non-network points
    """
    full_circle = _angle_full_circle(raw_text)
    by_no, by_id = _point_coordinates(raw_text)

    def coords(no: str, pid: str):
        return by_no.get(no) or by_id.get(pid)

    # 1) Collect setups and their sights
    setups = []
    cur = None
    in_slope = False
    for line in raw_text.splitlines():
        s = line.strip()
        if s.startswith("SETUP"):
            cur = {"no": "", "id": "", "sights": []}
            setups.append(cur)
            in_slope = False
        elif cur is not None and s.startswith("STN_NO"):
            cur["no"] = _split_fields(line)[-1]
        elif cur is not None and s.startswith("STN_ID"):
            cur["id"] = _split_fields(line)[-1].strip('"').strip()
        elif s.startswith("SLOPE"):
            in_slope = cur is not None
        elif s.startswith("END SLOPE"):
            in_slope = False
        elif in_slope:
            f = _split_fields(line)
            if len(f) >= SLOPE_FIELD_COUNT:
                cur["sights"].append({
                    "no": f[0], "id": f[1].strip('"').strip(),
                    "hz": _to_float(f[3]), "vz": _to_float(f[4]), "sd": _to_float(f[5]),
                })

    setups = [su for su in setups if su["id"]]
    station_ids = {su["id"] for su in setups}
    reference_ids = {su["sights"][0]["id"] for su in setups if su["sights"]}
    network_ids = station_ids | reference_ids

    # 2) Nodes with coordinates
    nodes = {}
    for su in setups:
        xy = coords(su["no"], su["id"])
        if xy:
            nodes[su["id"]] = {"x": xy[0], "y": xy[1], "role": "station"}
    for su in setups:
        for sg in su["sights"]:
            if sg["id"] in network_ids and sg["id"] not in nodes:
                xy = coords(sg["no"], sg["id"])
                if xy:
                    nodes[sg["id"]] = {"x": xy[0], "y": xy[1], "role": "reference"}

    # 3) Edges between network points, merging repeated sights of one direction
    edges: Dict[Tuple[str, str], Dict] = {}
    details = set()
    missing = set()
    for su in setups:
        if not su["sights"]:
            continue
        ref_hz = su["sights"][0]["hz"]
        for k, sg in enumerate(su["sights"]):
            a, b = su["id"], sg["id"]
            if a == b:
                continue
            if b not in network_ids:
                if a in nodes:
                    xy = coords(sg["no"], b)
                    if xy:
                        details.add((a, b, xy))
                continue
            if a not in nodes or b not in nodes:
                missing.add((a, b))
                continue
            e = edges.setdefault((a, b), {"from": a, "to": b, "hd": [], "hz": sg["hz"],
                                          "angle": None, "is_reference": False})
            if k == 0:
                e["is_reference"] = True
            elif not e["is_reference"] and e["angle"] is None and sg["hz"] is not None and ref_hz is not None:
                e["angle"] = (sg["hz"] - ref_hz) % full_circle
            if sg["sd"] and sg["vz"] is not None:
                e["hd"].append(sg["sd"] * np.sin(sg["vz"] * 2 * np.pi / full_circle))

    for e in edges.values():
        e["hd"] = float(np.mean(e["hd"])) if e["hd"] else None

    return {
        "nodes": nodes,
        "edges": list(edges.values()),
        "details": sorted(details),
        "missing": sorted(missing),
        "angle_unit": {400.0: "gon", 360.0: "°", 6400.0: "mil"}[full_circle],
    }


def make_network_figure(net: Dict, show_distances: bool = True, show_angles: bool = False,
                        show_details: bool = False):
    """
    Interactive (zoom/pan) plot of the network: stations, references and arrows
    for each measured direction. When A→B and B→A were both measured, each
    arrow is shifted to its own right-hand side so the pair doesn't overlap.
    """
    import plotly.graph_objects as go

    nodes, edges = net["nodes"], net["edges"]
    if not nodes:
        raise ValueError("No stations with coordinates were found in this file.")
    xs = [n["x"] for n in nodes.values()]
    ys = [n["y"] for n in nodes.values()]
    lengths = [np.hypot(nodes[e["to"]]["x"] - nodes[e["from"]]["x"], nodes[e["to"]]["y"] - nodes[e["from"]]["y"])
               for e in edges]
    lengths = [v for v in lengths if v > 0]
    typical = float(np.median(lengths)) if lengths else max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    gap = typical * 0.04          # spacing between forward and backward arrows
    label_px = 10                 # distance of the label from its arrow, in pixels
    pairs = {(e["from"], e["to"]) for e in edges}

    fig = go.Figure()

    # Sights to detail points (thin, no arrowheads, to keep the plot readable)
    if show_details and net["details"]:
        dx, dy = [], []
        for a, _, (bx, by) in net["details"]:
            dx += [nodes[a]["x"], bx, None]
            dy += [nodes[a]["y"], by, None]
        fig.add_trace(go.Scatter(x=dx, y=dy, mode="lines", line=dict(color="lightgray", width=1),
                                 name="Detail sights", hoverinfo="skip"))
        bx = [p[2][0] for p in net["details"]]
        by = [p[2][1] for p in net["details"]]
        fig.add_trace(go.Scatter(x=bx, y=by, mode="markers", marker=dict(size=4, color="gray"),
                                 name="Detail points", text=[p[1] for p in net["details"]],
                                 hovertemplate="%{text}<extra></extra>"))

    annotations = []
    hover_x, hover_y, hover_text = [], [], []
    for e in edges:
        a, b = nodes[e["from"]], nodes[e["to"]]
        x0, y0, x1, y1 = a["x"], a["y"], b["x"], b["y"]
        vx, vy = x1 - x0, y1 - y0
        length = float(np.hypot(vx, vy))
        if length == 0:
            continue
        ux, uy = vx / length, vy / length
        rx, ry = uy, -ux  # right-hand normal
        if (e["to"], e["from"]) in pairs:
            x0, y0, x1, y1 = x0 + rx * gap, y0 + ry * gap, x1 + rx * gap, y1 + ry * gap

        color = "#d62728" if e["is_reference"] else "#1f77b4"
        annotations.append(dict(
            x=x1, y=y1, ax=x0, ay=y0, xref="x", yref="y", axref="x", ayref="y",
            showarrow=True, arrowhead=3, arrowsize=1.2, arrowwidth=1.6, arrowcolor=color,
            standoff=9, startstandoff=9, text="",
        ))

        parts = []
        if show_distances and e["hd"] is not None:
            parts.append(f"{e['hd']:.2f} m")
        if show_angles and e["angle"] is not None:
            parts.append(f"∠ {e['angle']:.4f} {net['angle_unit']}")
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if parts:
            angle = np.degrees(np.arctan2(vy, vx))
            if angle > 90:
                angle -= 180
            elif angle < -90:
                angle += 180
            # offset in screen pixels so the label stays clear of its arrow at any zoom
            annotations.append(dict(
                x=mx, y=my, xref="x", yref="y", xshift=rx * label_px, yshift=ry * label_px,
                text=" · ".join(parts), showarrow=False, textangle=-angle,
                font=dict(size=10, color=color),
            ))

        hd = f"{e['hd']:.3f} m" if e["hd"] is not None else "—"
        ang = f"{e['angle']:.4f} {net['angle_unit']}" if e["angle"] is not None else "—"
        hz = f"{e['hz']:.4f} {net['angle_unit']}" if e["hz"] is not None else "—"
        hover_x.append(mx)
        hover_y.append(my)
        hover_text.append(
            f"<b>{e['from']} → {e['to']}</b>"
            f"{' (reference)' if e['is_reference'] else ''}"
            f"<br>Horizontal distance: {hd}<br>Hz reading: {hz}<br>Angle from reference: {ang}"
        )

    # Invisible midpoints carry the hover details for each arrow
    fig.add_trace(go.Scatter(x=hover_x, y=hover_y, mode="markers", marker=dict(size=10, opacity=0),
                             text=hover_text, hovertemplate="%{text}<extra></extra>", showlegend=False))

    for role, symbol, color, name in (("station", "triangle-up", "#d62728", "Stations"),
                                      ("reference", "circle", "#2ca02c", "References")):
        ids = [k for k, n in nodes.items() if n["role"] == role]
        if not ids:
            continue
        fig.add_trace(go.Scatter(
            x=[nodes[k]["x"] for k in ids], y=[nodes[k]["y"] for k in ids],
            mode="markers+text", text=ids, textposition="top center",
            textfont=dict(size=12, color="black"),
            marker=dict(symbol=symbol, size=14, color=color, line=dict(width=1, color="white")),
            name=name,
            customdata=[[nodes[k]["x"], nodes[k]["y"]] for k in ids],
            hovertemplate="<b>%{text}</b><br>E %{customdata[0]:.3f}<br>N %{customdata[1]:.3f}<extra></extra>",
        ))

    # Legend entries for the arrow colours
    for color, name in (("#d62728", "Sight to reference"), ("#1f77b4", "Sight to network point")):
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", line=dict(color=color, width=2), name=name))

    fig.update_layout(
        annotations=annotations, height=650, dragmode="pan",
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
        plot_bgcolor="white",
    )
    fig.update_xaxes(title="East (m)", showgrid=True, gridcolor="#eee", zeroline=False)
    fig.update_yaxes(title="North (m)", showgrid=True, gridcolor="#eee", zeroline=False,
                     scaleanchor="x", scaleratio=1)
    return fig


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

BASEMAPS = {
    "OpenStreetMap": (
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "© OpenStreetMap contributors",
    ),
    "Satellite (Esri WorldImagery)": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    ),
}

def _basemap_style(style: str) -> str:
    """
    Return the chosen basemap as a raster map style (data: URL) for map_style.
    A pydeck TileLayer can't be used here: without a JS renderSubLayers it hands
    image tiles to a GeoJsonLayer and draws nothing.
    """
    tiles, attribution = BASEMAPS.get(style, BASEMAPS["OpenStreetMap"])
    style_json = {
        "version": 8,
        "sources": {
            "basemap": {
                "type": "raster",
                "tiles": [tiles],
                "tileSize": 256,
                "maxzoom": 19,
                "attribution": attribution,
            }
        },
        "layers": [{"id": "basemap", "type": "raster", "source": "basemap"}],
    }
    return "data:application/json," + urllib.parse.quote(json.dumps(style_json))

def _render_map(lon: np.ndarray, lat: np.ndarray, Z: np.ndarray, labels: List[str], annotate: bool, basemap_style: str = "OpenStreetMap"):
    df = pd.DataFrame({
        "lon": lon, "lat": lat, "z": Z, "label": labels if annotate else [""] * len(labels)
    })

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
        layers=[points],
        initial_view_state=view,
        map_style=_basemap_style(basemap_style),
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

tabs = st.tabs(["🔧 Clean & Export", "📈 Visualize", "💾 Download Clear Desktop", "❓ Help"])

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
        submitted_viz = st.form_submit_button("Generate Visuals", type="primary")

    if submitted_viz:
        if not uploaded:
            st.warning("Please upload a file first.")
        else:
            # Network and point plots are built independently so one can work without the other
            viz = {"annotate": annotate}
            try:
                viz["network"] = parse_network(raw_text)
            except Exception as e:
                viz["network_error"] = str(e)
            try:
                x, y, Z, labels, skipped = parse_points_for_plot(raw_text)
                fig = make_plots(x, y, Z, labels if annotate else [""] * len(labels), grid_n=grid_n)
                png = io.BytesIO()
                fig.savefig(png, format="png", dpi=120)
                plt.close(fig)
                viz["points"] = {"x": x, "y": y, "Z": Z, "labels": labels,
                                 "skipped": skipped, "png": png.getvalue()}
            except Exception as e:
                viz["points_error"] = str(e)
            # store results so the visuals survive reruns
            st.session_state.viz = viz

    viz = st.session_state.get("viz")
    if not viz:
        st.info("Upload a file and click **Generate Visuals** to see the survey network, map and terrain plots.")
    else:
        pts = viz.get("points")

        # --- 1) Survey network ---
        st.markdown("### 🕸️ Survey Network")
        net = viz.get("network")
        if not net or not net["nodes"]:
            st.info(viz.get("network_error") or
                    "No station setups with coordinates were found in this file, so there is no network to draw.")
        else:
            c1, c2, c3 = st.columns(3)
            show_dist = c1.checkbox("Show distances", value=True)
            show_ang = c2.checkbox("Show angles from reference", value=False,
                                   help="Horizontal angle at the station, measured clockwise from the reference sight.")
            show_det = c3.checkbox("Show detail sights", value=False,
                                   help="Also draw sights to detail (non-network) points.")
            net_fig = make_network_figure(net, show_distances=show_dist, show_angles=show_ang,
                                          show_details=show_det)
            st.plotly_chart(net_fig, config={"scrollZoom": True, "displaylogo": False})
            n_st = sum(1 for n in net["nodes"].values() if n["role"] == "station")
            n_ref = len(net["nodes"]) - n_st
            st.caption(
                f"{n_st} stations, {n_ref} references, {len(net['edges'])} measured directions. "
                "Red arrows are sights to the setup's reference, blue arrows sights to other network points; "
                "forward and backward sights are drawn side by side. Distances are horizontal "
                "(slope distance × sin Vz). Scroll to zoom, drag to pan, double-click to reset; "
                "hover an arrow for its details."
            )
            if net["missing"]:
                st.caption("Not drawn (no coordinates in the POINTS table): "
                           + ", ".join(f"{a} → {b}" for a, b in net["missing"]))

        # --- 2) Interactive map ---
        st.markdown("### 🗺️ Interactive Map")
        if not pts:
            st.info(f"The map needs point coordinates. {viz.get('points_error', '')}")
        else:
            x, y, Z, labels = pts["x"], pts["y"], pts["Z"], pts["labels"]
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
                        _render_map(lon, lat, Z, labels, viz["annotate"], basemap_style=basemap_style)
                    except Exception as map_err:
                        st.warning(f"Couldn’t render map with CRS='{src_epsg}'. Tip: confirm your EPSG code. Details: {map_err}")

        # --- 3) Static terrain plots ---
        st.markdown("### 📈 Terrain Plots (MNT)")
        if not pts:
            st.warning(f"Terrain plots unavailable: {viz.get('points_error', '')}")
        else:
            x, y, Z = pts["x"], pts["y"], pts["Z"]
            st.image(pts["png"], width="stretch")
            st.download_button(
                "⬇️ Download plots (PNG)",
                data=pts["png"],
                file_name=f"{file_stem}_plots.png",
                mime="image/png",
            )

            # Quick stats
            st.subheader("Summary")
            c1, c2, c3 = st.columns(3)
            c1.metric("Points", f"{len(x)}")
            c2.metric("Z min / max", f"{np.min(Z):.3f} / {np.max(Z):.3f}")
            c3.metric("X span / Y span", f"{(np.max(x)-np.min(x)):.2f} / {(np.max(y)-np.min(y)):.2f}")
            if pts["skipped"]:
                st.caption(f"{pts['skipped']} point(s) skipped because East, North or Elevation was missing.")


# -------------- Tab 3: Download Clear Desktop --------------
with tabs[2]:
    st.subheader("Download Clear (Desktop)")
    st.caption("Click the button to download the Clear desktop installer.")

    # Your shared link converted to a direct-download URL
    direct_url = "https://drive.google.com/uc?export=download&id=1JukA4QQy2akC-GKhMemc2RvRpk2rdspK"

    st.link_button("⬇️ Download Clear Desktop", direct_url, width="stretch")

# -------------- Tab 4: Help --------------
with tabs[3]:
    # The user guide lives in README.md between the help markers, so the app and the
    # repository documentation stay in sync. Image lines are rendered with st.image.
    app_dir = Path(__file__).parent
    try:
        readme = (app_dir / "README.md").read_text(encoding="utf-8")
        guide = readme.split("<!-- help:start -->", 1)[1].split("<!-- help:end -->", 1)[0]
    except (OSError, IndexError):
        guide = ""

    if not guide.strip():
        st.info("The user guide is not available. See README.md in the project repository.")
    else:
        chunk: List[str] = []
        for line in guide.splitlines():
            img = re.fullmatch(r"!\[(.*)\]\((.+)\)", line.strip())
            if not img:
                chunk.append(line)
                continue
            st.markdown("\n".join(chunk))
            chunk = []
            img_path = app_dir / img.group(2)
            if img_path.exists():
                st.image(str(img_path), caption=img.group(1))
        st.markdown("\n".join(chunk))

# -------------- Footer --------------
st.divider()
st.markdown(
    f"<div style='text-align:center; color:gray; font-size:0.85em;'>"
    f"© {datetime.date.today().year} Tangent Analytics. All rights reserved. · "
    f"<a href='mailto:tangent.anlytics.ca@gmail.com' style='color:gray;'>tangent.anlytics.ca@gmail.com</a></div>",
    unsafe_allow_html=True,
)
