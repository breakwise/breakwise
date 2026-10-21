# Notice format (v0.1)

A **notice** describes one breaking change in one package. It is a YAML file stored at:

```
notices/<ecosystem>/<package>/<id>.yaml
```

Example: [`notices/pypi/pydantic-ai/BFX-2026-0001.yaml`](../notices/pypi/pydantic-ai/BFX-2026-0001.yaml)

The format is young and may change before v1.0. Changes will be listed in this file.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `schema_version` | yes | Version of this format. Currently `"0.1"`. |
| `id` | yes | Unique ID: `BFX-<year>-<number>`, e.g. `BFX-2026-0001`. Never reused. |
| `status` | yes | `draft` (not reviewed yet) or `verified` (checked by a maintainer against the official source). |
| `published` | yes | Date the notice was added, as `"YYYY-MM-DD"`. |
| `package.ecosystem` | yes | Where the package is published. Currently `PyPI`. |
| `package.name` | yes | Package name as published, in lowercase, e.g. `pydantic-ai`. |
| `package.aliases` | no | Other package names that ship the same code with the same versions, e.g. `pydantic-ai-slim`. |
| `type` | yes | `rename`, `removal`, `signature` (parameters changed) or `behavior` (same code, different result). |
| `breaks_in` | yes | First version in which the old code stops working. |
| `works_since` | no | First version in which the new code works. If set, the fix can be applied before upgrading. |
| `summary` | yes | One sentence describing the change. |
| `rename.from`, `rename.to` | when `type` is `rename` | Full old and new names, including the module. Tools rewrite imports and usages automatically. |
| `instructions` | when there is no automatic fix | Plain steps to migrate, for people and coding agents. |
| `example.before`, `example.after` | yes | The smallest code that shows the change. |
| `references` | yes | Links to the official source: changelog, migration guide or pull request. |

## Rules

1. **One change per notice.** Two renames in the same release are two notices.
2. **Official sources only.** Every notice links to the changelog, docs or code of the package itself.
3. **Automatic fixes first.** Use `rename` whenever the change is a pure rename. Use `instructions` only when a person has to decide.
4. **Quote versions and dates**, e.g. `"2.0.0"`, so YAML keeps them as text.
5. **New notices start as `draft`.** A maintainer checks them against the source and sets `verified`.
6. **IDs:** take the next free number. A maintainer fixes any clash when merging.
