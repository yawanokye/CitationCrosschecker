# Publication-event data

`retraction_watch.sqlite3` is a compact DOI/event index generated from the
official Crossref Retraction Watch data repository:

https://gitlab.com/crossref/retraction-watch-data

Snapshot date: **2026-09-15**  
Source commit: `d9e0bfa1ac8d644dfb1e9ebbbbe10ac1b8c43f28`

The index contains only fields needed for publication-status verification:
original DOI, event type and label, notice DOI, event date, source, and Retraction
Watch record identifier. It also records DOI values that identify publication
notices so CiteIntegrity does not treat a notice cited in its own right as a
retracted research work.

Refresh the index before future releases with:

```bash
python scripts/build_retraction_watch_index.py \
  --csv /path/to/retraction_watch.csv \
  --output data/retraction_watch.sqlite3 \
  --dataset-date YYYY-MM-DD \
  --source-commit COMMIT_SHA
```

Crossref states that the dataset is public and is updated each working day.
See its official documentation:

https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/
