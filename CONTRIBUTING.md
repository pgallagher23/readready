# Contributing

Thank you for helping improve ReadReady. This page covers the development setup
and what a pull request needs.

## Setup

```bash
git clone <repo-url> readready && cd readready
make venv        # .venv with CPU PyTorch and requirements.txt
make jar         # builds output_gen/itext_tagger.jar (needs JDK 17 and Maven)
make test        # runs the test suite
```

Python 3.10 or newer is required. The system packages are listed in
[README.md](README.md#requirements). Tests that exercise the Java tagger are
skipped when `output_gen/itext_tagger.jar` is missing, so run `make jar` first.

## Before every pull request

1. Run `make test` and confirm it passes. Add a failing test first for a bug fix
   or a new behavior.
2. If you change `config.yaml`, document the setting in a comment beside it,
   and update [README.md](README.md) if it affects installing or running.
3. If you change the Java tagger, rebuild with `make jar` and run the tests
   again.

Pull requests must keep CI green. CI runs the Python tests (on Python 3.10
and 3.12) against a freshly built tagger JAR, a Python 3.10 compatibility check
(`compat`, also `make compat`), a Docker image build, and the Maven build.

## Guidelines

- Keep a change focused; separate unrelated fixes into separate pull requests.
- Do not loosen verdict thresholds or validation gates to make a document pass.
  Fix the tagger or the remediation step instead.
- Do not commit fonts, sample documents, credentials, or `.env` files. The only
  PDF in the tree is `examples/sample.pdf`.
- Write documentation in plain language and keep headings descriptive.

## License of contributions

By submitting a contribution you agree that it is licensed under
AGPL-3.0-or-later, the same license as the project. See [LICENSE](LICENSE).
