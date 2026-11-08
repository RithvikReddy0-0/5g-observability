#!/usr/bin/env python3
"""
trace.py — structured decision events and spans from our own components (ADR-024).

WHY OUR OWN
Open5GS v2.8.0 has no tracing: no OpenTelemetry, OTLP, Jaeger or Zipkin code anywhere in the pinned
source (157f611a), and its `-t` flag only raises the log level. UERANSIM has none either. What CAN be
traced is what this project adds above the core: the slice orchestrator's admit / refuse / pre-empt
decisions and controller moves, the network orchestrator's lifecycle operations with the duration of
every step, the packet scheduler's re-applies, the KPI runner's phases. Those are the decisions an
operator needs to line up against a KPI breach.

WHAT IT WRITES
One JSON object per line, appended to OBS_TRACE_FILE:

    {"type": "span", "service": "network-orchestrator", "name": "netorch.activate",
     "trace_id": "4bf9…", "span_id": "00f0…", "parent_id": null,
     "start": 1794212338.12, "end": 1794212350.31, "duration_ms": 12190.4,
     "status": "ok", "attrs": {"slice": "mMTC", "reason": "demand: …"}, "events": []}

An instant decision is a span with start == end ("type": "event"). Optionally the same spans go to an
OpenTelemetry collector over OTLP/HTTP with JSON encoding (POST <endpoint>/v1/traces; trace and span
ids as hex strings; times as decimal strings of nanoseconds — the OTLP specification's JSON rules), when
OTEL_EXPORTER_OTLP_ENDPOINT is set. No collector is deployed by default.

SAFETY
Tracing is OFF unless OBS_TRACE_FILE or OTEL_EXPORTER_OTLP_ENDPOINT is set, so tests and one-off CLI
runs write nothing. A tracing failure never reaches the caller: a full disk or a dead collector loses
spans, never a decision. The OTLP export runs on a daemon thread with a bounded queue.

    from trace import tracer
    T = tracer("slice-orchestrator")
    with T.span("admission.decide", supi=supi, traffic_class=cls) as sp:
        result = decide(...)
        sp.set(admitted=result["admitted"])
    T.event("controller.cut", slice="eMBB", frm=500, to=232, why="URLLC p95 11.7 ms > 8.0 ms")

Standard library only.
"""

import json
import os
import queue
import random
import threading
import time
import urllib.request

__all__ = ["Tracer", "tracer", "read_spans"]


def _hex(bits):
    return "%0*x" % (bits // 4, random.getrandbits(bits) or 1)


def _clean(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v][:50]
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in list(v.items())[:50]}
    return str(v)


class Span:
    def __init__(self, tracer, name, parent, attrs):
        self.tracer, self.name = tracer, name
        self.trace_id = parent.trace_id if parent else _hex(128)
        self.span_id = _hex(64)
        self.parent_id = parent.span_id if parent else None
        self.start = time.time()
        self.end = None
        self.status, self.error = "ok", None
        self.attrs = {k: _clean(v) for k, v in attrs.items()}
        self.events = []

    def set(self, **attrs):
        self.attrs.update({k: _clean(v) for k, v in attrs.items()})
        return self

    def add_event(self, name, **attrs):
        self.events.append({"t": time.time(), "name": name, "attrs": {k: _clean(v) for k, v in attrs.items()}})

    def fail(self, error):
        self.status, self.error = "error", str(error)[:500]

    def as_dict(self):
        end = self.end if self.end is not None else time.time()
        return {"type": "event" if end == self.start else "span", "service": self.tracer.service,
                "name": self.name, "trace_id": self.trace_id, "span_id": self.span_id,
                "parent_id": self.parent_id, "start": round(self.start, 6), "end": round(end, 6),
                "duration_ms": round((end - self.start) * 1000, 3), "status": self.status,
                "error": self.error, "attrs": self.attrs, "events": self.events}

    # context manager: nests through a per-thread stack
    def __enter__(self):
        self.tracer._stack().append(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        stack = self.tracer._stack()
        if stack and stack[-1] is self:
            stack.pop()
        if exc is not None and self.status == "ok":
            self.fail("%s: %s" % (exc_type.__name__, exc))
        self.end = time.time()
        self.tracer._emit(self)
        return False


class _NoopSpan:
    trace_id = span_id = parent_id = None

    def set(self, **attrs):
        return self

    def add_event(self, name, **attrs):
        pass

    def fail(self, error):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


NOOP = _NoopSpan()


class Tracer:
    def __init__(self, service, path=None, otlp_endpoint=None, enabled=None):
        self.service = service
        self.path = path if path is not None else os.environ.get("OBS_TRACE_FILE", "")
        self.otlp = (otlp_endpoint if otlp_endpoint is not None
                     else os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")).rstrip("/")
        self.enabled = bool(self.path or self.otlp) if enabled is None else enabled
        self._local = threading.local()
        self._lock = threading.Lock()
        self._q = None
        self.dropped = 0
        self.written = 0

    def _stack(self):
        st = getattr(self._local, "stack", None)
        if st is None:
            st = self._local.stack = []
        return st

    def current(self):
        st = self._stack()
        return st[-1] if st else None

    def span(self, name, **attrs):
        if not self.enabled:
            return NOOP
        return Span(self, name, self.current(), attrs)

    def event(self, name, **attrs):
        """An instant decision (start == end), a child of the current span if there is one."""
        if not self.enabled:
            return None
        sp = Span(self, name, self.current(), attrs)
        sp.end = sp.start
        self._emit(sp)
        return sp

    # ------------------------------------------------------------------ export
    def _emit(self, sp):
        rec = sp.as_dict()
        if self.path:
            try:
                line = json.dumps(rec, separators=(",", ":")) + "\n"
                with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                self.written += 1
            except (OSError, TypeError, ValueError):
                self.dropped += 1
        if self.otlp:
            if self._q is None:
                with self._lock:
                    if self._q is None:
                        self._q = queue.Queue(maxsize=1000)
                        threading.Thread(target=self._exporter, daemon=True).start()
            try:
                self._q.put_nowait(rec)
            except queue.Full:
                self.dropped += 1

    def _exporter(self):
        while True:
            batch = [self._q.get()]
            deadline = time.time() + 1.0
            while len(batch) < 100 and time.time() < deadline:
                try:
                    batch.append(self._q.get(timeout=max(0.0, deadline - time.time())))
                except queue.Empty:
                    break
            try:
                body = json.dumps(otlp_payload(self.service, batch)).encode()
                req = urllib.request.Request(self.otlp + "/v1/traces", data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                self.dropped += len(batch)


def _otlp_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    return {"stringValue": json.dumps(v)}


def _otlp_attrs(d):
    return [{"key": k, "value": _otlp_value(v)} for k, v in d.items() if v is not None]


def otlp_payload(service, records):
    """ExportTraceServiceRequest in OTLP/JSON: hex ids, nanosecond times as decimal strings,
    enums as integers (SPAN_KIND_INTERNAL = 1; STATUS_CODE_OK = 1, ERROR = 2)."""
    spans = []
    for r in records:
        sp = {"traceId": r["trace_id"], "spanId": r["span_id"], "name": r["name"], "kind": 1,
              "startTimeUnixNano": str(int(r["start"] * 1e9)), "endTimeUnixNano": str(int(r["end"] * 1e9)),
              "attributes": _otlp_attrs(r["attrs"]),
              "status": {"code": 2 if r["status"] == "error" else 1}}
        if r.get("error"):
            sp["status"]["message"] = r["error"]
        if r.get("parent_id"):
            sp["parentSpanId"] = r["parent_id"]
        if r.get("events"):
            sp["events"] = [{"timeUnixNano": str(int(e["t"] * 1e9)), "name": e["name"],
                             "attributes": _otlp_attrs(e["attrs"])} for e in r["events"]]
        spans.append(sp)
    return {"resourceSpans": [{
        "resource": {"attributes": _otlp_attrs({"service.name": service})},
        "scopeSpans": [{"scope": {"name": "5g-observability.trace"}, "spans": spans}]}]}


_TRACERS = {}


def tracer(service):
    """One Tracer per service, configured from the environment at first use."""
    if service not in _TRACERS:
        _TRACERS[service] = Tracer(service)
    return _TRACERS[service]


def read_spans(path, since=None, until=None):
    """Spans and events from a JSON-lines trace file, optionally within [since, until]."""
    out = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if since is not None and r.get("end", 0) < since:
                continue
            if until is not None and r.get("start", 0) > until:
                continue
            out.append(r)
    out.sort(key=lambda r: r.get("start", 0))
    return out


def read_netorch_ops(path, since=None, until=None):
    """The network orchestrator's own operation log (NETORCH_LOG, one JSON object per operation with
    its steps and their durations) as spans — a trace source that exists even without OBS_TRACE_FILE.
    Steps are laid end to end from the operation's start, which is how they ran (serially)."""
    out = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                op = json.loads(line)
            except ValueError:
                continue
            start = op.get("t0")
            if start is None:
                continue
            end = start + float(op.get("seconds") or 0)
            if (since is not None and end < since) or (until is not None and start > until):
                continue
            tid = _hex(128)
            root = {"type": "span", "service": "network-orchestrator", "name": "netorch.%s" % op.get("op"),
                    "trace_id": tid, "span_id": _hex(64), "parent_id": None, "start": start, "end": end,
                    "duration_ms": round((end - start) * 1000, 1),
                    "status": "ok" if op.get("result") == "done" else "error", "error": op.get("error"),
                    "attrs": {"slice": op.get("slice"), "result": op.get("result"), "reason": op.get("reason"),
                              "warnings": op.get("warnings")}, "events": []}
            out.append(root)
            t = start
            for st in op.get("steps", []):
                d = float(st.get("seconds") or 0)
                out.append({"type": "span", "service": "network-orchestrator", "name": "netorch.step",
                            "trace_id": tid, "span_id": _hex(64), "parent_id": root["span_id"],
                            "start": t, "end": t + d, "duration_ms": round(d * 1000, 1), "status": "ok",
                            "error": None, "attrs": {"slice": op.get("slice"), "step": st.get("step"),
                                                     "detail": st.get("detail")}, "events": []})
                t += d
    out.sort(key=lambda r: r["start"])
    return out
