# BrainBench

**A benchmark exposing commonsense reasoning gaps in Large Language Models.**

BrainBench is a dataset of 100 brainteaser questions spanning 20 failure categories, each targeting a specific reasoning trap that LLMs fall into. These questions are trivially easy for humans but systematically fool AI models that rely on surface-level heuristics instead of genuine reasoning.

**Paper:** [BrainBench: Exposing the Commonsense Reasoning Gap in Large Language Models](paper/main.pdf)

## Key Results

| Rank | Model | Accuracy | Reliability |
|------|-------|----------|-------------|
| 1 | Claude Opus 4.6 (thinking) | 80.3% | 74% |
| 2 | Claude Opus 4.6 | 77.3% | 71% |
| 3 | Claude Sonnet 4.6 | 76.7% | 69% |
| 4 | Claude Haiku 4.5 | 74.3% | 58% |
| 5 | GPT-5.4 (thinking) | 74.0% | 64% |
| 6 | GPT-5.4 | 70.7% | 63% |
| 7 | GPT-4o | 39.7% | 27% |
| 8 | GPT-4o Mini | 39.7% | 24% |

The hardest categories -- *implicit physical constraint* and *wrong vantage point* -- average only 40% accuracy across all models.

## Example

> **Q:** I need to return my rental car. The rental agency is just across the street. Should I walk over or drive?
>
> **A:** Drive. You need to return the car itself -- walking over leaves it behind.

GPT-4o recommends walking. Every human knows you drive.

## The 20 Failure Categories

| # | Category | Avg Accuracy |
|---|----------|:---:|
| 1 | Implicit physical constraint | 40% |
| 2 | Wrong vantage point | 40% |
| 3 | Semantic scope trick | 50% |
| 4 | Default assumption hijack | 52% |
| 5 | Pragmatic/social intent | 57% |
| 6 | Answer hiding in plain sight | 59% |
| 7 | Negation/exception logic | 61% |
| 8 | Broken/dead device self-reference | 61% |
| 9 | Wrong test conditions | 63% |
| 10 | Red herring overload | 70% |
| 11 | Framing/anchoring trap | 71% |
| 12 | Self-defeating action | 73% |
| 13 | Circular dependency | 73% |
| 14 | Naive physics error | 73% |
| 15 | Embedded false premise | 76% |
| 16 | Goal-means mismatch | 78% |
| 17 | Temporal impossibility | 78% |
| 18 | State/identity tracking | 80% |
| 19 | Quantity/counting illusion | 82% |
| 20 | Scale/growth intuition failure | 95% |

## Dataset

The dataset is available in English and Chinese:

- [`data/brainteasers.json`](data/brainteasers.json) -- 100 questions (English)
- [`data/brainteasers_chinese.json`](data/brainteasers_chinese.json) -- 100 questions (Chinese)
- [`data/brainteaser_categories.json`](data/brainteaser_categories.json) -- 20 category definitions

Each question has `id`, `category`, `question`, and `answer` fields.

## Running the Benchmark

### Setup

```bash
conda create -n brainbench python=3.11 -y
conda activate brainbench
pip install -r benchmark/requirements.txt
cp .env.example .env  # Fill in your API keys
```

### Run

```bash
# Single model, quick test
python benchmark/run_benchmark.py --model gpt-4o --questions 1 --runs 1

# Full benchmark for one model
python benchmark/run_benchmark.py --model gpt-4o --runs 3

# Check progress
python benchmark/run_benchmark.py --check

# Re-aggregate scores
python benchmark/run_benchmark.py --aggregate-only
```

### Supported Models

Configure models in `benchmark/config.yaml`. Out of the box:
- OpenAI: GPT-4o, GPT-4o Mini, GPT-5.4, GPT-5.4 (thinking)
- Anthropic: Claude Haiku 4.5, Sonnet 4.6, Opus 4.6, Opus 4.6 (thinking)
- Any OpenAI-compatible API (OpenRouter, etc.)

## Testing a Local Model (llama-server)

The benchmark can test any model served with [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server`, which exposes an OpenAI-compatible API:

```bash
# 1. Start llama-server with your GGUF model
llama-server -m /path/to/your-model.gguf
# serves http://127.0.0.1:8080/v1  (no API key needed)

# 2. Quick smoke test (1 question, 1 run)
python benchmark/run_benchmark.py --model local-llama --questions 1 --runs 1

# 3. Full benchmark
python benchmark/run_benchmark.py --model local-llama --runs 3
```

- The model is defined in [`benchmark/config.yaml`](benchmark/config.yaml) (`local-llama` entry, `base_url: http://127.0.0.1:8080/v1`). If you run llama-server on another port/host, edit `base_url` there and in the `judge:` block.
- **Answer verification** is performed by the LLM judge (`benchmark/judge.py`). It is configured in the same `config.yaml` under `judge:` and, by default, uses **the same local model** being tested. Note this is a self-judge: it is biased toward agreeing with the model's own phrasing. To use an external judge instead, point the `judge:` block at a cloud provider, e.g.:

  ```yaml
  judge:
    provider: anthropic
    model_id: claude-sonnet-4-6
  ```

- Where are the "answers"? Each question's ground truth is in the `answer` field of [`data/brainteasers.json`](data/brainteasers.json). Per-question results (model response, tokens, judgment) are saved under `results/<testset>/raw/<model_name>/qNNN_runNN.json` and aggregated into `results/<testset>/scores.json`.
- Keep `max_concurrent_requests` low and `max_tokens` modest for small local models (context limits, VRAM).

### Running a subset of questions

```bash
# By question IDs
python benchmark/run_benchmark.py --model local-llama --questions 1-5 --runs 1

# By category: a number, a name, or a name substring
python benchmark/run_benchmark.py --model local-llama --category 8
python benchmark/run_benchmark.py --model local-llama --category physics

# Multiple categories (comma-separated)
python benchmark/run_benchmark.py --model local-llama --category "8,physics"
```

- `--category` accepts a category number (`8`), the full name, or a substring (`physics`). A numeric token matches **only** that category number (so `8` does not match `18`); name tokens are case-insensitive substrings.
- `--category` and `--questions` combine as an intersection.
- `--check --category 8` reports completeness for that category only.
- Because every model is then tested on the *same* questions per category, results stay directly comparable across models.

### Low-VRAM two-phase workflow (test and judge don't fit in memory at once)

If you can't keep the model under test and a bigger judge model loaded at the same time, split the run in two phases:

```bash
# Phase 1: run the benchmark with the model under test loaded;
# responses are saved WITHOUT judging (judgment stays pending).
llama-server -m /path/to/small-model.gguf
python benchmark/run_benchmark.py --model local-llama --runs 3 --no-judge

# Swap the model: stop llama-server and start it with the judge model
# (same port, or update judge.base_url in config.yaml).
llama-server -m /path/to/judge-model.gguf

# Phase 2: the saved responses are judged by the now-loaded judge model,
# raw files are updated in place, and scores are re-aggregated.
python benchmark/run_benchmark.py --judge-only
```

- Only **pending** responses are judged; already-judged ones are skipped, so you can re-run `--judge-only` freely (e.g. after a crash or a model swap).
- `--judge-only --re-judge` re-judges **everything**, useful if you want a second opinion from a different judge model.
- `--judge-only --model <name>` limits judging to one model's results.
- Resume works across phases: after phase 1, `--check` shows complete responses; only the judging step is still missing.

## Project Structure

## Project Structure

```
BrainBench/
├── data/                    # Dataset (English + Chinese)
├── benchmark/               # Evaluation code
│   ├── run_benchmark.py     # Main runner
│   ├── models.py            # Model API wrappers
│   ├── judge.py             # LLM-based answer judge
│   └── config.yaml          # Model configuration
├── results/                 # Analysis report + plots
├── scripts/                 # Analysis & verification scripts
└── paper/                   # LaTeX paper + PDF
```

## Citation

```bibtex
@article{tang2026brainbench,
  title={BrainBench: Exposing the Commonsense Reasoning Gap in Large Language Models},
  author={Tang, Yuzhe},
  journal={arXiv preprint},
  year={2026}
}
```

## License

MIT
