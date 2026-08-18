# PaperLocale

Verified, layout-preserving academic PDF translation with pluggable model providers and domain-specific terminology packs.

[中文说明](README.zh-CN.md)

## Status

PaperLocale is under active development. The first release focuses on one strict pipeline:

1. collect translatable segments from a PDF layout engine;
2. translate them with one explicitly selected provider;
3. reject translations that lose formulas, style tags, numbers, units, abbreviations, URLs, DOIs, or required terminology;
4. rebuild the PDF without changing its page geometry;
5. render every page and produce a reviewable QA report.

The project does not promise bitwise-identical typography. Chinese text naturally changes line breaks. Its promise is narrower and testable: preserve the page structure and protected scientific content, and fail before rendering when that contract is broken.

## Implemented providers and gates

- `codex-local`: local-only translation through an authenticated Codex CLI session;
- `openai-compatible`: BYOK access to OpenAI and compatible endpoints;
- an extensible `atmospheric-science` pack with terminology and evaluation cases;
- a resumable `collect -> translate -> validate -> render -> qa -> accept` workflow;
- page geometry, image object, blank-page, placeholder, and all-page visual checks.

PaperLocale never reads or copies Codex authentication files. ChatGPT-managed Codex access is for trusted local use only and is not exposed as a public translation API.

## Domain packs

The first built-in pack is `atmospheric-science`. A pack contains a manifest, glossary, prompt rules, and evaluation cases. New disciplines can be added without changing the translation pipeline.

```bash
python -m paperlocale domain-check atmospheric-science
python -m paperlocale validate-segments \
  --segments segments.jsonl \
  --translations translations.jsonl \
  --domain atmospheric-science
```

## Install

Python 3.10–3.13 and Poppler's `pdftoppm` are required for the complete workflow.

```bash
git clone https://github.com/hazugi2004/paperlocale.git
cd paperlocale
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[layout]"
paperlocale domain-check atmospheric-science
```

## Quick start

```bash
paperlocale init-run paper.pdf --run-dir runs/paper
paperlocale collect --run-dir runs/paper

# Uses the authenticated Codex CLI session on this trusted local machine.
paperlocale translate --run-dir runs/paper \
  --provider codex-local --domain atmospheric-science

paperlocale validate --run-dir runs/paper --domain atmospheric-science
paperlocale render --run-dir runs/paper
paperlocale qa --run-dir runs/paper
# Inspect every image under runs/paper/qa/comparisons/ before acceptance.
paperlocale accept --run-dir runs/paper --reviewed-by "Your name"
```

For a BYOK OpenAI-compatible endpoint:

```bash
export PAPERLOCALE_API_KEY="your-key"
paperlocale translate --run-dir runs/paper \
  --provider openai-compatible \
  --base-url https://api.example.com/v1 \
  --model your-model \
  --domain atmospheric-science
```

See the detailed [Chinese guide](README.zh-CN.md), [ROADMAP](docs/ROADMAP.md), [ARCHITECTURE](docs/ARCHITECTURE.md), [domain-pack guide](docs/DOMAIN_PACKS.zh-CN.md), and [PROVENANCE](docs/PROVENANCE.md).

## Development

Unit tests do not call a model or require the layout engine:

```bash
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
```

After layout-engine upgrades, run the deterministic full-path smoke test:

```bash
python -m pip install -e ".[layout,test]"
python scripts/layout_smoke.py --output tmp/layout-smoke-001
```

The script intentionally stops before visual acceptance and prints the comparison image to inspect.

## License

GNU Affero General Public License v3.0 only. This choice is aligned with the AGPL-licensed PDF layout engines the project is designed to integrate.
