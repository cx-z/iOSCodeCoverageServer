# iOS Coverage Platform server agent routing

- Start every task by reading `knowledge/README.md`.
- Follow its routing table and read only the relevant module and workflow documents.
- Current code is the final source of truth. If knowledge and code disagree, verify the code and update the knowledge base.
- Changes to module ownership, paths, invariants, or call chains must update the corresponding knowledge documents.
- Do not add one-off test results, Cycle IDs, Build IDs, IP addresses, session notes, or development-stage records to `knowledge/`.
- Before preparing a public change, run `python3 scripts/check_public_repository.py`; never add runtime configuration, credentials, profiles, databases, logs, or analysis output to Git.
