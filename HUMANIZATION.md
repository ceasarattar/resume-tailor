# HUMANIZATION.md — making generated text read like *you* wrote it

Two surfaces need this: résumé bullets (`app/humanize.py`) and free-text
application answers / cover letters (`app/answers.py::generate_answer`). The goal
is **natural, human, in-your-voice** writing — and, where an employer runs an AI
detector on free-text, text that doesn't trip one. The hard constraint never
changes: **humanization may not alter a single fact.** The metric guard in
`humanize.py` and the `grounded` flag in `answers.py` enforce that.

This doc is the expert layer: what AI detectors actually measure, the techniques
that move those measurements, and how each maps to this repo.

## What detectors actually measure (so we know what to move)

Surface paraphrasing is dead. Modern detectors (GPTZero, Originality, Turnitin,
Pangram) and the research behind them score **statistical and stylometric**
features, not vocabulary blocklists:

- **Perplexity** — how "surprised" a language model is by your next word. LLM
  text is *low*-perplexity (it picks the high-probability word). Human text is
  spikier and less predictable.
- **Burstiness** — the *variance* of sentence length and complexity. Humans mix a
  three-word fragment with a 40-word sentence. LLMs default to uniform,
  medium-length sentences. Low burstiness is the single strongest tell.
- **Stylometry** — character n-gram distributions, part-of-speech tag sequences,
  dependency-parse shapes, punctuation frequency, function-word ratios. These are
  hard to fake by editing words and are what survives paraphrasing.

Sources: [Understanding perplexity & burstiness](https://hastewire.com/blog/understanding-perplexity-and-burstiness-in-ai-text-detection),
[perplexity/burstiness 2025](https://humanizeai.now/blog/perplexity-burstiness-2025),
[LLMs can be guided to evade detection (arXiv 2305.10847)](https://arxiv.org/pdf/2305.10847),
[AI humanizer guide 2025](https://www.writelyhuman.com/blog/ai-humanizer-ultimate-guide-2025).

## The techniques experts actually use (ranked by leverage)

1. **Voiceprint / style transfer to a personal corpus.** The highest-leverage
   move by far: condition generation on *your own* writing so the output matches
   your distribution, not the model's. Feed real samples (your `about-me.md`, past
   cover letters, even Slack/email snippets) as a style exemplar and instruct the
   model to imitate cadence, not content. A detector keyed to "average LLM style"
   sees *your* style instead. This also makes answers genuinely yours, which keeps
   the whole thing honest.

2. **Burstiness injection.** Deliberately vary sentence length: target a high
   standard deviation. Drop in a deliberate fragment. Follow a long, clause-heavy
   sentence with a four-word punch. This is the most reliable single statistical
   lever and is easy to both generate and *measure* (see the linter below).

3. **Perplexity raising via specificity.** The cure for low perplexity is concrete
   detail, not thesaurus swaps. Real numbers, real tool names, a specific decision
   you made — these are low-probability tokens a model wouldn't have guessed, and
   they read human *and* truthful. Conversely, cut the high-probability connective
   scaffolding LLMs love: "Furthermore," "Moreover," "In today's fast-paced…,"
   "It is worth noting." Those are perplexity-lowering boilerplate.

4. **Stylometric camouflage.** Use contractions. Start an occasional sentence with
   "And" or "But." Vary punctuation rhythm; don't pile up em-dashes (a notorious
   LLM signature — `humanize.py` already flags ≥2 per line). Break parallelism and
   the rule-of-three ("fast, scalable, and reliable") — humans are asymmetric.

5. **Imperfection and idiosyncrasy.** A mild aside, a specific opinion, one
   slightly informal word. Not errors — texture. LLMs over-smooth; humans don't.

6. **Adversarial self-critique (two-pass).** Generate → run a detector-style critic
   over the draft (your own linter, or an LLM asked "which sentences read as
   AI-generated and why?") → rewrite only the flagged spans. This is what the good
   commercial humanizers do under the hood, locally and cheaply.

7. **Watermark/model laundering (context).** Some providers embed statistical
   watermarks; paraphrasing through a *different* model breaks them. Not relevant
   to us (we author with Claude and control the prompt), but worth knowing the
   mechanism exists.

## How this maps to the repo — and what to upgrade

Already implemented in `app/humanize.py`:
- A deterministic **AI-tell linter** (`find_ai_tells`) covering LLM vocab, filler,
  em-dash overuse, weak openers, negative parallelism, rule-of-three.
- An **LLM rewrite** with a banned-word list aligned to the tailor prompt.
- A **metric guard** that reverts any line whose numbers changed (honesty).

High-leverage upgrades (in priority order):
1. **Voiceprint.** Add a short, real writing sample to the system prompt of both
   `humanize.py::_SYSTEM` and `answers.py::_ANSWER_SYSTEM` — "match this person's
   cadence and word choice" — sourced from `about-me.md` plus, ideally, a new
   `profile/voice.md` holding 1–2 paragraphs you actually wrote.
2. **Burstiness metric.** `find_ai_tells` now flags low sentence-length variance
   (see `_burstiness_low`); extend the rewrite instruction to *target* high
   variance, and reject rewrites that flatten it.
3. **Apply the humanizer to free-text answers.** `answers.py::generate_answer`
   already bans the same words; route its output through the same burstiness +
   tell linter and a one-pass rewrite for anything over ~2 sentences.
4. **Self-critique loop.** For cover letters specifically, add a second pass that
   asks the model to find and fix its own AI tells before returning.

## The honesty / ethics line

This is for making *your truthful* answers sound like *you* — not for fabricating
credentials or committing academic fraud. Two rules hold:

- **Never alter facts to sound human.** The metric guard and `grounded` flag are
  not optional; a "more human" sentence that changes a number is reverted.
- **Don't deny help if asked directly.** If an application asks whether AI assisted
  your writing, answer honestly. Humanization makes the prose yours in *voice*; it
  is not a tool for lying about authorship where authorship is the question.
