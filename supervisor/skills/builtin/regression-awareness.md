---
description: When the target modifies existing code — requiring evidence that existing behavior survives.
---
Modifying existing code adds an invisible second target: do not break what
already worked.

1. When criteria touch existing modules, verify public entry points survive:
   imports, function names, and commands referenced elsewhere.
2. Before accepting a refactor as MET, read the changed file
   (`NEED_EVIDENCE: read_file <path>`) and check callers and exports were
   updated, not just the definition.
3. Prefer judging with the suite (`NEED_EVIDENCE: test_report`) when the
   workspace has one — a green run is the cheapest regression evidence.
4. Deletions are suspect: require the evidence to show why each removed
   function or file is safe to remove.
5. Only the TARGET may change behavior; flag accidental behavior changes
   even when all criteria look MET.
