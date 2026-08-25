#!/usr/bin/env python3
"""Main benchmark runner for the AI Brainteaser Benchmark."""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from tqdm import tqdm

from models import ModelConfig, ModelResponse, query_model
from judge import judge_response

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent

# Default testset: v3 (hard set, the 100-question English dataset in data/)
DEFAULT_TESTSET = "v3"

# Testset registry: maps testset name -> (questions_file, categories_file)
TESTSETS = {
    "v3": ("brainteasers.json", "brainteaser_categories.json"),
    "v3_chinese": ("brainteasers_chinese.json", "brainteaser_categories_chinese.json"),
}


def load_config(config_path: str = "config.yaml") -> dict:
    with open(Path(__file__).parent / config_path) as f:
        return yaml.safe_load(f)


def resolve_testset(testset: str, data_dir: str) -> tuple[str, str]:
    """Resolve testset name to (questions_file, categories_file).

    If testset is a registered name, use the mapping.
    Otherwise treat it as a questions filename directly.
    """
    if testset in TESTSETS:
        return TESTSETS[testset]
    # Fallback: treat as a direct filename
    return (testset, "brainteaser_categories.json")


def load_questions(data_dir: str, testset: str = DEFAULT_TESTSET) -> list[dict]:
    questions_file, _ = resolve_testset(testset, data_dir)
    path = ROOT_DIR / data_dir / questions_file
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_category_spec(spec: str, questions: list[dict]) -> list[str]:
    """Resolve --category tokens to full category names present in the dataset.

    Each comma-separated token can be a category number ('8'), or a substring
    of the category name ('physics', 'state/identity'). Returns the matched
    category strings; exits with the list of available categories on no match.
    """
    available = sorted({q["category"] for q in questions})
    wanted: list[str] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        matches = []
        if token.isdigit():
            # A number matches ONLY the category number, not substrings
            # ("8" must not match "18. Circular dependency").
            for cat in available:
                if cat.split(".", 1)[0].strip() == token:
                    matches.append(cat)
        else:
            for cat in available:
                if token.lower() in cat.lower():
                    matches.append(cat)
        if not matches:
            print(f"No category matches '{token}'. Available categories:")
            for cat in available:
                print(f"  {cat}")
            sys.exit(1)
        for m in matches:
            if m not in wanted:
                wanted.append(m)
    return wanted


def parse_model_configs(config: dict) -> list[ModelConfig]:
    models = []
    for m in config["models"]:
        if not m.get("enabled", True):
            continue
        models.append(ModelConfig(
            name=m["name"],
            provider=m["provider"],
            model_id=m["model_id"],
            enabled=True,
            base_url=m.get("base_url"),
            api_key_env=m.get("api_key_env"),
            reasoning_effort=m.get("reasoning_effort"),
            max_tokens=m.get("max_tokens"),
            reasoning_budget_tokens=m.get("reasoning_budget_tokens"),
            reasoning_budget_message=m.get("reasoning_budget_message"),
        ))
    return models


_INVALID_DIR_CHARS = re.compile(r'[\\/:*?"<>|]')


def safe_dir_name(name: str) -> str:
    """Make a model name usable as a results-folder name on any OS.

    Model ids discovered from the server look like 'owner/repo:Q4_K_XL' —
    fine for display, invalid as a Windows folder name."""
    return _INVALID_DIR_CHARS.sub("_", name)


def get_raw_path(output_dir: str, testset: str, model_name: str, question_id: int, run: int) -> Path:
    p = ROOT_DIR / output_dir / testset / "raw" / safe_dir_name(model_name)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"q{question_id:03d}_run{run:02d}.json"


def is_complete(output_dir: str, testset: str, model_name: str, question_id: int, run: int) -> bool:
    path = get_raw_path(output_dir, testset, model_name, question_id, run)
    if not path.exists():
        return False
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("response_text", "") != "" and data.get("error") is None
    except (json.JSONDecodeError, KeyError):
        return False


def save_raw(output_dir: str, testset: str, result: dict) -> None:
    path = get_raw_path(
        output_dir, testset, result["model_name"], result["question_id"], result["run_number"]
    )
    with open(path, "w") as f:
        json.dump(result, f, indent=2)


async def run_single(
    semaphore: asyncio.Semaphore,
    model_config: ModelConfig,
    question: dict,
    run_number: int,
    max_tokens: int,
    output_dir: str,
    testset: str,
    judge_cfg: dict,
    skip_judge: bool = False,
) -> dict:
    """Run a single question against a single model, then judge it."""
    async with semaphore:
        # Query model
        resp: ModelResponse = await query_model(
            config=model_config,
            question=question["question"],
            question_id=question["id"],
            run_number=run_number,
            max_tokens=max_tokens,
        )

        result = {
            "model_name": resp.model_name,
            "question_id": resp.question_id,
            "run_number": resp.run_number,
            "testset": testset,
            "question": question["question"],
            "ground_truth": question["answer"],
            "category": question["category"],
            "response_text": resp.response_text,
            "reasoning": resp.reasoning,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            # Thinking-budget provenance (None when the model doesn't use a
            # budget) — records how this run was constrained.
            "reasoning_budget_tokens": model_config.reasoning_budget_tokens,
            "reasoning_budget_message": model_config.reasoning_budget_message,
            "error": resp.error,
        }

        # Judge correctness (skip if model errored)
        if resp.error:
            result["judgment"] = {"correct": False, "reasoning": "Model error", "error": resp.error}
        elif skip_judge:
            result["judgment"] = None  # pending: judge later with --judge-only
        else:
            judgment = await judge_response(
                question=question["question"],
                ground_truth=question["answer"],
                response=resp.response_text,
                judge_model=judge_cfg.get("model_id", "claude-sonnet-4-20250514"),
                judge_provider=judge_cfg.get("provider"),
                judge_base_url=judge_cfg.get("base_url"),
                api_key_env=judge_cfg.get("api_key_env"),
                reasoning_budget_tokens=judge_cfg.get("reasoning_budget_tokens"),
                reasoning_budget_message=judge_cfg.get("reasoning_budget_message"),
            )
            result["judgment"] = judgment

        # Save raw result
        save_raw(output_dir, testset, result)
        return result


async def run_benchmark(
    config: dict,
    testset: str = DEFAULT_TESTSET,
    models: list[ModelConfig] | None = None,
    question_ids: list[int] | None = None,
    runs: int | None = None,
    resume: bool = True,
    skip_judge: bool = False,
    categories: list[str] | None = None,
) -> list[dict]:
    """Run the full benchmark."""
    all_questions = load_questions(config["data_dir"], testset=testset)
    questions = all_questions
    if categories:
        questions = [q for q in questions if q["category"] in categories]
        if not questions:
            logger.error("No questions match the selected categories — nothing to do.")
            return []
    all_models = models or parse_model_configs(config)
    num_runs = runs or config["runs_per_question"]
    max_tokens = config.get("max_tokens", 8192)
    output_dir = config["output_dir"]
    judge_cfg = dict(config.get("judge", {}))
    judge_cfg.setdefault("model_id", "claude-sonnet-4-20250514")
    max_concurrent = config.get("max_concurrent_requests", 5)

    # Filter questions if specified
    if question_ids:
        questions = [q for q in questions if q["id"] in question_ids]

    # Build task list
    tasks_to_run = []
    for model_config in all_models:
        for question in questions:
            for run in range(1, num_runs + 1):
                if resume and is_complete(output_dir, testset, model_config.name, question["id"], run):
                    continue
                tasks_to_run.append((model_config, question, run))

    total = len(tasks_to_run)
    if total == 0:
        logger.info("All tasks already complete. Nothing to run.")
        return []

    skipped = (len(all_models) * len(questions) * num_runs) - total
    logger.info(
        f"[testset={testset}] Running {total} tasks ({skipped} already complete) | "
        f"{len(all_models)} models × {len(questions)} questions × {num_runs} runs"
    )

    semaphore = asyncio.Semaphore(max_concurrent)

    # Create async tasks with progress bar
    pbar = tqdm(total=total, desc=f"Benchmark ({testset})", unit="task")

    async def run_with_progress(model_config, question, run_num):
        result = await run_single(
            semaphore, model_config, question, run_num,
            max_tokens, output_dir, testset, judge_cfg, skip_judge,
        )
        pbar.update(1)
        judgment = result.get("judgment")
        if judgment is None:
            status = "-"  # not judged yet (--no-judge / --judge-only pending)
        else:
            status = "✓" if judgment.get("correct", False) else "✗"
        pbar.set_postfix_str(f"{model_config.name} q{question['id']} {status}")
        return result

    coros = [
        run_with_progress(mc, q, r)
        for mc, q, r in tasks_to_run
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    pbar.close()

    # Filter out exceptions
    valid_results = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Task exception: {r}")
        else:
            valid_results.append(r)

    # Aggregate scores over the FULL question set (pass None to discover ALL
    # models with results) so scores.json keeps accumulating across runs.
    aggregate_scores(config, testset, None, all_questions, num_runs)

    return valid_results


def aggregate_scores(
    config: dict,
    testset: str,
    models: list[ModelConfig] | None,
    questions: list[dict],
    num_runs: int,
) -> dict:
    """Aggregate raw results into scores.json.

    Discovers ALL models with results in the raw directory, not just
    the ones passed in. This makes it safe to call from parallel runs.
    """
    output_dir = config["output_dir"]
    raw_dir = ROOT_DIR / output_dir / testset / "raw"

    # Discover all model dirs that have results, merge with passed models
    model_names_from_dir = set()
    if raw_dir.exists():
        model_names_from_dir = {d.name for d in raw_dir.iterdir() if d.is_dir() and any(d.iterdir())}

    # Build model name list: discovered dirs + passed models that actually
    # have raw files. Config names without results (e.g. 'local-llama' once
    # runs are saved under --model-name display names) must not create rows.
    all_model_names = set(model_names_from_dir)
    if models:
        for m in models:
            d = raw_dir / safe_dir_name(m.name)
            if d.is_dir() and any(d.iterdir()):
                all_model_names.add(m.name)

    scores = {"testset": testset, "models": {}, "meta": {"num_runs": num_runs, "num_questions": len(questions)}}

    for model_name in sorted(all_model_names):
        model_scores = {
            "overall_correct": 0,
            "overall_total": 0,
            "by_category": {},
            "by_question": {},
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        }

        for question in questions:
            q_correct = 0
            q_total = 0
            cat = question["category"]

            if cat not in model_scores["by_category"]:
                model_scores["by_category"][cat] = {"correct": 0, "total": 0}

            for run in range(1, num_runs + 1):
                path = get_raw_path(output_dir, testset, model_name, question["id"], run)
                if not path.exists():
                    continue

                try:
                    with open(path) as f:
                        data = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    continue

                judgment = data.get("judgment")
                if judgment is None:
                    # Pending (e.g. run with --no-judge): don't count it yet.
                    continue
                correct = judgment.get("correct", False)
                q_total += 1
                model_scores["overall_total"] += 1
                model_scores["by_category"][cat]["total"] += 1
                model_scores["total_input_tokens"] += data.get("input_tokens", 0)
                model_scores["total_output_tokens"] += data.get("output_tokens", 0)

                if correct:
                    q_correct += 1
                    model_scores["overall_correct"] += 1
                    model_scores["by_category"][cat]["correct"] += 1

            model_scores["by_question"][str(question["id"])] = {
                "correct": q_correct,
                "total": q_total,
                "accuracy": q_correct / q_total if q_total > 0 else 0,
            }

        # Compute derived metrics
        total = model_scores["overall_total"]
        model_scores["overall_accuracy"] = (
            model_scores["overall_correct"] / total if total > 0 else 0
        )

        for cat_data in model_scores["by_category"].values():
            cat_data["accuracy"] = (
                cat_data["correct"] / cat_data["total"] if cat_data["total"] > 0 else 0
            )

        # Consistency: fraction of questions where model gets 100% of runs correct
        reliable = 0
        q_count = 0
        for q_data in model_scores["by_question"].values():
            if q_data["total"] > 0:
                q_count += 1
                if q_data["accuracy"] == 1.0:
                    reliable += 1
        model_scores["reliability"] = reliable / q_count if q_count > 0 else 0

        scores["models"][model_name] = model_scores

    # Save per-testset scores
    scores_dir = ROOT_DIR / output_dir / testset
    scores_dir.mkdir(parents=True, exist_ok=True)
    scores_path = scores_dir / "scores.json"
    with open(scores_path, "w") as f:
        json.dump(scores, f, indent=2)

    logger.info(f"Scores saved to {scores_path}")
    return scores


async def judge_existing(
    config: dict,
    testset: str = DEFAULT_TESTSET,
    model_name: str | None = None,
    re_judge: bool = False,
) -> None:
    """Phase 2: judge responses that were saved earlier (see --judge-only).

    Use this after unloading the model under test and loading a (more
capable) judge model into the same llama-server endpoint. Raw files are
    updated in place, then scores are re-aggregated.
    """
    judge_cfg = dict(config.get("judge", {}))
    judge_cfg.setdefault("model_id", "claude-sonnet-4-20250514")
    raw_dir = ROOT_DIR / config["output_dir"] / testset / "raw"
    if not raw_dir.exists():
        logger.error(f"No results yet in {raw_dir} — run the benchmark first (with --no-judge).")
        return

    pending: list[tuple[Path, dict]] = []
    for model_dir in sorted(raw_dir.iterdir()):
        if not model_dir.is_dir():
            continue
        if model_name and model_dir.name != safe_dir_name(model_name):
            continue
        for path in sorted(model_dir.glob("q*_run*.json")):
            with open(path) as f:
                data = json.load(f)
            judgment = data.get("judgment")
            if judgment is None:
                needs = True
            elif re_judge:
                # Re-judge everything except runs where the model itself errored
                # (those have an empty response and nothing to judge).
                needs = bool(data.get("response_text", "").strip())
            elif judgment.get("error") and data.get("response_text", "").strip():
                # A previous judging attempt FAILED (e.g. the judge model was
                # not loaded yet, so the response was saved with an error
                # judgment). Retry it next time. Model-error runs have an
                # empty response and stay skipped.
                needs = True
            else:
                needs = False
            if needs:
                pending.append((path, data))

    if not pending:
        logger.info("Nothing to judge (all responses already judged). Use --re-judge to force.")
        return

    logger.info(
        f"Judging {len(pending)} saved response(s) with judge model "
        f"'{judge_cfg.get('model_id')}' (provider: {judge_cfg.get('provider', 'anthropic')})..."
    )
    semaphore = asyncio.Semaphore(config.get("max_concurrent_requests", 2))

    async def judge_one(path: Path, data: dict) -> None:
        async with semaphore:
            judgment = await judge_response(
                question=data["question"],
                ground_truth=data["ground_truth"],
                response=data.get("response_text", ""),
                judge_model=judge_cfg.get("model_id"),
                judge_provider=judge_cfg.get("provider"),
                judge_base_url=judge_cfg.get("base_url"),
                api_key_env=judge_cfg.get("api_key_env"),
                reasoning_budget_tokens=judge_cfg.get("reasoning_budget_tokens"),
                reasoning_budget_message=judge_cfg.get("reasoning_budget_message"),
            )
            data["judgment"] = judgment
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"  judged {path.relative_to(ROOT_DIR)} -> correct={judgment.get('correct')}")

    await asyncio.gather(*[judge_one(p, d) for p, d in pending])

    # Re-aggregate scores over all models with results
    questions = load_questions(config["data_dir"], testset=testset)
    aggregate_scores(config, testset, None, questions, config["runs_per_question"])


def check_completeness(config: dict, testset: str = DEFAULT_TESTSET, categories: list[str] | None = None) -> None:
    """Check which tasks are complete vs missing."""
    questions = load_questions(config["data_dir"], testset=testset)
    if categories:
        questions = [q for q in questions if q["category"] in categories]
    models = parse_model_configs(config)
    num_runs = config["runs_per_question"]
    output_dir = config["output_dir"]

    print(f"Testset: {testset}"
          + (f" | categories: {categories}" if categories else ""))
    for model_config in models:
        complete = 0
        missing = 0
        errors = 0
        for question in questions:
            for run in range(1, num_runs + 1):
                path = get_raw_path(output_dir, testset, model_config.name, question["id"], run)
                if not path.exists():
                    missing += 1
                else:
                    try:
                        with open(path) as f:
                            data = json.load(f)
                        if data.get("error"):
                            errors += 1
                        else:
                            complete += 1
                    except (json.JSONDecodeError, FileNotFoundError):
                        missing += 1

        total = len(questions) * num_runs
        print(
            f"{model_config.name:25s} | "
            f"complete: {complete:4d}/{total} | "
            f"missing: {missing:4d} | "
            f"errors: {errors:4d}"
        )


def parse_question_range(s: str) -> list[int]:
    """Parse '1-5' or '1,3,5' or '42' into a list of ints."""
    ids = []
    for part in s.split(","):
        if "-" in part:
            start, end = part.split("-", 1)
            ids.extend(range(int(start), int(end) + 1))
        else:
            ids.append(int(part))
    return ids


def main():
    parser = argparse.ArgumentParser(description="AI Brainteaser Benchmark Runner")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--testset", default=DEFAULT_TESTSET,
                        help=f"Testset name (registered: {list(TESTSETS.keys())}). Default: {DEFAULT_TESTSET}")
    parser.add_argument("--model", help="Run only this model (by name)")
    parser.add_argument("--model-name",
                        help="With --model: override the model's name (results folder) and API "
                             "model id (e.g. a model id discovered from the local server's "
                             "/v1/models)")
    parser.add_argument("--questions", help="Question IDs to run (e.g., '1-5' or '1,3,5')")
    parser.add_argument("--category",
                        help="Comma-separated categories to run: a number ('8'), a name, or a name "
                             "substring ('physics'). E.g. --category 8 or --category '5,red herring'. "
                             "Combines with --questions as an intersection.")
    parser.add_argument("--runs", type=int, help="Override number of runs per question")
    parser.add_argument("--no-resume", action="store_true", help="Don't skip completed tasks")
    parser.add_argument("--check", action="store_true", help="Check completeness only")
    parser.add_argument("--aggregate-only", action="store_true", help="Only re-aggregate scores")
    parser.add_argument("--no-judge", action="store_true",
                        help="Query models but save responses without judging "
                             "(judge later with --judge-only, e.g. after loading a judge model)")
    parser.add_argument("--judge-only", action="store_true",
                        help="Don't query any model: judge previously saved responses in place "
                             "(the judge model must be running at the configured judge endpoint), "
                             "then re-aggregate scores")
    parser.add_argument("--re-judge", action="store_true",
                        help="With --judge-only: re-judge ALL responses, not only pending ones")
    args = parser.parse_args()

    config = load_config(args.config)

    categories = None
    if args.category:
        all_qs = load_questions(config["data_dir"], testset=args.testset)
        categories = parse_category_spec(args.category, all_qs)

    if args.check:
        check_completeness(config, testset=args.testset, categories=categories)
        return

    if args.judge_only:
        # --model-name (if given) is the results-folder name of the model
        asyncio.run(judge_existing(
            config,
            testset=args.testset,
            model_name=args.model_name or args.model,
            re_judge=args.re_judge,
        ))
        return

    if args.aggregate_only:
        questions = load_questions(config["data_dir"], testset=args.testset)
        models = parse_model_configs(config)
        aggregate_scores(config, args.testset, models, questions, config["runs_per_question"])
        return

    # Filter to single model if specified
    models = None
    if args.model:
        all_models = parse_model_configs(config)
        models = [m for m in all_models if m.name == args.model]
        if not models:
            print(f"Model '{args.model}' not found. Available: {[m.name for m in all_models]}")
            sys.exit(1)
        if args.model_name:
            for m in models:
                m.name = args.model_name
                m.model_id = args.model_name

    question_ids = None
    if args.questions:
        question_ids = parse_question_range(args.questions)

    asyncio.run(run_benchmark(
        config=config,
        testset=args.testset,
        models=models,
        question_ids=question_ids,
        runs=args.runs,
        resume=not args.no_resume,
        skip_judge=args.no_judge,
        categories=categories,
    ))


if __name__ == "__main__":
    main()
