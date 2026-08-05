# Upstream bug report (draft — not yet filed)

**Repo:** `datahub-project/datahub`
**Title:** `datahub datapack --help` crashes: `datapack/resources/` missing from published wheel

---

### Description

`datahub datapack --help` raises `FileNotFoundError` on a clean install of
`acryl-datahub==1.7.0` from PyPI. The `datahub/cli/datapack/resources/`
directory ships with only `__init__.py`; both `DATAPACK_AGENT_CONTEXT.md` and
`registry.json` are absent, though both are present in the repository.

### Reproduction

```bash
python3 -m venv .venv && .venv/bin/pip install acryl-datahub==1.7.0
.venv/bin/datahub datapack --help
```

```
FileNotFoundError: [Errno 2] No such file or directory:
'.../site-packages/datahub/cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md'
```

Raised from `datahub/cli/datapack/datapack_cli.py:36` in `format_help`.

### Root cause

`metadata-ingestion/setup.py` declares package data for
`datahub.cli.resources` but has no entry for the newer
`datahub.cli.datapack.resources` package:

```python
package_data={
    ...
    "datahub.cli.gql": ["*.gql"],
    "datahub.cli.resources": ["*.md"],
    # no entry for datahub.cli.datapack.resources
},
```

Proof by contrast in an installed 1.7.0 wheel:

| Package | Declared in `package_data` | Files in wheel |
|---|---|---|
| `datahub/cli/resources/` | yes | `__init__.py` + all 4 `*_AGENT_CONTEXT.md` |
| `datahub/cli/datapack/resources/` | **no** | `__init__.py` only |

### Suggested fix

```diff
         "datahub.cli.gql": ["*.gql"],
         "datahub.cli.resources": ["*.md"],
+        "datahub.cli.datapack.resources": ["*.md", "*.json"],
     },
```

`*.json` is included because `registry.json` lives in the same directory and is
also absent from the wheel.

### Impact

`datahub datapack load <pack>` and `datahub datapack list` both work, so this
only breaks `--help`. But `--help` is the documented discovery path for the
command, and the traceback reads like a broken install.

### Environment

- `acryl-datahub` 1.7.0 (PyPI wheel), Python 3.11.0, macOS arm64
- DataHub GMS v1.7.0 via `datahub docker quickstart`
