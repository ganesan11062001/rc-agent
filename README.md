# TicketMate

Drafts replies to Research Computing ServiceNow tickets with a model served
on the cluster, grounded in the RC website and rc-docs.

```
/projects/rc/projects/ticketmate/     this repository
├── ticketmate/    the app: FastAPI backend, Chrome extension, OOD app, Slurm scripts
├── rc-kg/         the knowledge graph: Neo4j snapshot builds and the retrieval API
├── rc-copilot/    published OOD releases (not in git; written by publish.sh)
├── .uv/           shared Python toolchain (not in git)
└── .cache/        model weights and compile caches (not in git)
```

The two services talk only over rc-kg's versioned HTTP API
([rc-kg/API_CONTRACT.md](rc-kg/API_CONTRACT.md)); a change to it bumps the
version and runs both contract tests:

```bash
rc-kg/scripts/contract_test.sh             # provider side
ticketmate/tests/rc_kg/run_contract.sh     # consumer side
```

No script hard-codes this location: each finds the repository from its own
path, and the OOD app is told it when published. To move the checkout, move
the directory and republish (`ticketmate/ood-apps/publish.sh`).

- App: [ticketmate/README.md](ticketmate/README.md)
- Knowledge graph: [rc-kg/README.md](rc-kg/README.md)
- For RC members: [ticketmate/ood-apps/MEMBERS.md](ticketmate/ood-apps/MEMBERS.md)
