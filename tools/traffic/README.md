# tools/traffic — slice-aware traffic simulator (ported)

Flask server (`server.py`) + UE clients (`ue_client.py`) + `Dockerfile`, ported from the prior
project. Classifies traffic into `video` / `file` / `web` slices and exposes a
`traffic_requests_total` Prometheus counter labeled by slice type. This is **Phase-2+ load-generation
tooling**, not part of the Phase-1 core bring-up. It is kept here so slice observability work has a
ready traffic source.
