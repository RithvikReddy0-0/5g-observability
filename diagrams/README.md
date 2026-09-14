# diagrams — versioned diagram source (ADR-008)

Diagrams live here as **text source** (Mermaid / PlantUML), never opaque images, so they are
diffable and reviewable.

| Diagram | Stack | Basis |
|---|---|---|
| `ue-registration.mmd` | free5GC | observed AMF/UERANSIM logs |
| `pdu-session-establishment.mmd` | free5GC | control plane observed; data path **DESIGN ONLY** (no UPF without gtp5g) |
| `open5gs-pdu-session-establishment.mmd` | Open5GS | **every message observed on the wire**, including the user data — `docs/evidence/open5gs-pdu-session-capture/` (one call source-inferred, marked) |
