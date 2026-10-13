# Session cost

Appended at the end of every session, by the agent, before it stops. One row
per session. The purpose is to notice if the task workspace is costing more
context than it saves - if new input tokens climb while the work per session
does not, say so rather than just recording it.

Cached input is not free but is roughly an order of magnitude cheaper than
new input, so the column to watch is **new input**.

| Project / Task | Agent Messages | New Input Tokens | Cached Input Tokens | Output Tokens |
|---|---|---|---|---|
| agent-conductor / bootstrap | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (overnight + resource cap, registry, :8773 dashboard) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (auth diagnosis, abort-path fix) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (smoke readme step; found -p detaches) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (verified README task already landed; no-op) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (README task again; re-verified vs argparse, no-op) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (README task 3rd dispatch; still a no-op - trap-5 respawn) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (README task 4th dispatch; still a no-op - trap-5 respawn) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (README task 5th dispatch; still a no-op - trap-5 respawn) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |

If the figures are not available at the end of a session, write the row with
the counts left as `n/a` rather than skipping it, so the session is still
accounted for.
