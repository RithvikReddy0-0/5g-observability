# ran/config — UERANSIM gNB / UE configuration (ported)

`free5gc-gnb.yaml` and `free5gc-ue.yaml` ported from the prior project. Aligned with the core:
**PLMN 208/93, TAC 1, slice S-NSSAI (SST 1 / SD 010203)**, DNN `internet`, subscriber SUPI
`imsi-208930000000001`. Consistency across AMF/NSSF/SMF/gNB/UE is audited in
[`../../docs/runbooks/m0-report.md`](../../docs/runbooks/m0-report.md) (§5). Used at M3 (RAN +
Registration) and M4 (PDU session), not in M0.
