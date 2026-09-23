---
description: Writing feedback the agent can act on — it is the supervisor's only channel to the agent.
---
Your feedback text is sent to the agent almost verbatim; it is the only thing
steering the next turn.

1. Drive one primary objective per turn: the single most important UNMET
   criterion. Everything else is secondary.
2. Be concrete: name exact files, functions, commands, and the expected
   observable result ("create utils/cli.py exposing run()"), not themes.
3. State what evidence you will accept next turn, so the agent can produce it
   ("show the command output for X", "I will read the new test file").
4. Do not re-paste the protocol or repeat feedback already applied; reference
   it briefly ("TARGET 3 still missing").
5. Say "keep X" explicitly for working parts — feedback that only lists
   faults invites rewrites of code that already works.
