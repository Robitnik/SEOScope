"""Site Monitor (task-35): robots/sitemap/pages/DNS with full change history.

Self-contained on stdlib — see this task's plan §1.6 for why (`dnspython` is
not in the image; `httpx`/`lxml` are present only transitively through the
optional `local` dependency group and are absent on CI/deploy).
"""
