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
    DAY_ORDER,
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


def daily_to_markdown(daily_subtotals: Dict, week_num: int) -> str:
    """Render the per-day rollup for one week as a markdown table.

    Per the boss's spec: mini-microcycle = day, so this table sums all
    sub-sessions (AM + PM, multiple coaches) that fall on the same
    weekday into a single row. Sorted Mon → Sun via DAY_ORDER.
    """
    lines = [
        "| Day | Sprint | Mid | Distance | All | Total yd | Total m |",
        "|:---|---:|---:|---:|---:|---:|---:|",
    ]
    keys = [(wn, d) for (wn, d) in daily_subtotals if wn == week_num]
    keys.sort(key=lambda k: DAY_ORDER.get(k[1], 999))
    for k in keys:
        dt = daily_subtotals[k]
        dy = _yard_total(dt)
        dm = _meter_total(dt)
        if dy == 0 and dm == 0:
            continue
        lines.append(
            f"| {k[1].capitalize()} | {dt['sprint_y']:,} | {dt['mid_y']:,} "
            f"| {dt['distance_y']:,} | {dt['all_y']:,} "
            f"| **{dy:,}** | {dm:,} |"
        )
    return "\n".join(lines) if len(lines) > 2 else "_No daily data for this week._"


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


def build_xlsx(results: Dict) -> bytes:
    """Render the full results as a 4-sheet Excel workbook.

    Mirrors the website's hierarchy:
      Sheet 1 'Summary'    - Grand total + per-group percentages
      Sheet 2 'Per Week'   - One row per week (all 18 in the season)
      Sheet 3 'Per Day'    - One row per (week, day) — the mini-microcycle view
      Sheet 4 'Workouts'   - One row per sub-session (most granular)

    Each downstream sheet drills into the level above. Header row is
    bold; total/section rows are bold. Column widths auto-fit on content.
    """
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDDDDD")

    def write_header(ws, headers):
        ws.append(headers)
        for cell in ws[ws.max_row]:
            cell.font = bold
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

    def autosize(ws):
        for col in ws.columns:
            max_len = max((len(str(c.value)) if c.value is not None else 0
                          for c in col), default=0)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    # ---- Sheet 1: Summary ----
    ws = wb.active
    ws.title = "Summary"
    gt = results["grand_total"]
    y_total = _yard_total(gt)
    m_total = _meter_total(gt)

    write_header(ws, ["Group", "Yards", "Meters", "% of Total"])
    for key, label in [("sprint", "Sprint (Dave)"),
                       ("mid", "Mid (Josh)"),
                       ("distance", "Distance (Noah)"),
                       ("all", "All (whole-team)")]:
        y = gt[f"{key}_y"]
        m = gt[f"{key}_m"]
        pct = (y / y_total * 100) if y_total else 0
        ws.append([label, y, m, f"{pct:.1f}%"])
    ws.append(["TOTAL", y_total, m_total, "100.0%"])
    for cell in ws[ws.max_row]:
        cell.font = bold
    ws.append([])
    ws.append([f"Sub-sessions parsed: {results['workouts_parsed']}"])
    if results.get("deduped"):
        ws.append([
            f"Duplicates auto-removed: {len(results['deduped'])} "
            f"({sum(d['yards'] for d in results['deduped']):,} yd)"
        ])
    autosize(ws)

    # ---- Sheet 2: Per Week ----
    ws = wb.create_sheet("Per Week")
    write_header(ws, ["Week", "Sprint", "Mid", "Distance", "All",
                      "Total yd", "Total m"])
    for wn in sorted(results["weekly_subtotals"]):
        wt = results["weekly_subtotals"][wn]
        ws.append([
            f"Week {wn}",
            wt["sprint_y"], wt["mid_y"], wt["distance_y"], wt["all_y"],
            _yard_total(wt), _meter_total(wt),
        ])
    autosize(ws)

    # ---- Sheet 3: Per Day ----
    ws = wb.create_sheet("Per Day")
    write_header(ws, ["Week", "Day", "Sprint", "Mid", "Distance", "All",
                      "Total yd", "Total m"])
    keys = sorted(
        results["daily_subtotals"].keys(),
        key=lambda k: (k[0], DAY_ORDER.get(k[1], 999)),
    )
    for (wn, day) in keys:
        dt = results["daily_subtotals"][(wn, day)]
        ws.append([
            wn, day.capitalize(),
            dt["sprint_y"], dt["mid_y"], dt["distance_y"], dt["all_y"],
            _yard_total(dt), _meter_total(dt),
        ])
    autosize(ws)

    # ---- Sheet 4: Workouts ----
    ws = wb.create_sheet("Workouts")
    write_header(ws, ["Week", "Day", "Workout", "Default Group", "Method",
                      "Sprint", "Mid", "Distance", "All",
                      "Total yd", "Total m"])
    for w in results["workouts"]:
        t = w["totals"]
        ws.append([
            w["week"], w.get("day", "").capitalize(), w["header"],
            w["default_group"], w["method"],
            t["sprint_y"], t["mid_y"], t["distance_y"], t["all_y"],
            _yard_total(t), _meter_total(t),
        ])
    autosize(ws)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
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

# Sidebar — input controls. Filter dropdown is rendered below in a
# second sidebar block AFTER we've read the input, so it can size its
# week range to the actual max Week N found in the uploaded files
# (covers seasons of any length without hardcoding).
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

# Read input. Multi-file uploads are concatenated in chronological order
# (by the lowest Week N they contain). Pasted text is treated as a single
# block and wins only when no files are uploaded.
text = None
combined_filenames: List[str] = []
if uploaded:
    text, combined_filenames = combine_uploaded_files(uploaded)
elif pasted.strip():
    text = pasted


def detect_max_week(t: str) -> int:
    """Return the largest 'Week N' header found in text, or 17 as fallback.

    Used to size the week-filter dropdown so it matches the season length
    actually present in the upload. The 17-week fallback covers a typical
    college dual-meet season for the case where nothing's been uploaded
    yet and we still want to show a meaningful dropdown.
    """
    weeks = []
    for line in (t or "").splitlines():
        m = WEEK_HEADER_RE.match(line)
        if m:
            weeks.append(int(m.group(1)))
    return max(weeks) if weeks else 17


max_week = detect_max_week(text)

# Sidebar — filter + footer (separate from the input block above so it
# renders AFTER input has been read and the week range is known).
with st.sidebar:
    st.header("Filter")
    week_options = ["All weeks"] + [f"Week {i}" for i in range(1, max_week + 1)]
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


def _render_dedupe_note(deduped):
    """Show an info banner if auto-dedupe skipped any sub-sessions.

    Lists each skipped workout so the user can sanity-check that the
    removals are legitimate paste artifacts and not real workouts that
    happen to share a body with an earlier one.
    """
    if not deduped:
        return
    total = sum(d["yards"] for d in deduped)
    with st.expander(
        f"Auto-dedupe removed {len(deduped)} duplicate sub-session(s) "
        f"({total:,} yd) — click to see which",
        expanded=False,
    ):
        st.markdown(
            "These had body text that exactly matched an earlier sub-session "
            "in the document (a copy-paste artifact). The first occurrence "
            "is kept; later identical copies are excluded from totals."
        )
        for d in deduped:
            st.markdown(
                f"- **Week {d['week']}**: {d['header'][:100]} — {d['yards']:,} yd"
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

_render_dedupe_note(results.get("deduped", []))

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
        st.markdown("**Per-day breakdown (mini-microcycle):**")
        st.markdown(daily_to_markdown(results["daily_subtotals"], week_num))
        st.markdown("**Individual workouts:**")
        week_workouts = [w for w in results["workouts"] if w["week"] == week_num]
        st.markdown(workouts_to_markdown(week_workouts))

st.divider()

# All workouts table.
st.subheader("All Workouts")
st.markdown(workouts_to_markdown(results["workouts"]))

# Export — both flat CSV (one row per workout) and structured Excel
# (4 sheets matching the website hierarchy: Summary / Per Week / Per
# Day / Workouts). The Excel mirrors how the page is laid out so the
# boss can drill from the headline down to a single sub-session.
st.subheader("Export")
col_xlsx, col_csv = st.columns(2)
with col_xlsx:
    st.download_button(
        label="Download Excel (4 sheets, week-by-week)",
        data=build_xlsx(results),
        file_name="swim_workouts.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help=(
            "Multi-sheet workbook: Summary, Per Week, Per Day, and "
            "Workouts. Matches the page layout — Summary at top, then "
            "drill down by week / day / individual workout."
        ),
    )
with col_csv:
    st.download_button(
        label="Download CSV (flat workouts table)",
        data=workouts_to_csv(results["workouts"]),
        file_name="swim_workouts.csv",
        mime="text/csv",
        help="One row per workout. Useful for pivot-tabling in Excel/Sheets.",
    )
