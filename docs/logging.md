# Logging — ADR-005 compliance record

**Status:** Gap documented; interim handling in place. No upstream patch.

ADR-005 mandates structured JSON logging as the machine-facing default **"provided it can be
enabled through configuration without invasive source changes"**, and requires that where an
NF cannot do so, *"that gap is logged and handled through the ADR-004 ladder"*. This is that
record.

## Finding: JSON output is NOT configurable in free5GC v4.2.3

Verified against the pinned source in `external/free5gc` (commit
`3b34a08e93a9b334f0f4005d3a3a9f79b66d59b9`), not assumed:

`NFs/amf/pkg/factory/config.go:114` — the entire logger configuration surface:

```go
type Logger struct {
    Enable       bool   `yaml:"enable"       valid:"type(bool)"`
    Level        string `yaml:"level"        valid:"required,in(trace|debug|info|warn|error|fatal|panic)"`
    ReportCaller bool   `yaml:"reportCaller" valid:"type(bool)"`
}
```

Three fields. There is no `format`, `formatter`, or `json` key.

Corroborating checks across the pinned tree:

| Check | Result |
|---|---|
| `grep 'yaml:"format"'` across all `NFs/` | no matches |
| `grep JSONFormatter` across the AMF tree | no matches |
| Where the formatter is set | `github.com/free5gc/util/logger`, an external Go module — not an NF config surface |

**Conclusion:** every NF emits logrus *text* output with a formatter fixed in a shared
library. JSON cannot be enabled by configuration, and doing so would require patching an
upstream dependency.

## Disposition under the ADR-004 ladder

ADR-004 permits a source patch only after the external-first ladder is exhausted, and only
under an Instrumentation Change Record. We are **not** patching, because rung 2 already
delivers what Phase 1 needs:

| Rung | Applied? |
|---|---|
| 1 — native NF metrics | ✅ enabled (`metrics.enable: true`, port 9091) |
| 2 — structured logs → log-derived metrics | ✅ **interim handling**: `observability/slice-exporter` parses the text logs and derives slice-labeled metrics |
| 3 — infra metrics | not needed yet |
| 4 — SBI capture (proxy/eBPF) | not attempted; the faithful route if text parsing proves brittle |
| 5 — gated source patch (ICR) | **not done**, and not justified at Phase 1 |

The text logs are *parseable and stable enough* for the signals we need — in particular the
AMF emits slice identity in a fixed, greppable form at INFO level:

```
[AMF][Gmm][supi:SUPI:imsi-208930000000004] Select SMF [snssai: {Sst:1 Sd:010203}, dnn: internet]
```

## Consequences to carry forward

- **The Phase 1 acceptance item "all NFs emit structured JSON logs" is NOT met**, and cannot
  be met by configuration on the pinned version. It is met by ADR-005's documented-gap
  escape clause instead.
- Regex parsing of human-readable logs is **brittle across upstream versions** — a reworded
  log line silently breaks the slice exporter. Any free5GC version bump must re-verify the
  patterns in `observability/slice-exporter/slice_exporter.py`.
- If Phase 2+ needs richer or more reliable structure, the decision is between **rung 4**
  (SBI capture, no upstream change) and **rung 5** (a patch to `free5gc/util/logger` under an
  ICR). That choice should be made deliberately, not drifted into.
- Log level stays at `info` per ADR-005. Debug was trialled and found unnecessary: the slice
  identity we need is already present at INFO.
