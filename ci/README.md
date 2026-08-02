# ci — CI/CD (ported reference)

GitHub Actions (`github-actions/`), Jenkins (`jenkins/`), and helper `scripts/` ported from the
prior `5g-devops-framework` project as reference. The **Phase-1 CI contract** (SPEC / brief) is
narrower and wired in **M1**: run `scripts/bootstrap.sh --verify-only` as an integrity gate that
fails on upstream SHA drift, plus config/lint validation. Treat the ported pipelines as a starting
point, not the Phase-1 pipeline.
