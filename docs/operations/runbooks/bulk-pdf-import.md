Status: active
Owner: steward + operator
Last verified: 2026-11-20
Source of truth: `packages/python/lab_assistant/application/bulk_import.py`

# Runbook: importing a folder of PDFs

**Symptom.** Acquisitions are waiting for a person (`assisted` or `failed` on the
Collections page), and someone has the papers as PDFs — a library download, a
colleague's folder — rather than one at a time.

## Two ways in, one path

* **In the browser:** Collections → *Import many PDFs*. Drop the files, give the access
  basis and an attestation, then *Check matches* (stores nothing) and *Import*.
  A file that did not match can be assigned to a waiting acquisition right there.
* **On the server**, for a large folder:

  ```bash
  sudo docker compose -p lab-assistant -f docker-compose.yml -f infra/compose/docker-compose.prod.yml \
    run --rm -v /path/to/pdfs:/import:ro api \
    lab acquisition import-folder /import --workspace rlalab --actor <user-id> \
      --attestation "Downloaded through the Imperial library subscription." --dry-run
  ```

  Read the report, then run it again without `--dry-run`. Folders larger than 200
  files are sent in batches of 200; the exit status is non-zero if any file was
  rejected.

Both use the same service, and every import is an assisted upload in every respect:
rights decision, integrity and malware checks, identity check, then a parse task.

## How a file is matched

By DOI, PMID or PMCID only — never by title — taken from the file name
(`10.1234_abc.pdf`, `PMID12345678.pdf`, `PMC9876543.pdf`) or, if the name says nothing,
from the PDF's metadata and **first page**. Later pages are never read: a reference list
is full of other papers' DOIs.

| Outcome | Meaning | What to do |
|---|---|---|
| `imported` | matched and accepted; parsing is queued | nothing |
| `blocked` | accepted, but the malware scan did not pass | see [Malware detected](malware-detected.md) |
| `unmatched` | no waiting acquisition has its identifiers | rename the file to its DOI, or assign it by hand in the browser |
| `ambiguous` | it names several waiting works, or another file in the batch already took its acquisition | assign it by hand |
| `rejected` | a check refused it (rights, size, type, identity) | the detail says which; do not retry the same file blindly |

## Do not

**Do not import with a vague attestation.** It is recorded on every rights decision
this batch creates, and it is what someone will read when asking whether the lab was
entitled to the copy.

**Do not rename files to force a match.** A file named after the wrong DOI is attached
to the wrong work, and identity verification is the only thing between that and a
release.
