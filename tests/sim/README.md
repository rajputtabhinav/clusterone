# Fake Redfish / BMC simulator

Simulator-first development target (chosen for v1). This package will host a
lightweight HTTP server that emulates the DMTF Redfish surfaces ClusterOne
touches, so the **entire discovery + update pipeline runs end-to-end with zero
risk to real hardware**:

- `GET /redfish/v1/` — service root + vendor identity
- `GET /redfish/v1/Systems/<id>` — Model, SerialNumber, BiosVersion, PowerState
- `GET /redfish/v1/Managers/<id>` — BMC FirmwareVersion
- `POST /redfish/v1/UpdateService/...` — accept firmware, return a Task
- `GET <TaskMonitor>` — stream Flashing → Completed progress, then bump version

It will support scripted scenarios (slow flash, mid-flash failure, version
mismatch on verify, offline host) so the update state machine and UI can be
tested deterministically in CI.

Real lab BMCs wire in afterward (Phase 4 validation milestones).
