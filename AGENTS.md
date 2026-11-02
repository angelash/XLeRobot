# AGENTS.md

Scope: entire repository clone.

## Local project state

- Read PROJECT_STATE.md before hardware setup, procurement, compatibility testing, model training, or simulation work.
- Treat PROJECT_STATE.md as the canonical local build and inventory ledger.
- Whenever the user states that they already own, received, ordered, returned, or retired a hardware or software asset, append a dated entry to PROJECT_STATE.md.
- Never silently overwrite prior inventory or decisions. Add a new dated entry and mark superseded information when needed.
- Keep evidence labels explicit: user statement, published specification, local system verification, and physical test result.
- Record exact model numbers, interfaces, firmware, condition, and price only when known or observed. Mark unknown fields as pending.
- Preserve unrelated upstream files. Keep local inventory and build decisions additive and reviewable.
- Commit and push requested project changes to `origin` (`angelash/XLeRobot`) after focused validation; keep the original repository as `upstream`.
- Do not publish private-key material, credentials, generated virtual environments, server logs, runtime state, or temporary camera captures.
- Extend robot capabilities inside the existing XLeRobot Studio at port `8770` (`http://192.168.137.150:8770/` on the laptop). Add pages to its navigation and mount backend routes in the same application. Mapping, perception, planning, skills, robot presentation and future robot functions must use this shared platform; do not create a separate user-facing site, port or boot service unless the user explicitly requests one. Follow `.cursor/rules/robot-platform.mdc`.
- Before assistant-driven robot control, read `fabrication/assembly/debug_console/AGENT_CONTROL_ZH.md` (reviewed rules), `AGENT_OBSERVATION_ZH.md` (verified observation methods and limits) and `AGENT_LEARNING_ZH.md` (self-learned lessons pending review) in the same folder, and apply all three. The reviewed rules win on conflict; pending lessons may add caution or methods but never relax hard limits.
- Append every new lesson learned during robot work to `AGENT_LEARNING_ZH.md` as a dated pending entry with evidence. Only the user merges entries into `AGENT_CONTROL_ZH.md`; change the rules there only on explicit user instruction.

## Current project snapshot

- Repository: XLeRobot
- Local path: F:\workspace\github\XLeRobot
- Maintained repository: https://github.com/angelash/XLeRobot.git
- Upstream reference: https://github.com/Vector-Wangel/XLeRobot.git
- Preferred first build: XLeRobot 0.4.0 dual-wheel servo-motor version.
- Simulation virtual environment: .venv-sim
- Simulation scripts: simulation/mujoco
