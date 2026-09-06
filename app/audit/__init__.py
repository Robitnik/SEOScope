"""Журнал дій (task-72): «хто що коли зробив»."""

from __future__ import annotations

from app.audit.recorder import record_audit
from app.audit.sink import AuditRecord, AuditSink, get_audit_sink

__all__ = ["AuditRecord", "AuditSink", "get_audit_sink", "record_audit"]
