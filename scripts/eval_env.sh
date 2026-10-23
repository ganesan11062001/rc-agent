#!/usr/bin/env bash
# Create .venv-eval for the LLM-judged (RAGAS) evaluation. Kept apart from
# .venv: ragas pulls in langchain and pins old versions of it, none of which
# serving needs. Same base interpreter as .venv.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(readlink -f "$ROOT/.venv/bin/python")"
[ -x "$ROOT/.venv-eval/bin/python" ] || "$PY" -m venv "$ROOT/.venv-eval"
"$ROOT/.venv-eval/bin/pip" install -q --upgrade pip
"$ROOT/.venv-eval/bin/pip" install -q -r "$ROOT/requirements-eval.txt"
"$ROOT/.venv-eval/bin/python" -c "import ragas; print('ragas', ragas.__version__, 'ready in', '$ROOT/.venv-eval')"
