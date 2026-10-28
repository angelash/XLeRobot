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

## Current project snapshot

- Repository: XLeRobot
- Local path: F:\workspace\github\XLeRobot
- Maintained repository: https://github.com/angelash/XLeRobot.git
- Upstream reference: https://github.com/Vector-Wangel/XLeRobot.git
- Preferred first build: XLeRobot 0.4.0 dual-wheel servo-motor version.
- Simulation virtual environment: .venv-sim
- Simulation scripts: simulation/mujoco
