# docs/instrumentation — Instrumentation Change Records (ICRs, ADR-004)

External instrumentation is the default and is always attempted first. Any source change to
free5GC is gated by an **ICR** (context, why external failed, exact change, revert steps) and kept
as a rebasable, behavior-preserving patch. **No ICRs exist yet** — none are permitted before the
external-first ladder (ADR-004) is exhausted.
