# kkFileView Offline Bundle

This directory is the offline bundle root used by `start.py`.

Expected local-only subdirectories:

- `downloads/`: kkFileView Windows zip and Windows x64 JRE zip.
- `runtime/`: extracted runtime created by `start.py`.
- `logs/`: kkFileView logs.

Run this on an internet-connected machine:

```powershell
.\venv\Scripts\python.exe prepare_kkfileview_offline.py
```

If kkFileView has no public release asset, set `KKFILEVIEW_DOWNLOAD_URL` to a reachable zip URL and run the command again.
