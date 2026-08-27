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
- an auditable manual ChatGPT Web bridge that exports hash-bound prompts and imports
  strict JSON responses without browser login, scraping, or automation;
- `qwen-mt`: BYOK access to Qwen-MT's dedicated translation endpoint, with
  one-segment checkpoints and domain-pack terminology intervention;
- built-in `atmospheric-science` and `ecology` packs with terminology and evaluation cases;
- a resumable `collect -> translate -> validate -> render -> qa -> accept` workflow;
- page geometry, image-object, vector-drawing, blank-page, placeholder, and all-page visual checks.

PaperLocale never reads or copies Codex authentication files. ChatGPT-managed Codex access is for trusted local use only and is not exposed as a public translation API.

## Domain packs

The built-in packs are `atmospheric-science` and `ecology`. Each pack contains a manifest, glossary, prompt rules, and evaluation cases. New disciplines can be added without changing the translation pipeline.

```bash
python -m paperlocale domain-check atmospheric-science
python -m paperlocale domain-check ecology
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
Stable PaperLocale releases are published on PyPI through attested Trusted
Publishing. The audited maintainer procedure and release evidence are documented in
[docs/PYPI_PUBLISHING.zh-CN.md](docs/PYPI_PUBLISHING.zh-CN.md).
PaperLocale 0.4.2 adds `--unattended` and the audited repair commands documented
below. To run this checkout, use the normal non-editable install path
`python -m pip install ".[layout]"`.
The v0.4.0 manual ChatGPT Web bridge is documented in
[docs/CHATGPT_WEB_MANUAL.zh-CN.md](docs/CHATGPT_WEB_MANUAL.zh-CN.md) for its
copy/paste workflow and usage-limit boundary.

After v0.4.2 is published, install the exact public release with:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install "paperlocale[layout]==0.4.2"
paperlocale --version
paperlocale domain-check atmospheric-science
```

## Quick start

For a single non-interactive command that produces a complete candidate PDF,
use `--unattended`. The `codex-local` provider invokes structured
`codex exec --ephemeral` in the background; it does not open or wait for a Codex
conversation window:

```bash
paperlocale run paper.pdf --run-dir runs/paper \
  --provider codex-local \
  --model gpt-5.6-sol \
  --reasoning-effort high \
  --domain atmospheric-science \
  --unattended
```

The command automatically adopts only deterministic reference matches and
deterministic layout-safety passthroughs, then translates, validates, rebuilds,
and generates all-page machine QA. On success it prints the exact candidate PDF
path under `runs/paper/render_output/`. References remain unchanged under the
default `preserve` policy, and unsafe split layout objects remain byte-for-byte
unchanged with an audit record. A "complete candidate PDF" therefore means that
all pages and collected segments are closed, not that formulas, references, or
unsafe fragments are forcibly converted to Chinese.

Unattended mode does not fabricate human visual acceptance: the final state is
still `qa_generated`. Provider, content-contract, or machine-QA failures exit
with an explicit error and retain valid checkpoints; rerun the same command to
resume. PaperLocale never silently switches providers.

The supervised workflow remains available when reference boundaries should be
reviewed manually:

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
omit `--segment-id` when no manual additions are needed. If page columns or
post-reference sections cause a reviewed false positive, repeat
`--exclude-segment-id ID` on `confirm-references`; only IDs from the current
automatic set can be excluded, and every exclusion is recorded in the bound
map. The confirmed map is bound to the source PDF and `segments.jsonl` hashes. Use
`--reference-policy translate-titles` to translate work titles only; reference
rows do not use body-domain glossary gates.

The current released BabelDOC may still re-typeset an unchanged reference
paragraph. The object-level fix is proposed upstream in
[BabelDOC #610](https://github.com/funstory-ai/BabelDOC/issues/610) and
[PR #611](https://github.com/funstory-ai/BabelDOC/pull/611); PaperLocale does
not carry a local PDF overlay workaround while that review is pending.

The schema 4 run manifest binds the domain-pack content hash, provider, model,
reasoning effort, Codex CLI version history, collect/render layout-engine versions,
and any human-confirmed passthrough map. A Codex run therefore requires an explicit
`--model`.

When vector objects disappear, QA records their page, bounding box, and area,
and draws red boxes at the expected locations in both comparison panels. When
the original source paths should remain at those locations, replay only the
exactly missing paths through PaperLocale's controlled command:

```bash
paperlocale restore-source-vectors --run-dir runs/paper \
  --description "Restore source vectors confirmed missing by machine QA"
```

The command requires a current QA report whose source and translated hashes
match the manifest. It only inspects pages where QA records
`source_vector_drawings > translated_vector_drawings`, matches missing paths at
0.01 PDF-point precision, rejects text, page-geometry, or image changes, backs
up the candidate, and records `repair_history`. Other independently repaired
candidates can still use the audited import path:

```bash
paperlocale apply-vector-repair --run-dir runs/paper \
  --repaired-pdf repaired-paper.pdf \
  --description "Restore page 1 link vector icons"
```

The command rejects candidates that alter text, page geometry, or image counts,
backs up the previous PDF, appends `repair_history`, and requires QA and human
acceptance to run again.

If visual review finds a broken caption that cannot be repaired at segment
level, replace only an explicitly reviewed page rectangle:

```bash
paperlocale apply-text-repair --run-dir runs/paper \
  --page 27 \
  --rect 40 120 500 160 \
  --replacement "Figure 5 corrected caption" \
  --font-file /path/to/NotoSansCJKsc-Regular.otf \
  --font-size 9.5 \
  --single-line \
  --description "Repair a split-token figure caption"
```

The command verifies that the font contains every replacement glyph, removes
text only inside the rectangle while preserving page geometry, images, links,
existing vectors, and outside text, and rejects overflow. It subsets the
repair font before embedding it without rewriting existing PDF font programs,
and records the font and subset hashes, byte reduction, before/after text,
geometry, PDF hashes, and backup in `repair_history`. QA and human acceptance
must then run again. Use a locally licensed font and do not commit it to the
repository; a TTF/OTF usually produces cleaner extraction metadata than a font
collection, but every result still goes through the same QA warnings and visual
review. `--single-line` measures the subset font's actual width, ascender, and
descender and rejects a shallow rectangle before modification if the line does
not fit; omit it when normal textbox wrapping is intended.

For a reviewed fragment that must only be removed, pass an explicit empty
replacement and omit the font options:

```bash
paperlocale apply-text-repair --run-dir runs/paper \
  --page 2 --rect 120 29 144 39 --replacement '' \
  --description "Remove a reviewed split-token fragment"
```

Removal mode embeds no font, records `text-removal` in `repair_history`, and
still enforces the same rectangle, outside-text, page, image, link, vector,
backup, QA, and human-acceptance gates. Whitespace-only replacements are
rejected.

Every recorded PDF repair can be reversed from the current chain tail only:

```bash
paperlocale rollback-last-repair --run-dir runs/paper \
  --reason "Remove the last audited repair before rebuilding QA"
```

The command requires the current PDF to match the tail `after_sha256` and its
backup to match `before_sha256`. It restores that exact backup, moves the entry
from `repair_history` to `repair_rollback_history`, returns the run to
`rendered`, and invalidates the old QA and visual-acceptance binding. It cannot
skip newer repairs; run machine QA and full visual review again after rollback.

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

For Alibaba Cloud Bailian's dedicated Qwen-MT endpoint, keep the API key in an
environment variable and select the explicit provider. The base URL is the API
version prefix and must not include `/chat/completions`:

```bash
export PAPERLOCALE_API_KEY="your-DashScope-key"
paperlocale run paper.pdf --run-dir runs/paper \
  --provider qwen-mt \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --model qwen-mt-plus \
  --domain atmospheric-science
```

Qwen-MT receives only one source segment per request. PaperLocale derives its
language codes, domain instruction, and glossary terms from the selected
domain pack, validates every returned segment, and saves each accepted result
before starting the next request. API keys are sent only in the Authorization
header and are never written to the run manifest.

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

Start with the scoped [good first issues](https://github.com/hazugi2004/paperlocale/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22), or read [CONTRIBUTING.md](CONTRIBUTING.md). Current entry points cover an ecology domain pack, Ubuntu installation verification, and independent review of the atmospheric-science Provider evaluation.

## License

GNU Affero General Public License v3.0 only. This choice is aligned with the AGPL-licensed PDF layout engines the project is designed to integrate.
