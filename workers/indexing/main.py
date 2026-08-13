"""Entry point for the indexing worker.

A worker directory is a *deployment unit*, not a second copy of the business
logic: this module imports the shared application package and runs the queue loop
for its stage class. Workers never import each other.

Stage: indexing (compute-bound). Concurrency is capped per stage in the database, so
running two of these does not double the rate at which one stage hits a provider.
"""

from __future__ import annotations

import sys

from lab_assistant.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main(["worker", "compute", *sys.argv[1:]]))
