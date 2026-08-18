"""Reports engine (task-42): client-report generation via two independent
paths (Reporting Ninja REST API, and headless-browser PDF export) to the
same 8-metric x N-column table.

Ported from `/seo/AISeoTools/seo_audit/clientreport/**` and reworked per
task-42 §1.3/§2.1: the API path's attribution bug (`sessionMedium` +
summed-rows totals, disagreeing with the PDF by 5-15% on organic/direct) is
fixed here, not carried over. AISeoTools itself is not modified — it stays a
read-only mount (task-42 §2.1).
"""
