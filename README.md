<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/logo/breakwise-logo-dark.svg">
    <img alt="Breakwise" src="brand/logo/breakwise-logo.svg" height="44">
  </picture>
</h1>

**Know what an upgrade breaks before it breaks your code.**
An open registry of breaking changes in libraries and APIs, plus an agent that fixes your code when you upgrade.

> **Status: early.** This repository holds the notice format (v0.1) and the first notice.
> The tool that finds and fixes affected code comes next. Contributions are welcome.

## The problem

You upgrade a dependency and your code breaks: a class was renamed, a parameter was removed, a default changed.
Bots open the upgrade pull request for you, but the code changes are still yours to figure out,
and the information you need is scattered across changelogs and migration guides.

## The idea

When a car has a defect, the manufacturer issues a recall notice: which models are affected, what is wrong,
and how a garage fixes it. Breakwise does the same for code.

Each breaking change becomes a small, machine-readable **notice** that says:

- **What changed:** the package, the version and the kind of change.
- **How to fix it:** automatically when possible, always with a tested before/after example.
- **Where it comes from:** links to the official changelog, release notes or source code.

Notices live in this repository under [`notices/`](notices/), in the open,
so developers, maintainers and AI coding agents can all use and improve them.

On top of the registry, the Breakwise tool will:

1. read the notices that apply to your dependencies,
2. find the affected code in your repository,
3. apply the fix and run your tests,
4. open a pull request for you to review. It never merges on its own.

AI coding agents will be able to query the registry through an MCP server.

## What a notice looks like

```yaml
---
schema_version: "0.1"
id: BW-2026-0001
status: verified
published: "2026-10-21"
package:
  ecosystem: PyPI
  name: pydantic-ai
  aliases:
    - pydantic-ai-slim
type: rename
breaks_in: "2.0.0"
works_since: "0.7.6"
summary: OpenAIModel was renamed to OpenAIChatModel, and the old name was removed in 2.0.0.
rename:
  from: pydantic_ai.models.openai.OpenAIModel
  to: pydantic_ai.models.openai.OpenAIChatModel
example:
  before: |
    from pydantic_ai.models.openai import OpenAIModel

    model = OpenAIModel("gpt-5")
  after: |
    from pydantic_ai.models.openai import OpenAIChatModel

    model = OpenAIChatModel("gpt-5")
references:
  - https://pydantic.dev/docs/ai/changelog/
  - https://github.com/pydantic/pydantic-ai/blob/v0.7.6/pydantic_ai_slim/pydantic_ai/models/openai.py
```

Every field is described in [docs/notice-format.md](docs/notice-format.md),
and [schema/notice-0.1.schema.json](schema/notice-0.1.schema.json) defines the format for tools.

## Repository layout

| Path | Contents |
|---|---|
| [`notices/`](notices/) | The registry: one YAML file per breaking change |
| [`docs/`](docs/) | Documentation, starting with the notice format |
| [`brand/`](brand/) | The logo, colors, type and voice of Breakwise |
| [`schema/`](schema/) | JSON Schema for notices |
| [`tools/`](tools/) | Scripts that check notices and run their examples on real package versions |
| [`tests/`](tests/) | Tests for those scripts |
| [`.github/workflows/`](.github/workflows/) | The checks that run on every pull request |

## Roadmap

- [x] Notice format v0.1 and its JSON Schema
- [x] Check every notice and run its examples on real package versions, on each pull request
- [ ] First notices for pydantic-ai, LangChain and the OpenAI Python SDK
- [ ] CLI: scan a repository for affected code
- [ ] CLI: apply fixes, run tests, open a pull request
- [ ] MCP server for AI coding agents

## Contributing

Did an upgrade break your code? Open an issue with the package name, the version and a link to the changelog,
or add a notice yourself in a pull request following [docs/notice-format.md](docs/notice-format.md).
New notices start as `draft` until a maintainer verifies them.

## Development

Run the main checks locally with Python 3.12 or later, on Linux or macOS:

```sh
python -m pip install --require-hashes -r requirements-dev.txt
ruff check . && ruff format --check .
mypy
python -m pytest --cov
python tools/validate_notices.py
python tools/verify_examples.py --all
```

The tests must cover every line and branch of `tools/`.
`tools/verify_examples.py` installs real package versions from PyPI, so it needs network access.
The pull request checks also lint YAML, Markdown and the workflows;
see [`.github/workflows/checks.yml`](.github/workflows/checks.yml).

## License

- Code and schema: [Apache-2.0](LICENSE)
- Notices in [`notices/`](notices/): [CC BY 4.0](notices/LICENSE.md)
