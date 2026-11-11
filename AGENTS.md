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
- For robot planning, implementation, deployment, troubleshooting and control, also apply `fabrication/assembly/debug_console/AGENT_WORKFLOW_ZH.md`. Start from the user's end-to-end acceptance goal, current authorization and latest handoff; choose an action that resolves the next uncertainty or advances the task. Search pending lessons by the relevant failure, joint or sensor instead of dumping the whole history. Preserve existing physical holds and stop latches across conversations, restarts and trial batches.
- Apply the user's 2026-11-06 task-related mobility and autonomous recovery authorization in `AGENT_CONTROL_ZH.md` section 12. Resolve scene layout and necessary repositioning autonomously when observed safe; preserve hardware/stop guards and distinguish a scene replan from a physical fault instead of imposing a manual reset prerequisite.
- Append every new lesson learned during robot work to `AGENT_LEARNING_ZH.md` as a dated pending entry with evidence and a unique date-based ID. The user reviews/merges these entries, or may explicitly instruct the assistant to consolidate them. The user's 2026-11-11 cross-conversation review request authorizes the procedural consolidation in `AGENT_CONTROL_ZH.md` section 13 and `AGENT_WORKFLOW_ZH.md`; it does not validate every historical experimental parameter or authorize future automatic promotion of candidates.
- A request to pause or sleep takes priority over exploration, extra tests, deployment and optional archiving. Stop first, save the minimum durable handoff, then perform the requested power action promptly; continue workstation-only documentation/verification afterward. A later documentation task does not authorize waking the laptop or resuming motion. Synchronize changed operating rules to the vehicle on the next authorized maintenance/resume before assistant-driven control, using the existing version-difference checks.

## Current project snapshot

- Repository: XLeRobot
- Local path: F:\workspace\github\XLeRobot
- Maintained repository: https://github.com/angelash/XLeRobot.git
- Upstream reference: https://github.com/Vector-Wangel/XLeRobot.git
- Preferred first build: XLeRobot 0.4.0 dual-wheel servo-motor version.
- Simulation virtual environment: .venv-sim
- Simulation scripts: simulation/mujoco
