#!/usr/bin/env python3
"""
Zenodo Version Tracker - Streamlit GUI
=======================================
Interface graphique pour visualiser les diffs et chercher dans toutes les versions.

Usage:
    uv run streamlit run app.py
"""

import json
import re
from pathlib import Path

import streamlit as st
from diff_viewer import diff_viewer

from zenodo_versions import (
    convert_pdfs,
    download_all_versions,
    extract_archives,
    find_first_occurrence,
    find_target_files,
    get_all_versions,
    parse_record_id,
    search_in_versions,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path("zenodo_output")
DOWNLOADS_DIR = OUTPUT_DIR / "downloads"

st.set_page_config(page_title="Zenodo Version Tracker", layout="wide")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner="Fetching versions from Zenodo API...")
def fetch_versions(record_id: str) -> list[dict]:
    return get_all_versions(record_id)


def get_version_label(version: dict, index: int) -> str:
    meta = version.get("metadata", {})
    v = meta.get("version", f"v{index+1}")
    date = meta.get("publication_date", "?")
    return f"{index+1:02d} - {v} ({date})"


def load_text_dirs() -> list[tuple[str, Path]]:
    """Load all _text directories from downloaded versions."""
    text_dirs = []
    if not DOWNLOADS_DIR.exists():
        return text_dirs
    for version_dir in sorted(DOWNLOADS_DIR.iterdir()):
        if not version_dir.is_dir():
            continue
        text_dir = version_dir / "_text"
        if text_dir.exists() and any(text_dir.iterdir()):
            text_dirs.append((version_dir.name, text_dir))
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


# ---------------------------------------------------------------------------
# Sidebar: Record input & download
# ---------------------------------------------------------------------------

st.sidebar.title("Zenodo Version Tracker")

record_input = st.sidebar.text_input(
    "Zenodo Record URL or ID",
    value="https://zenodo.org/records/18437004",
    help="e.g. https://zenodo.org/records/18437004 or just 18437004",
)

token = st.sidebar.text_input("API Token (optional)", type="password")

if st.sidebar.button("Fetch & Download Versions", type="primary"):
    try:
        record_id = parse_record_id(record_input)
    except ValueError as e:
        st.sidebar.error(str(e))
        st.stop()

    versions = fetch_versions(record_id)
    st.sidebar.success(f"Found {len(versions)} version(s)")

    # Save versions summary
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    versions_summary = []
    for i, v in enumerate(versions):
        meta = v.get("metadata", {})
        versions_summary.append(
            {
                "index": i + 1,
                "id": v["id"],
                "version": meta.get("version", f"v{i+1}"),
                "title": meta.get("title", ""),
                "publication_date": meta.get("publication_date", ""),
            }
        )
    (OUTPUT_DIR / "versions.json").write_text(json.dumps(versions_summary, indent=2))

    # Download (incremental - skips already downloaded)
    progress = st.sidebar.progress(0, text="Downloading...")
    for i, version in enumerate(versions):
        record_id = version["id"]
        version_label = version.get("metadata", {}).get("version", f"v{i+1}")
        version_dir = DOWNLOADS_DIR / f"{i+1:03d}_{version_label}_{record_id}"
        version_dir.mkdir(parents=True, exist_ok=True)

        # Download files
        import hashlib
        import requests

        files = version.get("files", [])
        for file_info in files:
            filename = file_info["key"]
            file_url = file_info["links"]["self"]
            dest = version_dir / filename

            if dest.exists():
                existing_md5 = hashlib.md5(dest.read_bytes()).hexdigest()
                if existing_md5 == file_info.get("checksum", "").replace("md5:", ""):
                    continue  # Already downloaded

            headers = {"Authorization": f"Bearer {token}"} if token else {}
            resp = requests.get(file_url, headers=headers, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

        # Save metadata
        (version_dir / "_metadata.json").write_text(json.dumps(version, indent=2))

        # Extract & convert
        extract_archives(version_dir)
        target_files = find_target_files(version_dir)
        if target_files["pdf"] or target_files["tex"]:
            convert_pdfs(version_dir, target_files)

        progress.progress((i + 1) / len(versions), text=f"Version {i+1}/{len(versions)}")

    progress.empty()
    st.sidebar.success("All versions downloaded and converted!")
    st.rerun()

# ---------------------------------------------------------------------------
# Main content: Tabs
# ---------------------------------------------------------------------------

text_dirs = load_text_dirs()

if not text_dirs:
    st.info("No data yet. Enter a Zenodo record URL and click 'Fetch & Download Versions' in the sidebar.")
    st.stop()

st.sidebar.markdown("---")
st.sidebar.markdown(f"**{len(text_dirs)} version(s) loaded**")
for label, _ in text_dirs:
    st.sidebar.markdown(f"- `{label}`")

tab_diff, tab_search, tab_first, tab_browse = st.tabs(
    ["Diff Viewer", "Search", "First Occurrence", "Browse"]
)

# ---------------------------------------------------------------------------
# Tab 1: Diff Viewer (using streamlit-diff-viewer)
# ---------------------------------------------------------------------------

with tab_diff:
    st.header("Compare Versions")

    col1, col2 = st.columns(2)
    labels = [label for label, _ in text_dirs]

    with col1:
        idx_a = st.selectbox("Version A (left)", range(len(labels)), format_func=lambda i: labels[i], key="diff_a")
    with col2:
        default_b = min(idx_a + 1, len(labels) - 1)
        idx_b = st.selectbox("Version B (right)", range(len(labels)), index=default_b, format_func=lambda i: labels[i], key="diff_b")

    texts_a = load_version_text(text_dirs[idx_a][1])
    texts_b = load_version_text(text_dirs[idx_b][1])

    all_files = sorted(set(texts_a.keys()) | set(texts_b.keys()))

    if not all_files:
        st.warning("No text files found in selected versions.")
    else:
        selected_file = st.selectbox("File", all_files)

        text_a = texts_a.get(selected_file, "")
        text_b = texts_b.get(selected_file, "")

        if text_a == text_b:
            st.success("Files are identical.")
        else:
            diff_viewer(
                old_text=text_a,
                new_text=text_b,
                lang="text",
            )

# ---------------------------------------------------------------------------
# Tab 2: Search across all versions
# ---------------------------------------------------------------------------

with tab_search:
    st.header("Search Across All Versions")

    query = st.text_input("Search term or phrase", key="search_query")
    case_sensitive = st.checkbox("Case sensitive", value=False)

    if query:
        results = search_in_versions(text_dirs, query, case_sensitive=case_sensitive)

        if not results:
            st.warning(f"No results for '{query}'")
        else:
            st.success(f"Found {len(results)} match(es) across versions")

            # Group by version
            by_version = {}
            for r in results:
                by_version.setdefault(r["version"], []).append(r)

            for version_label, matches in by_version.items():
                with st.expander(f"{version_label} ({len(matches)} matches)", expanded=len(by_version) <= 3):
                    for m in matches[:50]:
                        st.markdown(f"**{m['file']}** line {m['line']}")
                        # Highlight the match in context
                        highlighted = re.sub(
                            f"({re.escape(query)})",
                            r"**\1**",
                            m["context"],
                            flags=0 if case_sensitive else re.IGNORECASE,
                        )
                        st.code(m["context"], language="markdown")

# ---------------------------------------------------------------------------
# Tab 3: First Occurrence tracker
# ---------------------------------------------------------------------------

with tab_first:
    st.header("Find First Occurrence")
    st.markdown("Enter a word or phrase to find the first version where it appears.")

    phrase = st.text_input("Word or phrase", key="first_occ_query")

    if phrase:
        first = find_first_occurrence(text_dirs, phrase)
        if first:
            st.success(f"First appears in: **{first['version']}** (file: `{first['file']}`)")

            # Show which versions contain it
            st.markdown("#### Presence across versions:")
            presence = []
            for label, text_dir in text_dirs:
                found = False
                for f in sorted(text_dir.iterdir()):
                    if f.is_file() and f.suffix in (".md", ".tex"):
                        content = f.read_text(encoding="utf-8", errors="replace")
                        if re.search(re.escape(phrase), content, re.IGNORECASE):
                            found = True
                            break
                presence.append({"Version": label, "Contains phrase": "Yes" if found else "No"})

            st.dataframe(presence, use_container_width=True)
        else:
            st.warning(f"'{phrase}' not found in any version.")

# ---------------------------------------------------------------------------
# Tab 4: Browse versions
# ---------------------------------------------------------------------------

with tab_browse:
    st.header("Browse Version Content")

    idx = st.selectbox("Select version", range(len(labels)), format_func=lambda i: labels[i], key="browse_v")
    texts = load_version_text(text_dirs[idx][1])

    if not texts:
        st.warning("No text files in this version.")
    else:
        file_name = st.selectbox("File", sorted(texts.keys()), key="browse_f")
        content = texts[file_name]

        st.markdown(f"**{len(content)} characters, {len(content.splitlines())} lines**")

        # Optionally search within this file
        local_search = st.text_input("Filter/highlight in this file", key="browse_search")
        if local_search:
            lines = content.splitlines()
            matching_lines = [
                (i, line)
                for i, line in enumerate(lines, 1)
                if re.search(re.escape(local_search), line, re.IGNORECASE)
            ]
            st.info(f"{len(matching_lines)} matching line(s)")
            for line_num, line in matching_lines[:100]:
                st.text(f"L{line_num}: {line}")
        else:
            st.code(content[:50000], language="markdown")
