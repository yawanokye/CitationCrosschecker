"""Bound web-process memory without changing analysis or stored manuscripts."""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import zipfile
from collections import OrderedDict
from collections.abc import MutableMapping
from xml.etree.ElementTree import iterparse
from starlette.exceptions import HTTPException


def _positive_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


MAX_UPLOAD_BYTES = _positive_int("MAX_UPLOAD_MB", 64) * 1024 * 1024
PREFLIGHT_BYTES = _positive_int("WEB_PREFLIGHT_MAX_MB", 8) * 1024 * 1024


def _deep_size(value, seen=None):
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_deep_size(key, seen) + _deep_size(item, seen) for key, item in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(_deep_size(item, seen) for item in value)
    return size


class BoundedJobStore(MutableMapping):
    """Short-lived fallback only. Production records are loaded from PostgreSQL."""
    def __init__(self, max_entries=8, max_bytes=32*1024*1024, ttl=300, clock=time.monotonic):
        self.entries = OrderedDict()
        self.max_entries, self.max_bytes, self.ttl = max_entries, max_bytes, ttl
        self.clock = clock
        self.lock = threading.RLock()

    def _prune(self):
        now = self.clock()
        for key in tuple(self.entries):
            if self.entries[key][0] <= now:
                del self.entries[key]
        # Recount because legacy callers sometimes mutate a record in place.
        sizes = {key: _deep_size(value[1]) for key, value in self.entries.items()}
        total = sum(sizes.values())
        for key in tuple(self.entries):
            if sizes[key] > self.max_bytes:
                del self.entries[key]
                total -= sizes[key]
        while self.entries and (len(self.entries) > self.max_entries or total > self.max_bytes):
            key, _ = self.entries.popitem(last=False)
            total -= sizes[key]

    def __getitem__(self, key):
        with self.lock:
            self._prune()
            value = self.entries[key][1]
            self.entries.move_to_end(key)
            return value

    def __setitem__(self, key, value):
        with self.lock:
            self.entries[key] = (self.clock() + self.ttl, value)
            self.entries.move_to_end(key)
            self._prune()

    def __delitem__(self, key):
        with self.lock:
            del self.entries[key]

    def __iter__(self):
        with self.lock:
            self._prune()
            return iter(tuple(self.entries))

    def __len__(self):
        with self.lock:
            self._prune()
            return len(self.entries)

    def items(self):
        with self.lock:
            self._prune()
            return [(key, value[1]) for key, value in self.entries.items()]


class _UploadTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "Upload exceeds the allowed file size.")


class WebMemoryGuard:
    """Limit expensive requests until the complete response has been sent.

    Admit before multipart parsing. File-size limits also cover chunked bodies.
    Health checks, payment callbacks, webhooks and static pages bypass this gate.
    """
    prefixes = ("/verify", "/result/", "/online/", "/api/report-package/", "/api/plans/recommend/",
                "/api/corrections/", "/api/manual-", "/api/certificate/",
                "/export-", "/apply-autofix", "/api/academic-voice/")

    def __init__(self, app, max_active=None, max_upload_bytes=MAX_UPLOAD_BYTES):
        self.app = app
        self.max_active = max_active or _positive_int("WEB_MAX_HEAVY_REQUESTS", 2)
        self.max_upload_bytes = max_upload_bytes
        self.active = 0
        self.lock = threading.Lock()

    async def _reject(self, send, status, message):
        body = json.dumps({"detail": message, "error": message}).encode()
        await send({"type":"http.response.start", "status":status,
                    "headers":[(b"content-type", b"application/json"), (b"retry-after", b"5")]})
        await send({"type":"http.response.body", "body":body})

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(self.prefixes):
            return await self.app(scope, receive, send)
        upload = path == "/verify" and scope.get("method") == "POST"
        # Permit a small allowance for multipart headers and form fields.
        body_limit = self.max_upload_bytes + 1024*1024
        headers = dict(scope.get("headers") or [])
        if upload:
            try:
                if int(headers.get(b"content-length", b"0")) > body_limit:
                    return await self._reject(send, 413, "Upload exceeds the allowed file size.")
            except ValueError:
                return await self._reject(send, 400, "Invalid upload length.")
        with self.lock:
            admitted = self.active < self.max_active
            if admitted:
                self.active += 1
        if not admitted:
            return await self._reject(send, 429, "The service is handling other documents. Please try again shortly.")
        received = 0
        started = False
        async def bounded_receive():
            nonlocal received
            message = await receive()
            if upload and message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    raise _UploadTooLarge()
            return message
        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)
        try:
            await self.app(scope, bounded_receive, tracked_send)
        except _UploadTooLarge:
            if not started:
                await self._reject(send, 413, "Upload exceeds the allowed file size.")
        finally:
            with self.lock:
                self.active -= 1


def estimate_upload_load(file_bytes, filename, *, pdf_reader=None, normal_pdf_pages=200, normal_docx_words=80000):
    """Conservative queue routing. Large or uncertain files are parsed by workers."""
    kind = str(filename or "").lower().rsplit(".", 1)[-1]
    info = {"filename":filename, "file_size_mb":round(len(file_bytes)/(1024*1024), 2),
            "file_type":kind, "page_count":None, "word_count":None,
            "large_file":True, "queue_name":"large_document_processing",
            "route_reason":"File requires background preflight."}
    if len(file_bytes) > PREFLIGHT_BYTES:
        return info
    try:
        if kind == "pdf" and pdf_reader:
            pages = len(pdf_reader(io.BytesIO(file_bytes)).pages)
            info["page_count"] = pages
            large = pages > normal_pdf_pages
        elif kind == "docx":
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
                document = archive.getinfo("word/document.xml")
                if document.file_size > PREFLIGHT_BYTES:
                    return info
                # Do not load images, embedded workbooks, styles or relationship parts.
                # Reject DTD/entity declarations before using the stdlib XML reader.
                with archive.open(document) as stream:
                    tail = b""
                    while chunk := stream.read(65536):
                        scanned = (tail + chunk).upper()
                        if b"<!DOCTYPE" in scanned or b"<!ENTITY" in scanned:
                            return info
                        tail = scanned[-16:]
                words = 0
                with archive.open(document) as stream:
                    for _, element in iterparse(stream, events=("end",)):
                        if element.tag == "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p":
                            text = "".join(node.text or "" for node in element.iter() if node.tag.endswith("}t"))
                            words += sum(1 for _ in re.finditer(r"\b\w+\b", text))
                            element.clear()
                info["word_count"] = words
                large = words > normal_docx_words
        else:
            return info
        info.update({"large_file":large,
                     "queue_name":"large_document_processing" if large else "document_processing",
                     "route_reason":"Background queue selected from bounded preflight."})
    except Exception:
        # An unreadable preflight must not lead to expensive web-process parsing.
        pass
    return info
