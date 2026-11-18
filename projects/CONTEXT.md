# projects/CONTEXT.md

```
workspace: projects | epoch: 0 | episode: 1
```

## What happens here

Git submodule mount points. Each subdirectory is a registered Collective member
checked out as a submodule. sCoRE can delegate execution tasks that operate
within these repos.

## Current contents

Empty. No submodules registered yet.

## Adding a submodule

```bash
git submodule add git@github.com:markm1206/{RepoName} projects/{RepoName}
git commit -m "ingest ~ projects >> add {RepoName} submodule"
```

## Updating a submodule

```bash
git submodule update --remote projects/{RepoName}
git commit -m "harmonize ~ projects >> bump {RepoName} to latest"
```

## When to add a project here

When sCoRE needs to delegate tasks that read or write into another Collective member's
codebase. Not required for coordination tasks that only touch tasks/, memory/, etc.

## Expected future submodules

- `RaBbLE-Collective` — for registry reads and manifest updates
- `RaBbLE-OS` — if sCoRE needs to delegate OS config tasks
