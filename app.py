"""Streamlit web UI for swim_parser.

A non-technical user uploads (or pastes) the Cal Swim workout document
and sees totals split by training group: Sprint (Dave's), Mid (Josh's),
Distance (Noah's), and All (no group specified). The four buckets sum
to the workout total by construction.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py

Deploy to Streamlit Community Cloud:
    1. Push this folder to a public GitHub repo (must include
       swim_parser.py, app.py, and requirements.txt).
    2. Go to https://share.streamlit.io and sign in with GitHub.
    3. Click "New app", pick the repo / branch / app.py.
    4. Click Deploy. Share the resulting URL with the team.
"""

import csv
import io
from typing import Dict, List

import streamlit as st

from swim_parser import (
    WEEK_HEADER_RE,
    compute_workout_totals,
    _yard_total,
    _meter_total,
)


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

GROUP_KEYS = ["sprint", "mid", "distance", "all"]
GROUP_LABELS = {
    "sprint":   "Sprint",
    "mid":      "Mid",
    "distance": "Distance",
    "all":      "All",
}
GROUP_HINTS = {
    "sprint":   "Dave's group",
    "mid":      "Josh's group",
    "distance": "Noah's group",
    "all":      "No group specified",
}
CSV_HEADERS = [
    "Week", "Workout", "Method", "Default Group",
    "Sprint yd", "Mid yd", "Distance yd", "All yd",
    "Sprint m", "Mid m", "Distance m", "All m",
    "Total yd", "Total m",
]


# ---------------------------------------------------------------------------
# Formatting helpers.
# ---------------------------------------------------------------------------

def fmt_amount(yards: int, meters: int) -> str:
    """Return e.g. '12,345 yd' or '12,345 yd  /  678 m'."""
    s = f"{yards:,} yd"
    if meters:
        s += f"  /  {meters:,} m"
    return s


def render_bucket_row(totals: Dict[str, int]) -> None:
    """Render Total + 4 group metrics as a 5-column row.

    Used for both the grand-total banner and each per-week expander.
    """
    y_total = _yard_total(totals)
    m_total = _meter_total(totals)
    cols = st.columns(5)
    cols[0].metric("Total", fmt_amount(y_total, m_total))
    for i, key in enumerate(GROUP_KEYS, start=1):
        y = totals[f"{key}_y"]
        m = totals[f"{key}_m"]
        pct = (y / y_total * 100) if y_total else 0
        cols[i].metric(
            GROUP_LABELS[key],
            fmt_amount(y, m),
            delta=f"{pct:.1f}%" if y_total else None,
            delta_color="off",
            help=GROUP_HINTS[key],
        )


def workouts_to_markdown(workouts: List[dict], header_max: int = 60) -> str:
    """Render the workout list as a GitHub-flavored markdown table.

    Used instead of st.dataframe because st.dataframe pulls in pyarrow,
    which has frequent x86_64/arm64 mismatch issues on macOS. Markdown
    tables render natively in Streamlit with no extra dependency.
    Long workout headers are truncated to header_max chars so the table
    stays readable on narrow screens.
    """
    lines = [
        "| Week | Workout | Default | Method "
        "| Sprint | Mid | Distance | All | Total yd | Total m |",
        "|---:|:---|:---:|:---:|---:|---:|---:|---:|---:|---:|",
    ]
    for w in workouts:
        t = w["totals"]
        hdr = w["header"]
        if len(hdr) > header_max:
            hdr = hdr[:header_max - 1] + "…"
        # Escape pipes so a stray '|' in a header doesn't break the table.
        hdr = hdr.replace("|", "\\|")
        lines.append(
            f"| {w['week']} | {hdr} | {w['default_group']} | {w['method']} "
            f"| {t['sprint_y']:,} | {t['mid_y']:,} | {t['distance_y']:,} "
            f"| {t['all_y']:,} | {_yard_total(t):,} | {_meter_total(t):,} |"
        )
    return "\n".join(lines)


def min_week_in_text(text: str) -> int:
    """Return the smallest 'Week N' header found in text (high sentinel if none).

    Used to sort multi-file uploads chronologically — a file containing
    'Week 1..10' sorts before one containing 'Week 11..17', regardless of
    the order the user dropped them in the uploader. Files without any
    Week header fall to the end of the order.
    """
    weeks = []
    for line in text.splitlines():
        m = WEEK_HEADER_RE.match(line)
        if m:
            weeks.append(int(m.group(1)))
    return min(weeks) if weeks else 10_000


def combine_uploaded_files(uploaded_files) -> tuple:
    """Read + concatenate multiple uploaded .txt files in week order.

    Returns (combined_text, ordered_file_names). The combined text has
    each file separated by a blank line so split_workouts() can't merge
    a workout across the file boundary. Order is determined by the
    minimum Week N in each file, so 'Weeks 1-10.txt' is processed
    before 'Weeks 11-17.txt' even if dropped in the opposite order.
    """
    decoded = []
    for uf in uploaded_files:
        # utf-8-sig strips a leading BOM if present. Without this, a BOM at
        # the start of the file prefixes ﻿ onto the first line and the
        # WEEK_HEADER_RE on that line silently fails to match, causing all
        # the file's content to be tagged as the previous file's last week.
        text = uf.read().decode("utf-8-sig")
        decoded.append((uf.name, text, min_week_in_text(text)))
    decoded.sort(key=lambda t: (t[2], t[0]))
    combined = "\n\n".join(text for _, text, _ in decoded)
    ordered_names = [name for name, _, _ in decoded]
    return combined, ordered_names


def workouts_to_csv(workouts: List[dict]) -> str:
    """Render the full workout list as a CSV string for download."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    for w in workouts:
        t = w["totals"]
        writer.writerow([
            w["week"], w["header"], w["method"], w["default_group"],
            t["sprint_y"], t["mid_y"], t["distance_y"], t["all_y"],
            t["sprint_m"], t["mid_m"], t["distance_m"], t["all_m"],
            _yard_total(t), _meter_total(t),
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Page layout.
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Cal Swim Workout Parser",
    layout="wide",
)

st.title("Cal Swim Workout Parser")
st.caption(
    "Upload the team workout document (or paste its text) to get total "
    "yardage broken out by training group. Sprint + Mid + Distance + All "
    "sums to the workout total for every workout, every week, and the "
    "grand total."
)

# Sidebar: input + week filter.
with st.sidebar:
    st.header("Input")
    uploaded = st.file_uploader(
        "Upload .txt file(s)",
        type=["txt"],
        accept_multiple_files=True,
        help=(
            "Drop one file or several at once (e.g. 'Weeks 1-10.txt' and "
            "'Weeks 11-17.txt'). Files are combined by the lowest Week N "
            "they contain, so upload order doesn't matter."
        ),
    )
    st.markdown("— or —")
    pasted = st.text_area(
        "Paste workout text",
        height=180,
        placeholder="Paste the document text here...",
    )

    st.header("Filter")
    week_options = ["All weeks"] + [f"Week {i}" for i in range(1, 11)]
    week_choice = st.selectbox("Show one week (optional)", week_options)
    week_filter = None if week_choice == "All weeks" else int(week_choice.split()[1])

    st.markdown("---")
    st.caption(
        "**Coach → group mapping**\n\n"
        "- Dave → Sprint\n"
        "- Josh → Mid\n"
        "- Noah → Distance\n"
        "- Unknown / multi-coach → All"
    )

# Read input. Multi-file uploads are concatenated in chronological order
# (by the lowest Week N they contain). Pasted text is treated as a single
# block and wins only when no files are uploaded.
text = None
combined_filenames: List[str] = []
if uploaded:
    text, combined_filenames = combine_uploaded_files(uploaded)
elif pasted.strip():
    text = pasted

if not text:
    st.info(
        "Upload one or more `.txt` files (or paste workout text) in the "
        "left sidebar to get started."
    )
    st.stop()

if len(combined_filenames) > 1:
    st.caption(
        "Combined "
        + str(len(combined_filenames))
        + " files in week order: "
        + ", ".join(f"`{n}`" for n in combined_filenames)
    )


# ---------------------------------------------------------------------------
# Parse and display.
# ---------------------------------------------------------------------------

with st.spinner("Parsing workouts..."):
    results = compute_workout_totals(text, week_filter=week_filter)

if not results["workouts"]:
    st.warning(
        "No workouts found. The text should contain date headers (e.g. "
        "'Wednesday, August 27, 2025'), 'Coach:' lines, and either "
        "####/#### cumulative checkpoints or NxDIST set lines."
    )
    st.stop()

# Grand total banner.
header_text = f"Grand Total ({results['workouts_parsed']} workout(s))"
if week_filter:
    header_text += f"  —  Week {week_filter} only"
st.subheader(header_text)
render_bucket_row(results["grand_total"])

st.divider()

# Per-week subtotals + drill-in.
st.subheader("Per-Week Subtotals")
for week_num in sorted(results["weekly_subtotals"]):
    wt = results["weekly_subtotals"][week_num]
    wy = _yard_total(wt)
    wm = _meter_total(wt)
    expander_label = f"Week {week_num} — {fmt_amount(wy, wm)}"
    expanded = (week_filter is not None) or (len(results["weekly_subtotals"]) == 1)
    with st.expander(expander_label, expanded=expanded):
        render_bucket_row(wt)
        st.markdown("**Workouts:**")
        week_workouts = [w for w in results["workouts"] if w["week"] == week_num]
        st.markdown(workouts_to_markdown(week_workouts))

st.divider()

# All workouts table.
st.subheader("All Workouts")
st.markdown(workouts_to_markdown(results["workouts"]))

# CSV export.
st.subheader("Export")
st.download_button(
    label="Download CSV",
    data=workouts_to_csv(results["workouts"]),
    file_name="swim_workouts.csv",
    mime="text/csv",
    help="One row per workout. Includes per-group yards and meters.",
)
