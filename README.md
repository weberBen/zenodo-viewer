# zenodo-viewer

![screenshot](assets/app.png)

Tool to track and compare all versions of a Zenodo record. Downloads every version, extracts PDF/TeX files, converts PDFs to Markdown, and lets you diff and search across the full history.

## Install

```
uv tool install .
```

Or in a venv:

```
uv sync
```

## Usage

### Web UI (Streamlit)

```
zenodo_viewer
# or
zw
```

Features:
- Interactive timeline of all versions
- Side-by-side diff between any two versions
- Full-text search across all versions
- Browse converted content per version

### Workflow

1. **Output directory** — Set the local path where all downloaded files, converted text, and metadata will be stored. This is persisted between sessions. The folder structure is created automatically.

2. **Fetch & Download** — Enter a Zenodo record URL or ID, then click "Fetch & Download". The app queries the Zenodo API to find all versions of that record (via `conceptrecid`), downloads every file, extracts archives, converts PDFs to Markdown, and stores everything in the output directory. Already-downloaded versions are skipped (checked via MD5).

### CLI

```
python zenodo_versions.py https://zenodo.org/records/18437004
python zenodo_versions.py 18437004 -o my_folder --token MY_TOKEN
python zenodo_versions.py 18437004 --search "term1" "term2"
python zenodo_versions.py 18437004 --no-interactive --skip-download
```

Options:
- `-o, --output` — output directory (default: `zenodo_output`)
- `-t, --token` — Zenodo API token (higher rate limits)
- `--marker` — use marker-pdf for conversion (better quality, needs PyTorch + GPU)
- `--search` — terms to search across all versions
- `--skip-download` — use existing files, don't re-download
- `--no-interactive` — skip interactive CLI mode

## Dependencies

- Python >= 3.12
- requests, pymupdf, pymupdf4llm, streamlit, streamlit-vis-timeline

Optional: `marker-pdf` for higher quality PDF conversion.

## Output structure

```
<output_dir>/
  downloads/
    001_v1_12345/
      file.pdf
      _metadata.json
      _text/
        file.md
    002_v2_12346/
      ...
  diffs/
    index.html
  versions.json
```
