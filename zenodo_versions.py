#!/usr/bin/env python3
"""
Zenodo Version Tracker
======================
Downloads all versions of a Zenodo record, extracts PDF/TeX files,
converts PDFs to Markdown, and provides diff visualization + search.

Dependencies:
    pip install requests pymupdf4llm pymupdf rich jinja2

Optional (better PDF conversion):
    pip install marker-pdf  # requires PyTorch, GPU recommended

Usage:
    python zenodo_versions.py https://zenodo.org/records/18437004
    python zenodo_versions.py 18437004
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_DIR = "zenodo_output"
ZENODO_API = "https://zenodo.org/api/records"

# ---------------------------------------------------------------------------
# 1. Zenodo API: Retrieve all versions
# ---------------------------------------------------------------------------


def parse_record_id(input_str: str) -> str:
    """Extract record ID from URL or plain ID."""
    # URL like https://zenodo.org/records/18437004
    match = re.search(r"records?/(\d+)", input_str)
    if match:
        return match.group(1)
    # Plain number
    if input_str.strip().isdigit():
        return input_str.strip()
    raise ValueError(f"Cannot parse Zenodo record ID from: {input_str}")


def get_record_metadata(record_id: str) -> dict:
    """Get metadata for a single record."""
    resp = requests.get(f"{ZENODO_API}/{record_id}")
    resp.raise_for_status()
    return resp.json()


def get_all_versions(record_id: str) -> list[dict]:
    """
    Get all versions of a record using its conceptrecid.
    Returns list of record metadata sorted by version (oldest first).
    """
    # First, get the record to find its conceptrecid
    meta = get_record_metadata(record_id)
    concept_recid = meta.get("conceptrecid", meta.get("conceptrecid"))

    if not concept_recid:
        print(f"[!] No conceptrecid found, treating as single version")
        return [meta]

    # Query all versions (max 25 per page without auth, 100 with token)
    versions = []
    page = 1
    while True:
        resp = requests.get(
            ZENODO_API,
            params={
                "q": f"conceptrecid:{concept_recid}",
                "all_versions": "true",
                "sort": "oldest",
                "size": 25,
                "page": page,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break
        versions.extend(hits)
        if len(versions) >= data["hits"]["total"]:
            break
        page += 1

    # Sort by creation date (oldest first)
    versions.sort(key=lambda v: v.get("created", ""))
    return versions


# ---------------------------------------------------------------------------
# 2. Download files
# ---------------------------------------------------------------------------


def download_file(url: str, dest: Path, token: str = None):
    """Download a file with progress indication."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(url, headers=headers, stream=True)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = downloaded * 100 // total
                print(f"\r  Downloading: {pct}% ({downloaded}/{total})", end="")
    print()


def download_all_versions(versions: list[dict], base_dir: Path, token: str = None):
    """Download all files for all versions."""
    for i, version in enumerate(versions):
        record_id = version["id"]
        version_label = version.get("metadata", {}).get("version", f"v{i+1}")
        pub_date = version.get("metadata", {}).get("publication_date", "unknown")
        version_dir = base_dir / f"{i+1:03d}_{version_label}_{record_id}"
        version_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'='*60}")
        print(f"Version {i+1}: {version_label} (published {pub_date}, id={record_id})")
        print(f"{'='*60}")

        files = version.get("files", [])
        if not files:
            print("  No files found for this version")
            continue

        for file_info in files:
            filename = file_info["key"]
            file_url = file_info["links"]["self"]
            dest = version_dir / filename

            if dest.exists():
                # Check checksum
                existing_md5 = hashlib.md5(dest.read_bytes()).hexdigest()
                if existing_md5 == file_info.get("checksum", "").replace("md5:", ""):
                    print(f"  [skip] {filename} (already downloaded)")
                    continue

            print(f"  [dl] {filename} ({file_info.get('size', '?')} bytes)")
            download_file(file_url, dest, token)

        # Save metadata
        meta_file = version_dir / "_metadata.json"
        with open(meta_file, "w") as f:
            json.dump(version, f, indent=2)

    return base_dir


# ---------------------------------------------------------------------------
# 3. Extract archives and find PDF/TeX files
# ---------------------------------------------------------------------------


def extract_archives(version_dir: Path) -> list[Path]:
    """Extract zip/tar archives and return list of all extracted paths."""
    extracted = []

    for archive in version_dir.iterdir():
        if archive.name.startswith("_"):
            continue

        if zipfile.is_zipfile(archive):
            print(f"  [unzip] {archive.name}")
            extract_dir = version_dir / f"_extracted_{archive.stem}"
            with zipfile.ZipFile(archive, "r") as zf:
                zf.extractall(extract_dir)
            extracted.append(extract_dir)

        elif tarfile.is_tarfile(str(archive)):
            print(f"  [untar] {archive.name}")
            extract_dir = version_dir / f"_extracted_{archive.stem}"
            extract_dir.mkdir(exist_ok=True)
            with tarfile.open(archive, "r:*") as tf:
                tf.extractall(extract_dir, filter="data")
            extracted.append(extract_dir)

    return extracted


def find_target_files(version_dir: Path) -> dict[str, list[Path]]:
    """Find all PDF and TeX files recursively."""
    results = {"pdf": [], "tex": []}

    for path in version_dir.rglob("*"):
        if path.is_file():
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                results["pdf"].append(path)
            elif suffix in (".tex", ".latex"):
                results["tex"].append(path)

    return results


# ---------------------------------------------------------------------------
# 4. Convert PDFs to text (Markdown)
# ---------------------------------------------------------------------------


def convert_pdf_pymupdf4llm(pdf_path: Path) -> str:
    """Convert PDF to Markdown using pymupdf4llm (lightweight, no GPU)."""
    try:
        import pymupdf4llm

        md_text = pymupdf4llm.to_markdown(str(pdf_path))
        return md_text
    except ImportError:
        print("  [!] pymupdf4llm not installed, trying pymupdf fallback")
        return convert_pdf_pymupdf(pdf_path)


def convert_pdf_pymupdf(pdf_path: Path) -> str:
    """Fallback: extract text with pymupdf."""
    try:
        import pymupdf

        doc = pymupdf.open(str(pdf_path))
        text = ""
        for page in doc:
            text += page.get_text() + "\n\n"
        return text
    except ImportError:
        print("  [!] pymupdf not installed either, skipping PDF")
        return ""


def convert_pdf_marker(pdf_path: Path) -> str:
    """Convert PDF to Markdown using marker-pdf (high quality, needs PyTorch)."""
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict

        models = create_model_dict()
        converter = PdfConverter(artifact_dict=models)
        rendered = converter(str(pdf_path))
        return rendered.markdown
    except ImportError:
        print("  [!] marker-pdf not installed, falling back to pymupdf4llm")
        return convert_pdf_pymupdf4llm(pdf_path)


def convert_pdfs(version_dir: Path, files: dict, use_marker: bool = False) -> Path:
    """Convert all PDFs to markdown, save alongside originals."""
    text_dir = version_dir / "_text"
    text_dir.mkdir(exist_ok=True)

    converter = convert_pdf_marker if use_marker else convert_pdf_pymupdf4llm

    for pdf_path in files["pdf"]:
        relative = pdf_path.relative_to(version_dir)
        out_path = text_dir / f"{relative.stem}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if out_path.exists():
            print(f"  [skip] {relative} (already converted)")
            continue

        print(f"  [convert] {relative} -> {out_path.name}")
        text = converter(pdf_path)
        out_path.write_text(text, encoding="utf-8")

    # Copy TeX files to text dir too
    for tex_path in files["tex"]:
        relative = tex_path.relative_to(version_dir)
        out_path = text_dir / relative.name
        if not out_path.exists():
            shutil.copy2(tex_path, out_path)
            print(f"  [copy tex] {relative}")

    return text_dir


# ---------------------------------------------------------------------------
# 5. Diff visualization
# ---------------------------------------------------------------------------


def generate_diff_html(text_a: str, text_b: str, label_a: str, label_b: str) -> str:
    """Generate HTML diff between two texts."""
    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()

    differ = difflib.HtmlDiff(wrapcolumn=80)
    html = differ.make_file(lines_a, lines_b, fromdesc=label_a, todesc=label_b)
    return html


def generate_unified_diff(text_a: str, text_b: str, label_a: str, label_b: str) -> str:
    """Generate unified diff."""
    lines_a = text_a.splitlines(keepends=True)
    lines_b = text_b.splitlines(keepends=True)

    diff = difflib.unified_diff(lines_a, lines_b, fromfile=label_a, tofile=label_b)
    return "".join(diff)


def build_diff_report(text_dirs: list[tuple[str, Path]], output_dir: Path):
    """Build HTML diff reports between consecutive versions."""
    diff_dir = output_dir / "diffs"
    diff_dir.mkdir(exist_ok=True)

    # Collect all text files per version
    version_texts = {}
    for label, text_dir in text_dirs:
        if not text_dir.exists():
            continue
        texts = {}
        for f in sorted(text_dir.iterdir()):
            if f.is_file() and f.suffix in (".md", ".tex"):
                texts[f.name] = f.read_text(encoding="utf-8", errors="replace")
        version_texts[label] = texts

    labels = list(version_texts.keys())

    # Generate pairwise diffs (consecutive versions)
    index_entries = []
    for i in range(len(labels) - 1):
        label_a = labels[i]
        label_b = labels[i + 1]
        texts_a = version_texts[label_a]
        texts_b = version_texts[label_b]

        # Match files by name
        all_files = sorted(set(texts_a.keys()) | set(texts_b.keys()))
        for fname in all_files:
            text_a = texts_a.get(fname, "")
            text_b = texts_b.get(fname, "")

            if text_a == text_b:
                continue  # No changes

            diff_filename = f"diff_{i+1:03d}_{label_a}_vs_{label_b}_{fname}.html"
            diff_path = diff_dir / diff_filename

            html = generate_diff_html(
                text_a, text_b, f"{label_a}/{fname}", f"{label_b}/{fname}"
            )
            diff_path.write_text(html, encoding="utf-8")
            index_entries.append((label_a, label_b, fname, diff_filename))

    # Generate index
    index_html = build_diff_index(index_entries, labels)
    (diff_dir / "index.html").write_text(index_html, encoding="utf-8")
    print(f"\n[+] Diff report: {diff_dir / 'index.html'}")
    return diff_dir


def build_diff_index(entries: list, labels: list) -> str:
    """Build an HTML index page for all diffs."""
    rows = ""
    for label_a, label_b, fname, diff_file in entries:
        rows += f'<tr><td>{label_a}</td><td>{label_b}</td><td>{fname}</td>'
        rows += f'<td><a href="{diff_file}">View diff</a></td></tr>\n'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Zenodo Version Diffs</title>
<style>
body {{ font-family: sans-serif; margin: 2em; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #4CAF50; color: white; }}
tr:nth-child(even) {{ background: #f2f2f2; }}
h1 {{ color: #333; }}
</style></head>
<body>
<h1>Version Diffs</h1>
<p>Versions found: {', '.join(labels)}</p>
<table>
<tr><th>From</th><th>To</th><th>File</th><th>Diff</th></tr>
{rows}
</table>
</body></html>"""


# ---------------------------------------------------------------------------
# 6. Search across versions
# ---------------------------------------------------------------------------


def search_in_versions(
    text_dirs: list[tuple[str, Path]], query: str, case_sensitive: bool = False
) -> list[dict]:
    """Search for a term/phrase across all versions. Returns matches with context."""
    results = []
    flags = 0 if case_sensitive else re.IGNORECASE

    for label, text_dir in text_dirs:
        if not text_dir.exists():
            continue
        for f in sorted(text_dir.iterdir()):
            if not f.is_file() or f.suffix not in (".md", ".tex"):
                continue
            content = f.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()

            for line_num, line in enumerate(lines, 1):
                if re.search(re.escape(query), line, flags):
                    # Get context (2 lines before/after)
                    start = max(0, line_num - 3)
                    end = min(len(lines), line_num + 2)
                    context = lines[start:end]

                    results.append(
                        {
                            "version": label,
                            "file": f.name,
                            "line": line_num,
                            "match_line": line.strip(),
                            "context": "\n".join(context),
                        }
                    )

    return results


def find_first_occurrence(
    text_dirs: list[tuple[str, Path]], query: str
) -> dict | None:
    """Find the first version where a term/phrase appears."""
    for label, text_dir in text_dirs:
        if not text_dir.exists():
            continue
        for f in sorted(text_dir.iterdir()):
            if not f.is_file() or f.suffix not in (".md", ".tex"):
                continue
            content = f.read_text(encoding="utf-8", errors="replace")
            if re.search(re.escape(query), content, re.IGNORECASE):
                return {"version": label, "file": f.name}
    return None


def build_search_report(
    text_dirs: list[tuple[str, Path]], queries: list[str], output_dir: Path
):
    """Generate HTML search report for given queries."""
    search_dir = output_dir / "search"
    search_dir.mkdir(exist_ok=True)

    report_parts = []
    for query in queries:
        results = search_in_versions(text_dirs, query)
        first = find_first_occurrence(text_dirs, query)

        part = f"<h2>Query: &quot;{query}&quot;</h2>\n"
        if first:
            part += f"<p><strong>First occurrence:</strong> {first['version']} ({first['file']})</p>\n"
        else:
            part += "<p><em>Not found in any version.</em></p>\n"

        if results:
            part += f"<p>Found in {len(results)} location(s):</p>\n<ul>\n"
            for r in results[:50]:  # Limit display
                part += f"<li><strong>{r['version']}</strong> / {r['file']} (line {r['line']}): "
                part += f"<code>{r['match_line'][:200]}</code></li>\n"
            part += "</ul>\n"

        report_parts.append(part)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Search Results</title>
<style>
body {{ font-family: sans-serif; margin: 2em; }}
code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }}
h2 {{ color: #2196F3; border-bottom: 1px solid #ddd; padding-bottom: 5px; }}
</style></head>
<body>
<h1>Search Results Across Versions</h1>
{''.join(report_parts)}
</body></html>"""

    report_path = search_dir / "search_results.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"[+] Search report: {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# 7. Interactive CLI
# ---------------------------------------------------------------------------


def interactive_mode(text_dirs: list[tuple[str, Path]], output_dir: Path):
    """Simple interactive search/diff CLI."""
    print("\n" + "=" * 60)
    print("INTERACTIVE MODE")
    print("=" * 60)
    print("Commands:")
    print("  search <term>      - Search across all versions")
    print("  first <term>       - Find first occurrence of a term")
    print("  diff <v1> <v2>     - Show diff between two version numbers")
    print("  versions           - List all versions")
    print("  quit               - Exit")
    print()

    labels = [label for label, _ in text_dirs]

    while True:
        try:
            cmd = input("zenodo> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not cmd:
            continue

        parts = cmd.split(maxsplit=1)
        action = parts[0].lower()

        if action == "quit" or action == "exit":
            break

        elif action == "versions":
            for i, label in enumerate(labels, 1):
                print(f"  {i}. {label}")

        elif action == "search" and len(parts) > 1:
            query = parts[1]
            results = search_in_versions(text_dirs, query)
            if not results:
                print(f"  No results for '{query}'")
            else:
                print(f"  Found {len(results)} match(es):")
                for r in results[:20]:
                    print(f"    [{r['version']}] {r['file']}:{r['line']} - {r['match_line'][:100]}")

        elif action == "first" and len(parts) > 1:
            query = parts[1]
            first = find_first_occurrence(text_dirs, query)
            if first:
                print(f"  First occurrence: {first['version']} ({first['file']})")
            else:
                print(f"  '{query}' not found in any version")

        elif action == "diff" and len(parts) > 1:
            try:
                nums = parts[1].split()
                v1, v2 = int(nums[0]) - 1, int(nums[1]) - 1
                label_a, dir_a = text_dirs[v1]
                label_b, dir_b = text_dirs[v2]

                files_a = {f.name: f for f in dir_a.iterdir() if f.suffix in (".md", ".tex")}
                files_b = {f.name: f for f in dir_b.iterdir() if f.suffix in (".md", ".tex")}
                all_files = sorted(set(files_a.keys()) | set(files_b.keys()))

                for fname in all_files:
                    text_a = files_a[fname].read_text(errors="replace") if fname in files_a else ""
                    text_b = files_b[fname].read_text(errors="replace") if fname in files_b else ""
                    diff = generate_unified_diff(text_a, text_b, f"{label_a}/{fname}", f"{label_b}/{fname}")
                    if diff:
                        print(diff[:3000])
                        if len(diff) > 3000:
                            print(f"  ... (truncated, full diff in HTML report)")
            except (ValueError, IndexError):
                print("  Usage: diff <version_num1> <version_num2>")

        else:
            print("  Unknown command. Type 'quit' to exit.")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Download and compare all versions of a Zenodo record"
    )
    parser.add_argument(
        "record", help="Zenodo record URL or ID (e.g., https://zenodo.org/records/18437004)"
    )
    parser.add_argument(
        "-o", "--output", default=OUTPUT_DIR, help="Output directory (default: zenodo_output)"
    )
    parser.add_argument(
        "-t", "--token", default=None, help="Zenodo API token (for higher rate limits)"
    )
    parser.add_argument(
        "--marker", action="store_true", help="Use marker-pdf for PDF conversion (better quality, needs PyTorch)"
    )
    parser.add_argument(
        "--search", nargs="*", help="Search terms to look for across all versions"
    )
    parser.add_argument(
        "--no-interactive", action="store_true", help="Skip interactive mode"
    )
    parser.add_argument(
        "--skip-download", action="store_true", help="Skip download (use existing files)"
    )

    args = parser.parse_args()

    record_id = parse_record_id(args.record)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    downloads_dir = output_dir / "downloads"
    downloads_dir.mkdir(exist_ok=True)

    # Step 1: Get all versions
    print(f"[*] Fetching all versions for record {record_id}...")
    versions = get_all_versions(record_id)
    print(f"[+] Found {len(versions)} version(s)")

    # Save version list
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
                "doi": v.get("doi", ""),
            }
        )
    (output_dir / "versions.json").write_text(json.dumps(versions_summary, indent=2))

    # Step 2: Download all files
    if not args.skip_download:
        print(f"\n[*] Downloading files to {downloads_dir}/...")
        download_all_versions(versions, downloads_dir, args.token)

    # Step 3: Extract archives and find PDF/TeX
    print(f"\n[*] Extracting archives and finding PDF/TeX files...")
    text_dirs = []

    for version_dir in sorted(downloads_dir.iterdir()):
        if not version_dir.is_dir():
            continue

        print(f"\n--- {version_dir.name} ---")
        extract_archives(version_dir)
        files = find_target_files(version_dir)

        print(f"  Found: {len(files['pdf'])} PDFs, {len(files['tex'])} TeX files")

        # Step 4: Convert PDFs
        if files["pdf"] or files["tex"]:
            text_dir = convert_pdfs(version_dir, files, use_marker=args.marker)
            label = version_dir.name
            text_dirs.append((label, text_dir))

    if not text_dirs:
        print("\n[!] No text content found in any version. Exiting.")
        sys.exit(1)

    # Step 5: Generate diffs
    print(f"\n[*] Generating diff reports...")
    build_diff_report(text_dirs, output_dir)

    # Step 6: Search
    if args.search:
        print(f"\n[*] Searching for: {args.search}")
        build_search_report(text_dirs, args.search, output_dir)

    # Step 7: Interactive mode
    if not args.no_interactive:
        interactive_mode(text_dirs, output_dir)

    print(f"\n[+] Done! Output in: {output_dir}/")
    print(f"    - Downloads:  {downloads_dir}/")
    print(f"    - Diffs:      {output_dir}/diffs/index.html")
    if args.search:
        print(f"    - Search:     {output_dir}/search/search_results.html")


if __name__ == "__main__":
    main()
