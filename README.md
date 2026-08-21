# PaperLocale

[![tests](https://github.com/hazugi2004/paperlocale/actions/workflows/tests.yml/badge.svg)](https://github.com/hazugi2004/paperlocale/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/hazugi2004/paperlocale)](https://github.com/hazugi2004/paperlocale/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/paperlocale)](https://pypi.org/project/paperlocale/)
[![license](https://img.shields.io/github/license/hazugi2004/paperlocale)](LICENSE)

Verified, layout-preserving academic PDF translation with pluggable model providers and domain-specific terminology packs.

[中文说明](README.zh-CN.md)

![PaperLocale verified workflow](docs/assets/workflow.svg)

The demo below is generated from PaperLocale's own one-page, double-column PDF
fixture. It contains formula text, a vector table, and an embedded image; no
copyrighted paper is redistributed.

![PaperLocale source, translation, and all-page QA demo](docs/assets/demo.gif)

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
- page geometry, image-object, vector-drawing, blank-page, placeholder, and all-page visual checks.

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

Evaluate a real Provider on every public domain case and save candidates next
to their references:

```bash
paperlocale provider-eval \
  --provider codex-local \
  --model gpt-5.6-sol \
  --reasoning-effort high \
  --domain atmospheric-science \
  --output provider-eval.json
```

The report automatically evaluates only the hard content contract and exact
reference matches. It never treats string similarity as semantic accuracy;
every candidate remains marked for manual domain review.

## Install

Python 3.10–3.13 and Poppler's `pdftoppm` are required for the complete workflow.
PaperLocale 0.3.2 is published on PyPI through attested Trusted Publishing.
The audited maintainer procedure and release evidence are documented in
[docs/PYPI_PUBLISHING.zh-CN.md](docs/PYPI_PUBLISHING.zh-CN.md).

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install "paperlocale[layout]==0.3.2"
paperlocale domain-check atmospheric-science
```

## Quick start

Use the same resumable command to initialize the run, collect layout segments,
translate, validate, rebuild, and generate all-page QA:

```bash
# Uses the authenticated Codex CLI session on this trusted local machine.
paperlocale run paper.pdf --run-dir runs/paper \
  --provider codex-local \
  --model gpt-5.6-sol \
  --reasoning-effort high \
  --domain atmospheric-science

# The first invocation stops after collection for reference review.
paperlocale confirm-references --run-dir runs/paper \
  --segment-id manually-confirmed-reference-segment-id \
  --confirmed-by "Your name"

# Rerun the original paperlocale run command after confirmation.
paperlocale run paper.pdf --run-dir runs/paper \
  --provider codex-local \
  --model gpt-5.6-sol \
  --reasoning-effort high \
  --domain atmospheric-science

# Inspect every image under runs/paper/qa/comparisons/ before acceptance.
paperlocale accept --run-dir runs/paper --reviewed-by "Your name"
```

If a stage fails, rerun the same `paperlocale run` command. The manifest resumes
from the last completed stage and every accepted segment is reused, including
valid rows from a batch that also produced rejected candidates. Those candidates
and their contract errors are stored in `rejected_translations.jsonl`.
The command deliberately stops at `qa_generated`; it never records human
acceptance. Once translation is complete, a resume command does not need
`--provider` or API credentials.

If a rejected segment is genuinely non-translatable, such as a pure formula or
an author-name list, confirm it explicitly instead of adding artificial Chinese
text or weakening the global CJK gate:

```bash
paperlocale confirm-passthrough --run-dir runs/paper \
  --segment-id confirmed-nontranslatable-segment-id \
  --reason "Pure formula with no translatable prose" \
  --confirmed-by "Your name"
```

The audited map binds the source PDF and `segments.jsonl` hashes. Confirmed
segments must remain byte-for-byte equal to their source, never reach the
Provider, and can safely resolve a rejected partial batch without repeating
already accepted model calls.

Before any Provider call, PaperLocale also compares collected segments with
the source PDF's exact visible page text. A segment that starts or ends inside
the same ASCII word (for example `Figu` + `re ... perio` + `d`), or a short
ASCII segment absent from visible page text, is written to
`segment_safety_review.jsonl` and blocks translation. Inspect that local file,
then confirm every listed ID with `confirm-passthrough`. v0.3.2 deliberately
keeps these objects unchanged; full translation requires upstream adjacent-
object context or merge support.

The default reference policy is `preserve`. PaperLocale writes every segment to
`reference_review.jsonl`, automatically selects only long segments that match
the source PDF's exact `REFERENCES` region, and requires explicit confirmation
before any model call. Automatically matched IDs do not need to be repeated;
omit `--segment-id` when no manual additions are needed. The confirmed map is
bound to the source PDF and `segments.jsonl` hashes. Use
`--reference-policy translate-titles` to translate work titles only; reference
rows do not use body-domain glossary gates.

The current released BabelDOC may still re-typeset an unchanged reference
paragraph. The object-level fix is proposed upstream in
[BabelDOC #610](https://github.com/funstory-ai/BabelDOC/issues/610) and
[PR #611](https://github.com/funstory-ai/BabelDOC/pull/611); PaperLocale does
not carry a local PDF overlay workaround while that review is pending.

The schema 4 run manifest binds the domain-pack content hash, provider, model,
reasoning effort, Codex CLI version, collect/render layout-engine versions, and
any human-confirmed passthrough map. A Codex run therefore requires an explicit
`--model`.

When vector objects disappear, QA records their page, bounding box, and area,
and draws red boxes at the expected locations in both comparison panels. Import
an independently repaired candidate through the audited path:

```bash
paperlocale apply-vector-repair --run-dir runs/paper \
  --repaired-pdf repaired-paper.pdf \
  --description "Restore page 1 link vector icons"
```

The command rejects candidates that alter text, page geometry, or image counts,
backs up the previous PDF, appends `repair_history`, and requires QA and human
acceptance to run again.

For a BYOK OpenAI-compatible endpoint:

```bash
export PAPERLOCALE_API_KEY="your-key"
paperlocale run paper.pdf --run-dir runs/paper \
  --provider openai-compatible \
  --base-url https://api.example.com/v1 \
  --model your-model \
  --domain atmospheric-science
```

Remote compatible endpoints must use HTTPS. Plain HTTP is accepted only for
loopback services on `localhost`, `127.0.0.1`, or `::1`.

For explicit stage-by-stage control, the same production path remains available
as `init-run -> collect -> reference-review/confirm-references -> optional
confirm-passthrough -> translate -> validate -> render -> qa -> accept`.

See the detailed [Chinese guide](README.zh-CN.md), [ROADMAP](docs/ROADMAP.md), [ARCHITECTURE](docs/ARCHITECTURE.md), [domain-pack guide](docs/DOMAIN_PACKS.zh-CN.md), [Codex for Open Source readiness](docs/CODEX_FOR_OSS_READINESS.zh-CN.md), and [PROVENANCE](docs/PROVENANCE.md).

## Citation and community

Research and teaching users can cite the software through
[`CITATION.cff`](CITATION.cff); GitHub exposes the same metadata through its
“Cite this repository” control. Participation in issues, pull requests, and
reviews follows the [PaperLocale Code of Conduct](CODE_OF_CONDUCT.md).

## Development

Unit tests do not call a model or require the layout engine:

```bash
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
```

After layout-engine upgrades, run the deterministic full-path smoke test:

```bash
python -m pip install -e ".[layout,test]"
python scripts/layout_smoke.py \
  --output tmp/layout-smoke-001 \
  --demo-gif tmp/layout-smoke-001.gif
```

The script intentionally stops before visual acceptance and prints the comparison image to inspect.
The scheduled compatibility workflow repeats this real CLI check weekly against
the newest `pdf2zh-next` release allowed by the declared dependency range.

## Contributing

Start with the scoped [good first issues](https://github.com/hazugi2004/paperlocale/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22), or read [CONTRIBUTING.md](CONTRIBUTING.md). Current entry points cover an ecology domain pack, a two-page synthetic PDF regression, and Ubuntu installation verification.

## License

GNU Affero General Public License v3.0 only. This choice is aligned with the AGPL-licensed PDF layout engines the project is designed to integrate.
