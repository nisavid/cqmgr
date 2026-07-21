# Operator workflow prototype

Throwaway, read-only prototype for Wayfinder issue #18. It compares three
workflow structures while keeping the same domain operations and safety facts.
It does not call Google Cloud or perform quota mutations.

Run it from the repository root:

```sh
python3 -m http.server 4173 --directory tools/cloud-quotas/prototypes/operator-workflows
```

Then open `http://localhost:4173/?variant=A`. Use the switcher at the bottom to
compare variants A, B, and C.

