# Notice format (v0.1)

A **notice** describes one breaking change in one package.
Each notice is a YAML file stored at:

```text
notices/<ecosystem>/<package>/<id>.yaml
```

- `<ecosystem>` and `<package>` are lowercase, e.g. `notices/pypi/pydantic-ai/`.
- The file name is the notice ID, e.g. `BFX-2026-0001.yaml`.

Example: [`notices/pypi/pydantic-ai/BFX-2026-0001.yaml`](../notices/pypi/pydantic-ai/BFX-2026-0001.yaml)

The machine-readable definition is [`schema/notice-0.1.schema.json`](../schema/notice-0.1.schema.json)
(JSON Schema, draft 2020-12). If this page and the schema disagree, the schema wins.

The format may change before v1.0. Every change is listed in the [changelog](#changelog) below.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `schema_version` | yes | Version of this format: `"0.1"`. |
| `id` | yes | Unique ID `BFX-<year>-<number>` with at least four digits, e.g. `BFX-2026-0001`. Never reused. |
| `status` | yes | `draft` (not reviewed yet) or `verified` (checked by a maintainer against the official sources). |
| `published` | yes | Date the notice was added, as `"YYYY-MM-DD"`. |
| `modified` | no | Date of the last change to the notice, as `"YYYY-MM-DD"`. |
| `package.ecosystem` | yes | Where the package is published. Currently only `PyPI`. |
| `package.name` | yes | Normalized package name: lowercase, words joined by `-`, e.g. `pydantic-ai`. |
| `package.aliases` | no | Other packages that ship the same code with the same version numbers, e.g. `pydantic-ai-slim`. |
| `type` | yes | `rename`, `removal`, `signature` (parameters changed) or `behavior` (same code, different result). |
| `breaks_in` | yes | First version in which the old code stops working. |
| `works_since` | no | First version in which the new code works. When set, the fix can be applied before upgrading. |
| `summary` | yes | One sentence, at most 200 characters. |
| `rename.from`, `rename.to` | when `type` is `rename` | Old and new names as users import them, e.g. `pydantic_ai.models.openai.OpenAIModel`. Tools rewrite imports and usages automatically. |
| `instructions` | for every other `type` | Plain steps to migrate, for people and coding agents. |
| `example.before`, `example.after` | yes | The smallest code that shows the change. |
| `references` | yes | `https` links to official sources: changelog, migration guide, release notes or source code. |

Versions follow the package's own scheme ([PEP 440](https://peps.python.org/pep-0440/) for PyPI)
and are always quoted, e.g. `"2.0.0"`, so YAML keeps them as text. The same applies to dates.

## Rules

1. **One change per notice.** Two renames in the same release are two notices.
2. **Official sources only.** Every notice links to the changelog, docs, release notes or source code
   of the package itself.
3. **Automatic fixes first.** Use `rename` whenever the change is a pure rename.
   Use `instructions` only when a person has to decide.
4. **Tested examples.** `example.before` must work on a version before `breaks_in`,
   and `example.after` must work on `breaks_in` and later.
5. **New notices start as `draft`.** A maintainer checks them against the sources and sets `verified`.
6. **IDs:** take the next free number. A maintainer fixes any clash when merging.

## Changelog

- **0.1** (2026-10-21): first version.
