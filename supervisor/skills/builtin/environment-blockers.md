---
description: Telling code bugs from environment blockers (deps, network, platform) and unblocking them.
---
An agent can stall on environment problems that no code change can fix;
your job is to classify the stall and route around it.

1. Diagnose the category first: missing dependency, network or auth failure,
   platform mismatch (paths, shell, line endings), or a genuine code bug.
2. Missing dependencies are usually fixable: direct the agent to install
   them and continue, unless RESTRICTIONS forbid it.
3. For network or credential failures, allow one bounded retry with the
   error surfaced; endless retries are a failure pattern — require a
   workaround or a stubbed fallback instead.
4. Require evidence of the environment state when blocked: command output,
   versions, logs (`NEED_EVIDENCE: read_file <path>`).
5. A blocker is not a code failure: do not let the agent edit surrounding
   code to compensate; keep changes minimal and TARGET-focused.
