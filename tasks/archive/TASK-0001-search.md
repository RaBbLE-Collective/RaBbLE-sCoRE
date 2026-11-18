# Task 0001
agent: search
priority: low
created: 2026-04-29T00:00:00Z
context_refs: []

## Objective

Verify that inotify-tools is available on Fedora 43 and document the exact package
name and install command for the project prerequisites section.

## Input

Target OS: Fedora 43
Package needed: inotify-tools (provides inotifywait)

## Output format

```markdown
## inotify-tools on Fedora 43
- Package name: {exact dnf package name}
- Install: `{exact install command}`
- inotifywait binary path after install: {path}
- Version available: {version if findable}
```

## Success criteria

- Package name confirmed against current Fedora repos
- Install command is copy-pasteable and correct
- If inotify-tools is unavailable on Fedora 43, surface the alternative
