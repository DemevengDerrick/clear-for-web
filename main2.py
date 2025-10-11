# app.py
import io
from pathlib import Path
from typing import Iterable, List, Tuple, Optional

import numpy as np
import streamlit as st
import matplotlib.pyplot as plt
from scipy.interpolate import griddata  # replaces deprecated matplotlib.mlab.griddata

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
    Reproduce your per-line transformation:
      - If two tokens: drop first, join + "," (mode-independent in your code)
      - Else: del [7:11], del [2], del [0], join (mode1 no delimiter, mode2 comma),
              wrap with leading & trailing newline
    Guards against short rows.
    """
    if len(tokens) == 2:
        del tokens[0]
        return "".join(tokens) + ","
    if len(tokens) < 11:
        return ""  # malformed / too short
    t = tokens[:]
    try:
        del t[7:11]
        del t[2]
        del t[0]
    except IndexError:
        return ""
    joined = "".join(t) if station_mode == 1 else ",".join(t)
    return f"\n{joined}\n"

def _prefix_codes(lines: Iterable[str], cs: str, cr: str, cm: str) -> Iterable[str]:
    """
    Prefix each non-empty line with Station/Reference/Measure codes using your n/m rule:
      - if len(line) < 25 → station code; set m = n+1
      - elif n == m → reference code
      - else → measure code
    """
    n = 0
    m = 0
    for lin in lines:
        if not lin.strip():
            continue
        n += 1
        if len(lin) < 25:
            out = f"{cs},{lin}".rstrip("\n") + "\n"
            m = n + 1
        elif n == m:
            out = f"{cr},{lin}".rstrip("\n") + "\n"
        else:
            out = f"{cm},{lin}".rstrip("\n") + "\n"
        yield out

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

# ------------------------
# Streamlit UI
# ------------------------

st.set_page_config(page_title="IDX Cleaner & Viz", page_icon="🧹", layout="wide")
st.title("🧹 IDX Cleaner & Visualizer")

tabs = st.tabs(["🔧 Clean & Export", "📈 Visualize"])

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
            station_mode = st.radio("Code of Apparatus (Station Mode)", options=[1, 2], index=0, horizontal=True,
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

                # Quick stats
                st.subheader("Summary")
                c1, c2, c3 = st.columns(3)
                c1.metric("Points", f"{len(x)}")
                c2.metric("Z min / max", f"{np.min(Z):.3f} / {np.max(Z):.3f}")
                c3.metric("X span / Y span", f"{(np.max(x)-np.min(x)):.2f} / {(np.max(y)-np.min(y)):.2f}")

            except Exception as e:
                st.error(f"Visualization failed: {e}")
