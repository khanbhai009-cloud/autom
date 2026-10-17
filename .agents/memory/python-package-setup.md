---
name: Python package setup side effects
description: Package installation can alter imported-project dependency and Replit configuration files.
---

After installing Python dependencies in this imported project, the package helper appended duplicates of already-listed requirements and added an unnecessary `nodejs-24` module to `.replit`. The dependency list was cleaned to retain only the two required additions, and the original `.replit` workflow configuration was restored through the validated replacement flow.

**Why:** Unrelated package-manager changes can be accidentally shipped with feature work and can alter runtime configuration.

**How to apply:** After Python installs, inspect `requirements.txt` and `.replit`; keep only intentional dependency/configuration changes and use the validated config-replacement flow for `.replit`.
