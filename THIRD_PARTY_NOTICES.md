# Third-party notices

Original integration scripts and glue in this repository are licensed under AGPL-3.0-or-later. Copyright (c) 2026 f246813.

Upstream projects are downloaded by the installer or provided by the user. Their original notices and licenses remain applicable:

- **agent-search**, https://github.com/sebrinass/agent-search, MIT, Copyright (c) 2024 IS. License: [licenses/agent-search-MIT.txt](licenses/agent-search-MIT.txt). The patch includes context from this project; retain this notice when distributing the patch or patched source.
- **SearXNG**, https://github.com/searxng/searxng, AGPL-3.0-or-later. License: [licenses/searxng-AGPL-3.0.txt](licenses/searxng-AGPL-3.0.txt). The installer downloads the pinned source and applies the included Windows patch.
- **Open WebUI**, https://github.com/open-webui/open-webui, Open WebUI License, Copyright (c) 2023- Open WebUI Inc. License: [licenses/open-webui.txt](licenses/open-webui.txt). The installer uses the official PyPI package; UI and branding remain unchanged.
- **llama.cpp**, https://github.com/ggml-org/llama.cpp, MIT. Not redistributed by this repository.
- **uv**, https://github.com/astral-sh/uv, MIT or Apache-2.0. Downloaded from its official release; not bundled in the published archive.
- **Qwen3-4B** and **bge-reranker-v2-m3** model files are not redistributed. Obtain models separately and consult their model cards and licenses.

Installed Python and Node dependencies retain their licenses. Versions are recorded in config/*-requirements.lock.txt, config/versions.json, and the pinned upstream agent-search package-lock.json.
