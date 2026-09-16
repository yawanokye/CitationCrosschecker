# Bundled publication-integrity data

`retraction_watch.sqlite3` is the compact DOI index used for fast publication-
status checks. It was generated from Crossref's official Retraction Watch data
snapshot dated 15 September 2026.

The deployed file must remain at `data/retraction_watch.sqlite3`. The application
also accepts the legacy project-root location for compatibility, but the `data/`
path is canonical.

Refresh the snapshot before a future release with:

```bash
python scripts/build_retraction_watch_index.py \
  --csv /path/to/retraction_watch.csv \
  --output data/retraction_watch.sqlite3 \
  --dataset-date YYYY-MM-DD \
  --source-commit COMMIT_SHA
```
