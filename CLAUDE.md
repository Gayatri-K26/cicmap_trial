# cicmap

Project context lives in a shared Claude Doc: https://claude.ai/artifact/HCi87euGBwETtaS1PvUcyy
(doc id `8334be26-71c9-431f-800a-78cee42a709c`).

At the start of a session, read that doc with the Claude Docs tools and use it as context.
Record new decisions in its "Decisions log" table.

- DeepSeek API key: `DEEPSEEK_API_KEY` in `.env` (gitignored). Never commit it or write it into the doc.
- Target programs come from Hasnain's public Google Sheet via `python -m cicmap sync` (no local copy).
- Run everything with `.venv/bin/python -m cicmap ...`; tests with `.venv/bin/pytest`.
- Python is 3.9: use `Optional[...]`, not `X | None`, in pydantic models.
- New catalog platforms go in `cicmap/adapters/` as `@register`ed `CatalogAdapter` subclasses, each with a
  saved sample page in `tests/fixtures/` and a test.
