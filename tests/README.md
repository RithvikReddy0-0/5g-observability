# tests — Phase 1 acceptance criteria as executable checks

```bash
tests/acceptance.sh                 # expects 20 UEs
EXPECT_UES=19 tests/acceptance.sh   # override the expected UE count
```

Each criterion from SPEC §5 reports one of:

| Verdict | Meaning |
|---|---|
| `PASS` | criterion met and verified |
| `FAIL` | criterion is runnable here and did **not** pass — exit code 1 |
| `SKIP-ODE` | genuinely requires the Official Development Environment (gtp5g / user plane) |
| `GAP` | a documented, accepted gap (see `docs/logging.md`) |

**A green run on this laptop does not mean Phase 1 is complete.** The script says so
explicitly: when any `SKIP-ODE` or `GAP` remains it prints `PASS (non-baseline)` and names
how many criteria still require the ODE. Only a run with zero skips and zero gaps prints
`PASS — Phase 1 acceptance complete`.

Last run on this host: **PASS=12, FAIL=0, SKIP-ODE=7, GAP=1**.

Implementation notes:
- Uses plain `docker` against container names rather than `docker compose` — on Docker
  Desktop + WSL the compose plugin is a symlink into `/mnt/wsl/docker-desktop/` that
  disappears whenever Desktop restarts, while the daemon keeps working.
- The slice-consistency check compares the S-NSSAI the UE configs request against what
  AMF, SMF, NSSF and the gNB advertise. That mismatch is exactly what caused
  `AMF can not select an target AMF by NRF` and cost a full debugging cycle.
