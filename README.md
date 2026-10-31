<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/breakwise-logo-on-dark.svg">
    <img alt="Breakwise" src=".github/assets/breakwise-logo-on-light.svg" height="44">
  </picture>
</h1>

**An open registry of breaking changes in libraries and APIs, plus an agent that fixes your code when you upgrade.**

> **Status: early.** This repository holds the notice format (v0.3), the first notices and the `breakwise` package.
> The package on PyPI shows any notice offline; finding and fixing affected code comes next. Contributions are welcome.

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
4. propose the change for you to review, as a diff, a comment or a pull request. It never merges on its own.

AI coding agents will be able to query the registry through an MCP server.

## What a notice looks like

```yaml
---
schema_version: "0.3"
id: BW-2026-0001
status: verified
published: "2026-10-21"
modified: "2026-10-24"
package:
  ecosystem: PyPI
  name: pydantic-ai
  aliases:
    - pydantic-ai-slim
type: rename
breaks_in: "2.0.0b1"
works_since: "0.7.6"
summary: OpenAIModel was renamed to OpenAIChatModel, and the old name was removed in 2.0.0b1.
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
  - https://pydantic.dev/docs/ai/project/changelog/
  - https://github.com/pydantic/pydantic-ai/blob/v0.7.6/pydantic_ai_slim/pydantic_ai/models/openai.py
```

Every field is described in [docs/notice-format.md](docs/notice-format.md),
and [schema/notice-0.3.schema.json](schema/notice-0.3.schema.json) defines the format for tools.

## Install

```sh
pip install breakwise
breakwise explain BW-2026-0001
```

The package comes with a copy of the notices, so `breakwise explain` works offline.
It exits with 0 when it shows the notice, 1 for an ID that is wrong or not there, 2 for wrong usage, and 141 when the output is closed before it ends.
Finding the code that an upgrade breaks comes next; see the [roadmap](#roadmap).

## Repository layout

| Path | Contents |
| --- | --- |
| [`notices/`](notices/) | The registry: one YAML file per breaking change |
| [`docs/`](docs/) | Documentation: the [notice format](docs/notice-format.md) and the [architecture](docs/architecture.md) |
| [`schema/`](schema/) | JSON Schema for notices |
| [`src/breakwise/`](src/breakwise/) | The `breakwise` package and its command line |
| [`tools/`](tools/) | Scripts that check notices and run their examples on real package versions |
| [`tests/`](tests/) | Tests for the package and the scripts |
| [`.github/workflows/`](.github/workflows/) | The checks that run on every pull request, and the release to PyPI |

## Roadmap

- [x] Notice format v0.1 and its JSON Schema
- [x] Check every notice and run its examples on real package versions, on each pull request
- [x] A first release on PyPI: `breakwise explain` shows any notice, offline
- [ ] The notice formats that the pydantic-ai V2 notices need, those notices and a published index
- [ ] `breakwise scan`: report the code that an upgrade breaks
- [ ] MCP server for AI coding agents
- [ ] `breakwise fix`: apply tested fixes, alone or in a two-step upgrade
- [ ] GitHub Action that comments tested fixes on Dependabot and Renovate pull requests
- [ ] Draft generator for new notices, and an agent for changes that need a person

Notices for LangChain and the OpenAI Python SDK start once the pydantic-ai V2 notices are in, and continue alongside the others.
The phases, and why Breakwise is built this way, are in [docs/architecture.md](docs/architecture.md).

## Contributing

Did an upgrade break your code? [Request a notice](https://github.com/breakwise/breakwise/issues/new?template=request-notice.yml),
or add one yourself in a pull request: [CONTRIBUTING.md](.github/CONTRIBUTING.md) explains how.
New notices start as `draft` until a maintainer verifies them.
Everyone who takes part follows the [code of conduct](.github/CODE_OF_CONDUCT.md),
and security problems are reported privately, as the [security policy](.github/SECURITY.md) explains.

## Development

Run the main checks locally on Linux or macOS. The tools work on Python 3.12, 3.13 and 3.14, and CI tests all three:

```sh
python -m pip install --require-hashes -r requirements-dev.txt
ruff check . && ruff format --check .
mypy
python -m pytest --cov
python tools/validate_notices.py
python tools/verify_examples.py --all
```

The tests must cover every line and branch of `src/` and `tools/`.
`tools/verify_examples.py` installs real package versions from PyPI, so it needs network access.
Run it with Python 3.12, or pass `--python` a Python 3.12 interpreter, because old package versions may not install on newer Python versions.
The pull request checks also lint YAML, Markdown and the workflows;
see [`.github/workflows/checks.yml`](.github/workflows/checks.yml).
markdownlint-cli2 needs Node.js 22.12 or later. It and its dependencies are pinned in `package-lock.json`; install them with `npm ci --ignore-scripts`.
To update it, change its version in `package.json`, then run `npm install --package-lock-only --ignore-scripts`.
`overrides` in `package.json` forces katex 0.19.0, which fixes [GHSA-238p-pmpm-9mq7](https://github.com/advisories/GHSA-238p-pmpm-9mq7), and re2js 2.8.8, which fixes [GHSA-gg6h-894w-4m9c](https://github.com/advisories/GHSA-gg6h-894w-4m9c); remove each one once markdownlint depends on a fixed release.

## License

- Code, schema and docs: [Apache-2.0](LICENSE)
- Notices in [`notices/`](notices/): [CC BY 4.0](notices/LICENSE.md)
- The [code of conduct](.github/CODE_OF_CONDUCT.md), adapted from the Contributor Covenant 3.0: [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
