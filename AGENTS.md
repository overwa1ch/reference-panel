# Repository instructions

Read README.md for product scope and CONTRIBUTING.md for development paths.

<!-- project-worktree-boundary: v1 -->
## Worktree Boundary

- The coordinator owns linked-worktree creation, assignment and removal.
- Parallel workers only write inside the working tree returned by `git rev-parse --show-toplevel`.
- Before handing back changes, run `python3 -m unittest discover -s verification -p 'test_*.py' -v`, `node verification/test_probe_ui.cjs`, and `git diff --check`.

Use temporary fixtures, preserve original reference files, and keep actual native UI acceptance distinct from synthetic tests. Do not add user media, diagnostic records or credentials to the repository.
