The `vendor/` directory contains Three.js 0.180.0 (MIT), its GLTFLoader, and
BufferGeometryUtils. The license is at `vendor/LICENSE`. The npm tarball integrity,
local SHA-256 values, and the one modification (relative local addon imports)
are recorded in `vendor/manifest.json`.

Reproduce with `python -m presentation.vendor_three` from the repository root.
No dependency is fetched at application runtime.
