# Security policy

This package processes untrusted graph and certificate files. The CLI reads at
most `--max-input-bytes` bytes per input (64 MiB by default), rejects malformed
values, and never executes input as code. In-memory Python APIs receive objects
or byte strings that are already resident in the caller's process; callers are
responsible for bounding those buffers before invoking the parser. External
graph-generator integrations use argument arrays without a shell.

Do not open a public issue for a suspected vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/chenle02/total-coloring-toolkit/security/advisories/new)
or email `chenle02@gmail.com` if that form is unavailable. Include a minimal
reproducer, affected version or commit, and impact. Supported security fixes
target the latest minor release.
