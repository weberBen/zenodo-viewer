#!/usr/bin/env python3
"""
Zenodo Version Tracker - Streamlit GUI
=======================================
Usage:
    uv run streamlit run app.py
"""

import difflib
import hashlib
import json
import re
from pathlib import Path

import requests
import streamlit as st
from streamlit_timeline import st_timeline

from zenodo_versions import (
    convert_pdfs,
    extract_archives,
    find_target_files,
    get_all_versions,
    parse_record_id,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path("zenodo_output")
DOWNLOADS_DIR = OUTPUT_DIR / "downloads"

st.set_page_config(page_title="Zenodo Version Tracker", layout="wide")

TABS = ["Diff Viewer", "Search", "Browse"]


# ---------------------------------------------------------------------------
# Navigation helper
# ---------------------------------------------------------------------------


def navigate_to_browse(version_idx: int, file_name: str = None, line: int = None, highlight: str = None):
    """Navigate to Browse tab showing a specific version."""
    st.session_state["_nav_target"] = "Browse"
    st.session_state["browse_version"] = version_idx
    if file_name:
        st.session_state["browse_file"] = file_name
    if line:
        st.session_state["browse_line"] = line
    if highlight:
        st.session_state["browse_highlight"] = highlight
    st.rerun()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner="Fetching versions from Zenodo API...")
def fetch_versions(record_id: str) -> list[dict]:
    return get_all_versions(record_id)


def derive_version_name(full_meta: dict) -> str:
    """Derive a meaningful version name from full record metadata."""
    nested = full_meta.get("metadata", {})

    explicit = nested.get("version")
    if explicit:
        return explicit

    files = full_meta.get("files", [])
    if files:
        stem = files[0].get("key", "").rsplit(".", 1)[0]
        parts = stem.rsplit("_", 1)
        if len(parts) == 2:
            version_part = parts[1]
            sub_versions = re.findall(r'v[\d]+(?:[.\-]\d+)*', version_part)
            if sub_versions and len(sub_versions) > 1:
                return sub_versions[-1]
            return version_part

    relations = nested.get("relations", {})
    version_info = relations.get("version", [])
    if version_info and isinstance(version_info, list) and len(version_info) > 0:
        return f"v{version_info[0].get('index', 0)}"

    return "v?"


def get_created_date(full_meta: dict) -> str:
    """Extract YYYY-MM-DD from the top-level 'created' ISO timestamp."""
    created = full_meta.get("created", "")
    if created and len(created) >= 10:
        return created[:10]
    return full_meta.get("metadata", {}).get("publication_date", "?")


def load_text_dirs() -> list[tuple[str, Path, dict]]:
    """Load all _text directories with full record metadata."""
    text_dirs = []
    if not DOWNLOADS_DIR.exists():
        return text_dirs
    for version_dir in sorted(DOWNLOADS_DIR.iterdir()):
        if not version_dir.is_dir():
            continue
        text_dir = version_dir / "_text"
        meta_file = version_dir / "_metadata.json"
        full_meta = {}
        if meta_file.exists():
            full_meta = json.loads(meta_file.read_text())
        if text_dir.exists() and any(text_dir.iterdir()):
            text_dirs.append((version_dir.name, text_dir, full_meta))
    return text_dirs


def load_version_text(text_dir: Path) -> dict[str, str]:
    """Load all text files from a version's _text directory."""
    texts = {}
    if not text_dir.exists():
        return texts
    for f in sorted(text_dir.iterdir()):
        if f.is_file() and f.suffix in (".md", ".tex"):
            texts[f.name] = f.read_text(encoding="utf-8", errors="replace")
    return texts


def get_display_label(entry: tuple) -> str:
    """Get display label: #{num} {version_name} ({created date})."""
    label, _, full_meta = entry
    num = label.split("_", 2)[0]
    v = derive_version_name(full_meta)
    date = get_created_date(full_meta)
    return f"#{num} {v} ({date})"


def highlight_text_html(text: str, query: str, case_sensitive: bool = False) -> str:
    """Highlight query matches in text with HTML mark tags."""
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.escape(query)
    return re.sub(
        f"({pattern})",
        r'<mark style="background-color: #ffeb3b; padding: 2px;">\1</mark>',
        text,
        flags=flags,
    )


def compute_diff_lines(text_a: str, text_b: str) -> dict:
    """Compute diff and categorize lines as added, removed, or unchanged."""
    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()

    matcher = difflib.SequenceMatcher(None, lines_a, lines_b)

    result = {"added": [], "removed": [], "unchanged": [], "combined": []}

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in lines_a[i1:i2]:
                result["unchanged"].append(line)
                result["combined"].append(("unchanged", line, line))
        elif tag == "replace":
            for line in lines_a[i1:i2]:
                result["removed"].append(line)
            for line in lines_b[j1:j2]:
                result["added"].append(line)
            max_len = max(i2 - i1, j2 - j1)
            for k in range(max_len):
                old = lines_a[i1 + k] if k < (i2 - i1) else ""
                new = lines_b[j1 + k] if k < (j2 - j1) else ""
                result["combined"].append(("changed", old, new))
        elif tag == "delete":
            for line in lines_a[i1:i2]:
                result["removed"].append(line)
                result["combined"].append(("removed", line, ""))
        elif tag == "insert":
            for line in lines_b[j1:j2]:
                result["added"].append(line)
                result["combined"].append(("added", "", line))

    return result


# ---------------------------------------------------------------------------
# Sidebar: Record input & download
# ---------------------------------------------------------------------------

st.sidebar.title("Zenodo Version Tracker")

record_input = st.sidebar.text_input(
    "Zenodo Record URL or ID",
    value="https://zenodo.org/records/18437004",
)

token = st.sidebar.text_input("API Token (optional)", type="password")

if st.sidebar.button("Fetch & Download", type="primary"):
    try:
        record_id = parse_record_id(record_input)
    except ValueError as e:
        st.sidebar.error(str(e))
        st.stop()

    versions = fetch_versions(record_id)
    st.sidebar.success(f"Found {len(versions)} version(s)")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    progress = st.sidebar.progress(0, text="Downloading...")
    for i, version in enumerate(versions):
        vid = version["id"]
        version_label = version.get("metadata", {}).get("version", f"v{i+1}")
        version_dir = DOWNLOADS_DIR / f"{i+1:03d}_{version_label}_{vid}"
        version_dir.mkdir(parents=True, exist_ok=True)

        meta_file = version_dir / "_metadata.json"
        files = version.get("files", [])
        already_done = meta_file.exists()
        if already_done:
            for file_info in files:
                dest = version_dir / file_info["key"]
                if not dest.exists():
                    already_done = False
                    break
                existing_md5 = hashlib.md5(dest.read_bytes()).hexdigest()
                if existing_md5 != file_info.get("checksum", "").replace("md5:", ""):
                    already_done = False
                    break

        if not already_done:
            for file_info in files:
                filename = file_info["key"]
                file_url = file_info["links"]["self"]
                dest = version_dir / filename
                if dest.exists():
                    existing_md5 = hashlib.md5(dest.read_bytes()).hexdigest()
                    if existing_md5 == file_info.get("checksum", "").replace("md5:", ""):
                        continue
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                resp = requests.get(file_url, headers=headers, stream=True)
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)

            meta_file.write_text(json.dumps(version, indent=2))

        extract_archives(version_dir)
        target_files = find_target_files(version_dir)
        if target_files["pdf"] or target_files["tex"]:
            convert_pdfs(version_dir, target_files)

        progress.progress((i + 1) / len(versions), text=f"Version {i+1}/{len(versions)}")

    progress.empty()
    st.sidebar.success("Done!")
    st.rerun()

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

text_dirs = load_text_dirs()

if not text_dirs:
    st.info("No data yet. Enter a Zenodo record URL and click 'Fetch & Download' in the sidebar.")
    st.stop()

display_labels = [get_display_label(e) for e in text_dirs]

# ---------------------------------------------------------------------------
# Sidebar: clickable version list
# ---------------------------------------------------------------------------

st.sidebar.markdown("---")
st.sidebar.markdown(f"**{len(text_dirs)} version(s)**")

sidebar_container = st.sidebar.container(height=400)
with sidebar_container:
    for i, (label, _, full_meta) in enumerate(text_dirs):
        v = derive_version_name(full_meta)
        date = get_created_date(full_meta)
        if st.button(f"{v} ({date})", key=f"sidebar_v_{i}", use_container_width=True):
            navigate_to_browse(i)

# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

st.header("Version Timeline")

timeline_items = []
for i, (label, _, full_meta) in enumerate(text_dirs):
    v = derive_version_name(full_meta)
    created = full_meta.get("created", "2025-01-01")
    title_text = full_meta.get("metadata", {}).get("title", "")
    timeline_items.append({
        "id": i,
        "content": v,
        "start": created,
        "title": f"{v} ({get_created_date(full_meta)}) - {title_text}",
    })

if timeline_items:
    selected = st_timeline(
        timeline_items,
        groups=[],
        options={
            "selectable": True,
            "zoomable": True,
            "moveable": True,
            "height": "200px",
            "margin": {"item": 10},
            "zoomMin": 1000 * 60 * 60 * 24,
            "zoomMax": 1000 * 60 * 60 * 24 * 365 * 2,
        },
        height="200px",
    )

    if selected and "id" in selected:
        clicked_id = selected["id"]
        # Navigate to browse on timeline click
        if st.session_state.get("_timeline_last_click") != clicked_id:
            st.session_state["_timeline_last_click"] = clicked_id
            navigate_to_browse(clicked_id)

st.markdown("---")

# ---------------------------------------------------------------------------
# Tab navigation via segmented control (supports programmatic switching)
# ---------------------------------------------------------------------------

# Apply pending navigation (must happen before widget instantiation)
nav_target = st.session_state.pop("_nav_target", None)
if nav_target:
    st.session_state["tab_selector"] = nav_target
    # Also sync browse widgets so selectboxes show the correct selection
    if nav_target == "Browse" and "browse_version" in st.session_state:
        st.session_state["browse_v"] = st.session_state["browse_version"]
    if nav_target == "Browse" and "browse_file" in st.session_state:
        # browse_f uses the filename directly, but selectbox expects index for range options
        # We handle it by setting browse_f_pending and resolving inside the Browse tab
        st.session_state["_browse_file_pending"] = st.session_state.pop("browse_file")
    if nav_target == "Browse" and "browse_highlight" in st.session_state:
        st.session_state["browse_search"] = st.session_state.pop("browse_highlight")

active_tab = st.segmented_control(
    "Navigation",
    TABS,
    default=TABS[0],
    key="tab_selector",
    label_visibility="collapsed",
)

# ---------------------------------------------------------------------------
# Tab: Diff Viewer
# ---------------------------------------------------------------------------

if active_tab == "Diff Viewer":
    st.header("Compare Versions")

    col1, col2 = st.columns(2)

    with col1:
        idx_a = st.selectbox("Version A (older)", range(len(display_labels)),
                             format_func=lambda i: display_labels[i], key="diff_a")
    with col2:
        default_b = min(idx_a + 1, len(display_labels) - 1)
        idx_b = st.selectbox("Version B (newer)", range(len(display_labels)),
                             index=default_b, format_func=lambda i: display_labels[i], key="diff_b")

    # Quick links to browse each version
    col_btn1, col_btn2, _ = st.columns([1, 1, 3])
    with col_btn1:
        if st.button(f"Open {display_labels[idx_a]}", key="diff_open_a"):
            navigate_to_browse(idx_a)
    with col_btn2:
        if st.button(f"Open {display_labels[idx_b]}", key="diff_open_b"):
            navigate_to_browse(idx_b)

    texts_a = load_version_text(text_dirs[idx_a][1])
    texts_b = load_version_text(text_dirs[idx_b][1])

    files_a = sorted(texts_a.keys())
    files_b = sorted(texts_b.keys())

    paired_files = []
    if len(files_a) == 1 and len(files_b) == 1:
        paired_files.append((files_a[0], files_b[0]))
    else:
        matched_a = set()
        matched_b = set()
        for fa in files_a:
            if fa in texts_b:
                paired_files.append((fa, fa))
                matched_a.add(fa)
                matched_b.add(fa)
        remaining_a = [f for f in files_a if f not in matched_a]
        remaining_b = [f for f in files_b if f not in matched_b]
        for fa, fb in zip(remaining_a, remaining_b):
            paired_files.append((fa, fb))
        for fa in remaining_a[len(remaining_b):]:
            paired_files.append((fa, None))
        for fb in remaining_b[len(remaining_a):]:
            paired_files.append((None, fb))

    if not paired_files:
        st.warning("No text files found.")
    else:
        pair_labels = []
        for fa, fb in paired_files:
            if fa == fb:
                pair_labels.append(fa)
            elif fa and fb:
                pair_labels.append(f"{fa}  ->  {fb}")
            elif fa:
                pair_labels.append(f"{fa}  (removed)")
            else:
                pair_labels.append(f"{fb}  (added)")

        selected_pair_idx = st.selectbox("File", range(len(pair_labels)),
                                         format_func=lambda i: pair_labels[i], key="diff_file")
        fa, fb = paired_files[selected_pair_idx]
        text_a = texts_a.get(fa, "") if fa else ""
        text_b = texts_b.get(fb, "") if fb else ""

        if text_a == text_b:
            st.success("Files are identical.")
        else:
            diff_mode = st.radio(
                "Display mode",
                ["Side by side", "Additions only", "Deletions only"],
                horizontal=True,
            )

            diff_data = compute_diff_lines(text_a, text_b)

            if diff_mode == "Additions only":
                st.markdown(f"**{len(diff_data['added'])} added line(s)**")
                html_parts = []
                for line in diff_data["added"]:
                    escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    html_parts.append(
                        f'<div style="background-color: #e6ffe6; padding: 2px 8px; '
                        f'border-left: 3px solid #4caf50; margin: 1px 0; font-family: monospace; '
                        f'font-size: 13px;">+ {escaped}</div>'
                    )
                st.markdown("".join(html_parts), unsafe_allow_html=True)

            elif diff_mode == "Deletions only":
                st.markdown(f"**{len(diff_data['removed'])} removed line(s)**")
                html_parts = []
                for line in diff_data["removed"]:
                    escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    html_parts.append(
                        f'<div style="background-color: #ffe6e6; padding: 2px 8px; '
                        f'border-left: 3px solid #f44336; margin: 1px 0; font-family: monospace; '
                        f'font-size: 13px;">- {escaped}</div>'
                    )
                st.markdown("".join(html_parts), unsafe_allow_html=True)

            else:  # Side by side
                st.markdown(
                    f"**{len(diff_data['added'])} additions, "
                    f"{len(diff_data['removed'])} deletions, "
                    f"{len(diff_data['unchanged'])} unchanged**"
                )

                html = [
                    '<div style="overflow-x: auto; font-family: monospace; font-size: 12px;">',
                    '<table style="width: 100%; border-collapse: collapse; table-layout: fixed;">',
                    '<tr><th style="width:50%; padding:4px; background:#f0f0f0; border:1px solid #ddd;">',
                    f'{display_labels[idx_a]}</th>',
                    '<th style="width:50%; padding:4px; background:#f0f0f0; border:1px solid #ddd;">',
                    f'{display_labels[idx_b]}</th></tr>',
                ]

                for tag, old, new in diff_data["combined"]:
                    old_esc = old.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    new_esc = new.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

                    if tag == "unchanged":
                        html.append(
                            f'<tr><td style="padding:2px 6px; border:1px solid #eee; '
                            f'white-space:pre-wrap; word-break:break-all;">{old_esc}</td>'
                            f'<td style="padding:2px 6px; border:1px solid #eee; '
                            f'white-space:pre-wrap; word-break:break-all;">{new_esc}</td></tr>'
                        )
                    elif tag == "removed":
                        html.append(
                            f'<tr><td style="padding:2px 6px; border:1px solid #eee; '
                            f'background-color:#ffe6e6; white-space:pre-wrap; word-break:break-all;">'
                            f'<span style="background:#ffb3b3;">{old_esc}</span></td>'
                            f'<td style="padding:2px 6px; border:1px solid #eee;"></td></tr>'
                        )
                    elif tag == "added":
                        html.append(
                            f'<tr><td style="padding:2px 6px; border:1px solid #eee;"></td>'
                            f'<td style="padding:2px 6px; border:1px solid #eee; '
                            f'background-color:#e6ffe6; white-space:pre-wrap; word-break:break-all;">'
                            f'<span style="background:#b3ffb3;">{new_esc}</span></td></tr>'
                        )
                    elif tag == "changed":
                        html.append(
                            f'<tr><td style="padding:2px 6px; border:1px solid #eee; '
                            f'background-color:#ffe6e6; white-space:pre-wrap; word-break:break-all;">'
                            f'<span style="background:#ffb3b3;">{old_esc}</span></td>'
                            f'<td style="padding:2px 6px; border:1px solid #eee; '
                            f'background-color:#e6ffe6; white-space:pre-wrap; word-break:break-all;">'
                            f'<span style="background:#b3ffb3;">{new_esc}</span></td></tr>'
                        )

                html.append("</table></div>")
                st.markdown("".join(html), unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Tab: Search
# ---------------------------------------------------------------------------

elif active_tab == "Search":
    st.header("Search Across All Versions")

    query = st.text_input("Search term or phrase", key="search_query")
    case_sensitive = st.checkbox("Case sensitive", value=False)

    if query:
        flags = 0 if case_sensitive else re.IGNORECASE
        all_results = []

        for label, text_dir, full_meta in text_dirs:
            if not text_dir.exists():
                continue
            v = derive_version_name(full_meta)
            date = get_created_date(full_meta)
            display = f"{v} ({date})"

            for f in sorted(text_dir.iterdir()):
                if not f.is_file() or f.suffix not in (".md", ".tex"):
                    continue
                content = f.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()

                for line_num, line in enumerate(lines, 1):
                    if re.search(re.escape(query), line, flags):
                        start = max(0, line_num - 3)
                        end = min(len(lines), line_num + 2)
                        context = lines[start:end]
                        all_results.append({
                            "version_display": display,
                            "version_label": label,
                            "file": f.name,
                            "line": line_num,
                            "match_line": line.strip(),
                            "context": "\n".join(context),
                        })

        if not all_results:
            st.warning(f"No results for '{query}'")
        else:
            first = all_results[0]
            st.info(
                f"**First occurrence:** {first['version_display']} "
                f"(file: `{first['file']}`, line {first['line']})"
            )
            st.success(f"**{len(all_results)}** match(es) across all versions")

            by_version = {}
            for r in all_results:
                by_version.setdefault(r["version_display"], []).append(r)

            for version_display, matches in by_version.items():
                with st.expander(f"{version_display} -- {len(matches)} match(es)", expanded=False):
                    for i, m in enumerate(matches[:100]):
                        context_html = m["context"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        context_html = highlight_text_html(context_html, query, case_sensitive)

                        st.markdown(
                            f'<div style="background:#f8f9fa; border:1px solid #dee2e6; '
                            f'border-radius:4px; padding:10px; margin:8px 0; font-family:monospace; '
                            f'font-size:13px; white-space:pre-wrap;">'
                            f'<strong>{m["file"]}:{m["line"]}</strong><br><br>'
                            f'{context_html}</div>',
                            unsafe_allow_html=True,
                        )

                        btn_key = f"goto_{m['version_label']}_{m['file']}_{m['line']}_{i}"
                        if st.button(f"Open full document at line {m['line']}", key=btn_key):
                            for idx, (lbl, _, _) in enumerate(text_dirs):
                                if lbl == m["version_label"]:
                                    navigate_to_browse(idx, m["file"], m["line"], query)

# ---------------------------------------------------------------------------
# Tab: Browse
# ---------------------------------------------------------------------------

elif active_tab == "Browse":
    st.header("Browse Version Content")

    # browse_v is set directly by navigation before this widget renders
    idx = st.selectbox(
        "Select version",
        range(len(display_labels)),
        format_func=lambda i: display_labels[i],
        key="browse_v",
    )

    texts = load_version_text(text_dirs[idx][1])

    if not texts:
        st.warning("No text files in this version.")
    else:
        file_names = sorted(texts.keys())

        # Apply pending file selection from navigation
        pending_file = st.session_state.pop("_browse_file_pending", None)
        if pending_file and pending_file in file_names:
            st.session_state["browse_f"] = pending_file

        file_name = st.selectbox("File", file_names, key="browse_f")
        content = texts[file_name]

        st.markdown(f"**{len(content):,} characters, {len(content.splitlines()):,} lines**")

        # browse_search is set directly by navigation before this widget renders
        local_search = st.text_input("Highlight text", key="browse_search")

        lines = content.splitlines()

        # Jump target from navigation
        target_line = st.session_state.pop("browse_line", None)

        if local_search:
            search_flags = re.IGNORECASE
            matching_indices = [
                i for i, line in enumerate(lines)
                if re.search(re.escape(local_search), line, search_flags)
            ]

            st.info(f"{len(matching_indices)} matching line(s)")

            if target_line and target_line - 1 in range(len(lines)):
                start = max(0, target_line - 10)
                end = min(len(lines), target_line + 10)
                context_lines = lines[start:end]
                context_text = "\n".join(
                    f"L{start+j+1:4d} | {line}" for j, line in enumerate(context_lines)
                )
                context_html = context_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                context_html = highlight_text_html(context_html, local_search)
                st.markdown(
                    f'<div style="background:#fff3cd; border:2px solid #ffc107; border-radius:4px; '
                    f'padding:12px; margin:8px 0; font-family:monospace; font-size:13px; '
                    f'white-space:pre-wrap;"><strong>Target: line {target_line}</strong><br><br>'
                    f'{context_html}</div>',
                    unsafe_allow_html=True,
                )
                st.markdown("---")

            for idx_line in matching_indices[:200]:
                start = max(0, idx_line - 1)
                end = min(len(lines), idx_line + 2)
                snippet = "\n".join(f"L{start+j+1:4d} | {lines[start+j]}" for j in range(end - start))
                snippet_html = snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                snippet_html = highlight_text_html(snippet_html, local_search)
                st.markdown(
                    f'<div style="background:#f8f9fa; border:1px solid #dee2e6; border-radius:4px; '
                    f'padding:8px; margin:4px 0; font-family:monospace; font-size:12px; '
                    f'white-space:pre-wrap;">{snippet_html}</div>',
                    unsafe_allow_html=True,
                )
        else:
            if len(lines) > 500:
                st.warning(f"Showing first 500 of {len(lines)} lines. Use highlight to search.")
                st.code("\n".join(lines[:500]), language="markdown")
            else:
                st.code(content, language="markdown")
