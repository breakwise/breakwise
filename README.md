# bumpfix

**An open registry of breaking changes in libraries and APIs, plus an agent that fixes your code when you upgrade.**

> **Status: early.** This repo holds the notice format (v0.1) and the first notices. The tool that finds and fixes affected code is next. Contributions are welcome.

## The problem

You upgrade a dependency and your code breaks: a class was renamed, a parameter was removed, a default changed. Bots open the upgrade pull request for you, but the code changes are still yours to figure out, and the information you need is scattered across changelogs and migration guides.

## The idea

When a car has a defect, the manufacturer issues a recall notice: which models are affected, what is wrong, and how a garage fixes it. bumpfix does the same for code.

Each breaking change becomes a small, machine-readable **notice** that says:

- **what changed:** the package, the version and the kind of change
- **how to fix it:** automatically when possible, always with a before/after example
- **where it comes from:** a link to the official changelog or migration guide

Notices live in this repo under [`notices/`](notices/), in the open, so developers, maintainers and coding agents can all use and improve them.

On top of the registry, the bumpfix tool will:

1. read the notices that apply to your dependencies,
2. find the affected code in your repository,
3. apply the fix and run your tests,
4. open a pull request for you to review. It never merges on its own.

Coding agents such as Claude Code and Cursor will be able to query the registry through an MCP server.

## What a notice looks like

```yaml
schema_version: "0.1"
id: BFX-2026-0001
status: verified
published: "2026-10-21"
package:
  ecosystem: PyPI
  name: pydantic-ai
  aliases: [pydantic-ai-slim]
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
```

Every field is described in [docs/notice-format.md](docs/notice-format.md).

## Roadmap

- [x] Notice format v0.1
- [ ] First notices for pydantic-ai, LangChain and the OpenAI Python SDK
- [ ] Validator that checks every notice in CI
- [ ] CLI: scan a repository for affected code
- [ ] CLI: apply fixes, run tests, open a pull request
- [ ] MCP server for coding agents

## Contributing

Did an upgrade break your code? Open an issue with the package name, the version and a link to the changelog, or add a notice yourself in a pull request.

## License

- Code: [Apache-2.0](LICENSE)
- Notices in [`notices/`](notices/): [CC BY 4.0](notices/LICENSE.md)
