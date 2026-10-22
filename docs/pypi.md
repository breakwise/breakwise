# Breakwise

An open registry of breaking changes in Python libraries, with tested fixes.

Each breaking change is a small notice: the package, the release that breaks your code, and how to fix it.
Every notice has a before-and-after example that runs on real releases from PyPI before it is accepted.

## Install

```sh
pip install breakwise
```

## Show a notice

```sh
breakwise explain BW-2026-0001
```

This release comes with a copy of the notices, so it works offline.
A command that finds the code an upgrade breaks comes next.

## Links

- [Repository](https://github.com/breakwise/breakwise)
- [Notice format](https://github.com/breakwise/breakwise/blob/main/docs/notice-format.md)
- [Architecture and roadmap](https://github.com/breakwise/breakwise/blob/main/docs/architecture.md)

## License

The code is licensed under Apache-2.0.
The notices are licensed under CC BY 4.0: credit them as "Data from the Breakwise registry, CC BY 4.0".
