#!/usr/bin/env python3
"""BrainBench GUI — a free (FreeSimpleGUI) front-end for the CLI benchmark runner.

The GUI is a THIN wrapper: it launches the existing, tested CLI
(``run_benchmark.py``) as a subprocess, streams its output to a log pane,
computes live progress by watching the raw result files, and renders the
aggregated ``scores.json`` as tables. All benchmark logic stays in the CLI.

Run it from anywhere with:

    python benchmark/gui.py
"""

import json
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import FreeSimpleGUI as sg

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))

from run_benchmark import (  # noqa: E402  (import after sys.path tweak)
    DEFAULT_TESTSET,
    TESTSETS,
    load_config,
    load_questions,
    parse_category_spec,
    parse_model_configs,
    parse_question_range,
    safe_dir_name,
)

ROOT_DIR = BENCH_DIR.parent
GUI_CONFIG_PATH = BENCH_DIR / "gui_config.json"

TQDM_LINE = re.compile(r"\d+%\s*\|.*?[\d.]+/[\d.]+\s*\[")
LOG_MAX_LINES = 2000

# Persistent log files in the project root, appended line by line as they
# are shown in the GUI tabs (for debugging).
APP_LOG_PATH = ROOT_DIR / "bench_app.log.txt"
SERVER_LOG_PATH = ROOT_DIR / "bench_server.log.txt"

# Local llama.cpp server (scripts live in the user's llama.cpp folder).
# The variants and their launch scripts live in gui_config.json under
# "llama_versions" (list of {"name": ..., "script": ...}); these defaults
# apply when the key is missing/corrupt.
DEFAULT_LLAMA_VERSIONS = (
    ("ggml", "run_srv_ggml_p8080.cmd"),
    ("turboquant", "run_srv_turboquant_p8080.cmd"),
    ("unsloth", "run_srv_unsloth_p8080.cmd"),
)
DEFAULT_LLAMA_FOLDER = r"D:\Programs\llama.cpp"
CREATE_NO_WINDOW = 0x08000000  # win32 creation flag

# Cap values offered by the "Reasoning budget" combo (config.yaml,
# local-llama reasoning_budget_tokens — cap on the thinking phase).
REASONING_BUDGETS = (512, 1024, 2048, 4096, 8192)

# The config entry for the local server, and the id of the model selected
# from it (saved in gui_config.json as "local_model_id").
LOCAL_MODEL_NAME = "local-llama"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
# Lines matching this are shown in red in the system log
ERR_RE = re.compile(
    r"traceback \(most recent call last\)"
    r"|^\s*[A-Za-z_.]*error\b"
    r"|\bconnection (refused|reset|timed out)\b"
    r"|\[exit code [1-9]\d*\]",
    re.IGNORECASE)


def llama_script_path(folder: str, script: str) -> Path:
    return Path(folder) / script


def _pump_lines(proc: subprocess.Popen, q: "queue.Queue"):
    """Feed a subprocess's stdout into a queue until EOF (sentinel None)."""
    assert proc.stdout is not None
    for line in proc.stdout:
        q.put(line.rstrip("\n"))
    proc.wait()
    q.put(None)


# --------------------------------------------------------------------------- #
# GUI user settings (gui_config.json, next to gui.py)
# --------------------------------------------------------------------------- #
def load_llama_versions(cfg: dict | None = None) -> list[tuple[str, str]]:
    """Server variants from gui_config.json ('llama_versions'), (name, script).

    Entry format: {"name": "unsloth", "script": "run_srv_unsloth_p8080.cmd"}.
    The FIRST entry is the default variant. Falls back to
    DEFAULT_LLAMA_VERSIONS when the key is missing or no entry is usable."""
    if cfg is None:
        cfg = load_gui_config()
    out: list[tuple[str, str]] = []
    raw = cfg.get("llama_versions")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                script = str(item.get("script") or "").strip()
                if name and script:
                    out.append((name, script))
    return out or list(DEFAULT_LLAMA_VERSIONS)


def load_gui_config() -> dict:
    """Read gui_config.json; tolerate missing/corrupt file."""
    try:
        with open(GUI_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_gui_config(updates: dict):
    """Merge `updates` into gui_config.json (atomic write, best effort)."""
    cfg = load_gui_config()
    cfg.update(updates)
    try:
        tmp = GUI_CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp.replace(GUI_CONFIG_PATH)
    except Exception:
        pass  # never let a settings save crash the app


def _saved_window_size() -> tuple[int, int] | None:
    ws = load_gui_config().get("window_size")
    if (isinstance(ws, (list, tuple)) and len(ws) == 2
            and all(isinstance(x, int) and x > 100 for x in ws)):
        return (ws[0], ws[1])
    return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_yaml(path: Path):
    import yaml

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def raw_dir_for(output_dir: str, testset: str, model: str | None = None) -> Path | None:
    base = ROOT_DIR / output_dir / testset / "raw"
    # Must match the CLI: it stores results under safe_dir_name(model_name).
    d = base / safe_dir_name(model) if model else base
    return d if d.is_dir() else None


def raw_files(raw_dir: Path, question_ids: set[int] | None = None):
    files = []
    for f in raw_dir.glob("q*_run*.json"):
        if question_ids is None:
            files.append(f)
            continue
        m = re.match(r"q(\d+)_run\d+\.json$", f.name)
        if m and int(m.group(1)) in question_ids:
            files.append(f)
    return sorted(files)


def file_status(path: Path) -> str:
    """'pending' | 'done' | 'error' for a raw result file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "pending"
    j = data.get("judgment")
    if j is None:
        return "pending"
    return "error" if j.get("error") else "done"


def count_statuses(raw_dir: Path | None, question_ids: set[int] | None = None):
    counts = {"pending": 0, "done": 0, "error": 0}
    if raw_dir is None:
        return counts
    for f in raw_files(raw_dir, question_ids):
        counts[file_status(f)] += 1
    return counts


def scope_question_ids(config: dict, testset: str, questions_str: str, category_str: str) -> list[int]:
    """Mirror the CLI's scoping: category filter ∩ question-id filter."""
    qs = load_questions(config.get("data_dir", "data"), testset=testset)
    ids = [q["id"] for q in qs]
    if category_str.strip():
        cats = parse_category_spec(category_str, qs)
        ids = [q["id"] for q in qs if q["category"] in cats]
    if questions_str.strip():
        wanted = set(parse_question_range(questions_str))
        ids = [i for i in ids if i in wanted]
    return ids


# --------------------------------------------------------------------------- #
# Subprocess management
# --------------------------------------------------------------------------- #
class Job:
    """One CLI invocation: subprocess + reader thread + output queue."""

    def __init__(self, args: list[str]):
        self.args = args
        self.q: "queue.Queue" = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.rc: int | None = None
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        kwargs = dict(
            cwd=BENCH_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "run_benchmark.py", *self.args], **kwargs
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        assert self.proc is not None
        for line in self.proc.stdout:
            self.q.put(line.rstrip("\n"))
        self.proc.wait()
        self.rc = self.proc.returncode
        self.q.put(None)  # EOF sentinel
        self._done.set()

    @property
    def finished(self) -> bool:
        return self.proc is not None and self._done.is_set()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class App:
    def __init__(self):
        sg.theme("DarkBlue4")
        self.config_path = "config.yaml"
        self.job: Job | None = None
        self.job_kind: str = ""          # 'run' | 'judge' | 'other'
        self.job_expected: int = 0       # total tasks at launch
        self.job_model: str | None = None
        self.job_scope: set[int] | None = None
        self.log_lines: list[tuple[str, bool]] = []  # (text, is_error)
        self._server_log_lines: list[tuple[str, bool]] = []
        # dedup: last line written per log target (skip exact consecutive repeats)
        self._last_app_log: str | None = None
        self._last_srv_log: str | None = None
        self._log_tree = None          # Treeview behind the -LOG- table
        self._srv_log_tree = None      # Treeview behind the -SRVLOG- table
        self.selected_model: str | None = None
        self._model_rows: list[list] = []  # mirror of the -MODELS- table rows
        self.server: dict | None = None    # live llama server process

        # model chosen from the local server (gui_config.json "local_model_id")
        self.local_model: str | None = load_gui_config().get("local_model_id") or None
        self._discovery_q: "queue.Queue" = queue.Queue()

        self.cfg = self._load_cfg_state()
        self.layout = self._build_layout()
        saved_size = _saved_window_size()
        self.win = sg.Window("BrainBench — AI Brainteaser Benchmark", self.layout,
                            finalize=True, resizable=True,
                            **({"size": saved_size} if saved_size else {}))
        self.e = self.win
        # keep gui_config.json up to date while the user resizes the window
        self._last_saved_size = saved_size
        self._last_size_save = 0.0
        self._last_cfg_size: tuple[int, int] | None = None
        self.e.TKroot.bind("<Configure>", self._on_configure, add=True)
        # log trees: red error tag on the Treeview behind each log table
        self._log_tree = self.e["-LOG-"].TKTreeview
        self._log_tree.tag_configure("logerr", foreground="#ff6b6b")
        self._srv_log_tree = self.e["-SRVLOG-"].TKTreeview
        self._srv_log_tree.tag_configure("logerr", foreground="#ff6b6b")
        # right-click context menu on the models table: remember which row is
        # under the cursor when the user right-clicks (the built-in menu does
        # not report the clicked row)
        self._rcm_row: int | None = None
        self.e["-MODELS-"].TKTreeview.bind("<Button-3>", self._on_models_rcm_press)
        if self.local_model:
            self.e["-LLMALIST-"].update(values=[self.local_model])
        # mirror the config.yaml reasoning budget into the combo
        self._refresh_rbtokens()
        # startup check: does the local server respond? (result via the queue)
        self._discover_models()

    def _on_configure(self, _event=None):
        self._sync_window_size()
        # The table updates inside the refresh re-trigger <Configure> on the
        # root WITHOUT a real size change; without this guard the refresh
        # feedback loop spins ~20/s and floods the log. React only to real
        # window resizes.
        size = (self.e.TKroot.winfo_width(), self.e.TKroot.winfo_height())
        if size == self._last_cfg_size:
            return
        self._last_cfg_size = size
        self._refresh_models(keep=self.e["-MODEL-"].Get())
        self._refresh_results()

    # ------------------------------ config ------------------------------- #
    def _cfg_file(self) -> Path:
        p = Path(self.e["-CFG-"].Get().strip() or "config.yaml")
        if not p.is_absolute():
            p = BENCH_DIR / p
        return p

    def _load_cfg_state(self) -> dict:
        return {}

    def _read_config(self):
        """Return (config_dict, model_names, error)."""
        try:
            cfg = load_config(str(self._cfg_file()))
        except Exception as e:
            return None, [], str(e)
        models = [m.name for m in parse_model_configs(cfg) if getattr(m, "enabled", True)]
        return cfg, models, None

    def _refresh_models(self, keep: str | None = None):
        cfg, models, err = self._read_config()
        # show the selected server model in place of the generic name
        if self.local_model and LOCAL_MODEL_NAME in models:
            models = [self.local_model if m == LOCAL_MODEL_NAME else m
                      for m in models]
        self.e["-MODEL-"].update(values=models or ["<unavailable>"])
        if keep and keep in models:
            self.e["-MODEL-"].update(value=keep)
        elif models:
            self.e["-MODEL-"].update(value=models[0])
        if err:
            self.e["-STATUS-"].update(f"Config error: {err}")

    def _selected(self):
        return {
            "model": self.e["-MODEL-"].Get(),
            "testset": self.e["-TESTSET-"].Get(),
            "questions": self.e["-QUESTIONS-"].Get().strip(),
            "category": self.e["-CATEGORY-"].Get().strip(),
            "runs": self.e["-RUNS-"].Get().strip(),
            "resume": self.e["-RESUME-"].Get(),
            "no_judge": self.e["-NOJUDGE-"].Get(),
        }

    def _save_run_settings(self):
        """Persist Questions/Category/Runs/resume/no-judge to gui_config.json."""
        try:
            save_gui_config({
                "questions": self.e["-QUESTIONS-"].Get().strip(),
                "category": self.e["-CATEGORY-"].Get().strip(),
                "runs": self.e["-RUNS-"].Get().strip(),
                "resume": bool(self.e["-RESUME-"].Get()),
                "no_judge": bool(self.e["-NOJUDGE-"].Get()),
            })
        except Exception:
            pass  # best effort, never block the UI on a settings save

    # ------------------------------- layout ------------------------------- #
    def _build_layout(self):
        # last-used run options (persisted in gui_config.json)
        gcfg = load_gui_config()
        ts_row = sg.Col([
            [sg.Text("Testset:  ", size=(10, 1), justification="r"),
             sg.Combo(list(TESTSETS.keys()), key="-TESTSET-", size=(14, 1),
                      default_value=DEFAULT_TESTSET, readonly=True),
             sg.Text("   Config:", size=(8, 1)),
             sg.Input("config.yaml", key="-CFG-", size=(28, 1),
                      tooltip="Relative to benchmark/ or absolute"),
             sg.Button("Reload", key="-RELOADCFG-")],
        ], pad=(4, 2))

        sel_row = sg.Col([
            [sg.Text("Model:", size=(8, 1)),
             sg.Combo([], key="-MODEL-", size=(26, 1), readonly=True,
                      tooltip="Enabled models from the config file"),
             sg.Text("Questions:", size=(10, 1)),
             sg.Input(gcfg.get("questions", ""), key="-QUESTIONS-", size=(18, 1),
                      tooltip="e.g. 1-10, 42, 99-100  (empty = all)"),
             sg.Text("Category:", size=(8, 1)),
             sg.Input(gcfg.get("category", ""), key="-CATEGORY-", size=(18, 1),
                      tooltip="e.g. 8 or 'physics' (empty = all)")],
            [sg.Text("Runs:", size=(8, 1)),
             sg.Input(gcfg.get("runs", ""), key="-RUNS-", size=(6, 1),
                      tooltip="override runs per question (empty = config default)"),
             sg.Checkbox("resume (skip completed)", key="-RESUME-",
                         default=gcfg.get("resume", True)),
             sg.Checkbox("no-judge (responses only, judge later)",
                         key="-NOJUDGE-", default=gcfg.get("no_judge", False))],
        ], pad=(4, 2))

        llama_folder = gcfg.get("llama_folder") or DEFAULT_LLAMA_FOLDER
        # variant list (name -> script) from gui_config.json; the FIRST
        # entry is the default variant
        self._llama_versions = load_llama_versions(gcfg)
        llama_names = [n for n, _ in self._llama_versions]
        llama_version = gcfg.get("llama_version") or llama_names[0]
        if llama_version not in llama_names:
            llama_version = llama_names[0]
        # trusted mirror of the combo selection (the widget's Get() is not
        # reliable late in the session: a dead/empty widget silently falls
        # back to the first variant)
        self._llama_ver_sel = llama_version
        llama_row = sg.Col([
            [sg.Text("Llama server:", size=(12, 1)),
             sg.Combo(llama_names, key="-LLAMAVER-", size=(13, 1),
                      default_value=llama_version, readonly=True,
                      enable_events=True,
                      tooltip="Server variant (name -> launch script mapping in "
                              "gui_config.json 'llama_versions')"),
             sg.Input(llama_folder, key="-LLAMAFOLDER-", size=(36, 1),
                      tooltip="Folder containing the run_srv_*.cmd scripts"),
             sg.Button("Browse", key="-LLAMABROWSE-"),
             sg.Button("▶ Run server", key="-LLAMARUN-", button_color="white on #2e7d32"),
             sg.Text("stopped", key="-LLAMASTATUS-", size=(26, 1),
                     tooltip="State of the local llama server")],
            [sg.Text("Models:", size=(12, 1)),
             sg.Listbox(values=[], key="-LLMALIST-", size=(48, 4),
                        change_submits=True,
                        tooltip="Models served by the local llama-server "
                                "(/v1/models). Select one to use it for the "
                                "benchmark — it replaces the generic 'local-llama'."),
             sg.Button("⟳ Discover", key="-LLMDISC-",
                       tooltip="GET /v1/models on the local server")],
            [sg.Text("Reasoning budget:", size=(18, 1)),
             sg.Combo([str(v) for v in REASONING_BUDGETS], key="-RBTOKENS-",
                      size=(10, 1), readonly=True, enable_events=True,
                      tooltip="reasoning_budget_tokens of the local-llama "
                              "model in config.yaml (cap on the thinking "
                              "phase). The selected value is saved to the "
                              "config file immediately.")],
        ], pad=(4, 2))

        # current judge model: config.yaml is the source of truth
        self._judge_model = ""
        try:
            jc = load_config(str(BENCH_DIR / "config.yaml")).get("judge") or {}
            self._judge_model = str(jc.get("model_id") or "").strip()
        except Exception:
            pass

        btns = sg.Col([
            [sg.Button("Run benchmark", key="-RUN-", button_color="white on #2e7d32"),
             sg.Button("Judge only", key="-JUDGE-", tooltip="Judge pending responses (judge model must be loaded)"),
             sg.Button("Re-judge all", key="-REJUDGE-", tooltip="--judge-only --re-judge"),
             sg.Button("Check", key="-CHECK-", tooltip="Completeness check only"),
             sg.Button("Aggregate", key="-AGG-", tooltip="Re-aggregate scores.json only"),
             sg.Button("Stop", key="-STOP-", disabled=True, button_color="white on #b71c1c"),
             sg.Text("Judge Model:", size=(66, 1), justification="r"),
             sg.Combo([], key="-JUDGEMODEL-", size=(36, 1),
                      default_value=self._judge_model, readonly=True,
                      enable_events=True,
                      tooltip="Judge model (llama-server). The selection is saved "
                              "to judge.model_id in config.yaml immediately. "
                              "Independent from the model under test.")],
        ], pad=(4, 2))

        prog = sg.Col([
            [sg.ProgressBar(100, orientation="h", size=(420, 18), key="-PROG-"),
             sg.Text("Idle", key="-STATUS-", size=(60, 1))],
        ], pad=(4, 2))

        # Fixed-width columns (no expand_x): Model widened ~40% vs the old
        # 16-char cap, numeric columns halved. The freed space is given to
        # the category table below (its Col expands instead of this table).
        # FreeSimpleGUI 5.0.0 right_click_menu quirk: the outer list's slot 0
        # is IGNORED and the items are read from slot 1 (menu[1]), so the
        # first entry is a placeholder. Selecting the item fires the event
        # with the item's exact string.
        table_models = sg.Table(
            values=[], headings=["Model", "Corr", "Tot", "Acc %",
                                 "Rel %", "Pend", "In tok", "Out tok"],
            col_widths=[40, 5, 5, 6, 6, 5, 7, 7],
            auto_size_columns=False,
            # FreeSimpleGUI 5.0.0 default is justification='right' (all cols);
            # keep text col left, align numbers right.
            cols_justification=["l", "r", "r", "r", "r", "r", "r", "r"],
            right_click_menu=["-", ["Refresh details for this model",
                                      "Delete results for this model"]],
            key="-MODELS-", num_rows=6,
            row_height=22, select_mode=sg.TABLE_SELECT_MODE_BROWSE,
            enable_click_events=True,  # left-click fires ("-MODELS-","+CLICKED+",(row,col))
            expand_y=True)

        detail = sg.Col([
            [sg.Table(values=[], headings=["Category", "Corr", "Tot", "Acc %"],
                      key="-CATS-", num_rows=6, col_widths=[30, 8, 8, 8],
                      auto_size_columns=False,
                      cols_justification=["l", "r", "r", "r"],
                      row_height=20, expand_x=True, expand_y=True,
                      tooltip="Per-category breakdown of the selected model")],
        ], expand_x=True, pad=(0, 2))

        log_app = sg.Table(values=[], headings=["App log"], key="-LOG-",
                          num_rows=9, row_height=17, expand_x=True,
                          font=("Consola", 9),
                          background_color="black", text_color="white",
                          justification="left",
                          tooltip="Application log — errors in red")
        log_srv = sg.Table(values=[], headings=["Server log"], key="-SRVLOG-",
                          num_rows=9, row_height=17, expand_x=True,
                          font=("Consola", 9),
                          background_color="black", text_color="white",
                          justification="left",
                          tooltip="llama-server console output — errors in red")
        # NOTE: in FreeSimpleGUI 5.x the tab container is sg.TabGroup (there is
        # no sg.Tabs). Like any container, Tab and TabGroup take a layout of
        # ROWS (lists), so the tables must be double-nested: [[table]].
        log = sg.TabGroup([[sg.Tab("App log", [[log_app]]),
                            sg.Tab("Server log", [[log_srv]])]],
                          key="-LOGTABS-", expand_x=True)

        layout = [[sg.Text("BrainBench", font=("Helvetica", 14, "bold")),
                   sg.Button("Refresh results", key="-REFRESH-"),
                   sg.Button("Open results folder", key="-OPENDIR-")],
                  [ts_row], [llama_row], [sel_row], [btns], [prog],
                  [table_models, detail],
                  [log]]
        return layout

    # ------------------------------ results ------------------------------- #
    def _scores_path(self, testset: str) -> Path:
        cfg, _, _ = self._read_config()
        outdir = (cfg or {}).get("output_dir", "results")
        return ROOT_DIR / outdir / testset / "scores.json"

    def _refresh_results(self):
        testset = self.e["-TESTSET-"].Get() or DEFAULT_TESTSET
        path = self._scores_path(testset)
        rows = []
        pending_info = "no results yet"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    scores = json.load(f)
            except Exception:
                scores = {"models": {}}
            # pending counts per model from raw files
            cfg, _, _ = self._read_config()
            outdir = (cfg or {}).get("output_dir", "results")
            base = ROOT_DIR / outdir / testset / "raw"
            for name, ms in scores.get("models", {}).items():
                rd = base / name
                st = count_statuses(rd if rd.is_dir() else None)
                rows.append([
                    name,
                    ms.get("overall_correct", 0),
                    ms.get("overall_total", 0),
                    f"{100 * ms.get('overall_accuracy', 0):.1f}%",
                    f"{100 * ms.get('reliability', 0):.0f}%",
                    st["pending"] + st["error"],
                    ms.get("total_input_tokens", 0),
                    ms.get("total_output_tokens", 0),
                ])
            rows.sort(key=lambda r: (-float(str(r[3]).rstrip("%") or 0)))
            total_pending = sum(r[5] for r in rows)
            pending_info = f"{total_pending} pending (unjudged/errored) responses"
        self._model_rows = rows
        self.e["-MODELS-"].update(values=rows)
        self.log(pending_info)
        if self.selected_model not in (rows and [str(r[0]) for r in rows] or []):
            # keep selection on a model that still exists (or the first one)
            self.selected_model = str(rows[0][0]) if rows else None
        self._refresh_cats()

    def _refresh_cats(self):
        testset = self.e["-TESTSET-"].Get() or DEFAULT_TESTSET
        model = self.selected_model
        cats = []
        if model:
            path = self._scores_path(testset)
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    scores = json.load(f)
                ms = scores.get("models", {}).get(str(model), {})
                for cat, cd in (ms.get("by_category") or {}).items():
                    cats.append([cat, cd.get("correct", 0), cd.get("total", 0),
                                 f"{100 * cd.get('accuracy', 0):.0f}%"])
        self.e["-CATS-"].update(values=cats)

    # --------------------- models table right-click menu ------------------- #
    def _on_models_rcm_press(self, event):
        """Right button pressed on the models table: mark the row under the
        cursor as the target of the right-click menu (and highlight it)."""
        tree = self.e["-MODELS-"].TKTreeview
        iid = tree.identify_row(event.y)
        if iid:
            self._rcm_row = int(iid) - 1  # rows are inserted with iid = index+1
            tree.selection_set(iid)
            tree.see(iid)
        else:
            self._rcm_row = None

    def _rcm_target_row(self):
        """Index of the models-table row the right-click menu was opened on
        (the row under the cursor), falling back to the current selection.
        Returns None if there is no valid target row."""
        i = self._rcm_row
        if i is None:  # fall back to the current selection
            sel = self.e["-MODELS-"].Get()
            i = sel[0] if isinstance(sel, list) and sel else (
                sel if isinstance(sel, int) else None)
        if i is None or not (0 <= i < len(self._model_rows)):
            return None
        return i

    def _refresh_model_details(self):
        """Right-click menu: refresh the detail (category) table for the model
        under the cursor by re-reading its data from disk."""
        i = self._rcm_target_row()
        if i is None:
            sg.popup("Select a model row first (left click).")
            return
        self.selected_model = str(self._model_rows[i][0])
        self._refresh_cats()
        self.log(f"[refresh] details for '{self.selected_model}'")

    def _delete_model_results(self):
        """Right-click menu: delete all results of the model under the cursor
        (raw folder for the selected testset + scores.json entry)."""
        rows = self._model_rows
        i = self._rcm_target_row()
        if i is None:
            sg.popup("Select a model row first (left click).")
            return
        name = str(rows[i][0])
        # safety: the row name must be a plain sanitized folder name
        if name in (".", "..") or re.search(r'[\\/:*?"<>|]', name):
            sg.popup_error(f"Invalid model name: {name}")
            return
        cfg, _, _ = self._read_config()
        outdir = (cfg or {}).get("output_dir", "results")
        testset = self.e["-TESTSET-"].Get() or DEFAULT_TESTSET
        raw_dir = ROOT_DIR / outdir / testset / "raw" / name
        scores_path = ROOT_DIR / outdir / testset / "scores.json"
        scores: dict = {}
        if scores_path.exists():
            try:
                scores = json.loads(scores_path.read_text(encoding="utf-8"))
            except Exception:
                scores = {}
        in_scores = name in (scores.get("models") or {})
        if not raw_dir.is_dir() and not in_scores:
            sg.popup(f"No results found for:\n{name}")
            return
        if not sg.popup_yes_no(
                f"Delete all results of:\n\n{name}\n\n"
                "Raw response files and the scores.json entry "
                "will be removed.",
                title="Delete results"):
            return
        if raw_dir.is_dir():
            shutil.rmtree(raw_dir)
        if in_scores:
            del scores["models"][name]
            tmp = scores_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(scores, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            tmp.replace(scores_path)
        self.selected_model = None
        self._refresh_results()
        self.log(f"[delete] results of '{name}' removed")

    # ------------------------------ launching ------------------------------ #
    def _start(self, args: list[str], kind: str, model: str | None,
               scope: set[int] | None, expected: int):
        if self.job and not self.job.finished:
            sg.popup_error("A job is already running. Stop it first.")
            return
        self.job_kind = kind
        self.job_model = model
        self.job_scope = scope
        self.job_expected = expected
        self.job = Job(args)
        self.job.start()
        self._set_running(True)
        self.log(f"$ python run_benchmark.py {' '.join(args)}")

    def _set_running(self, running: bool):
        self.e["-RUN-"].update(disabled=running)
        self.e["-JUDGE-"].update(disabled=running)
        self.e["-REJUDGE-"].update(disabled=running)
        self.e["-CHECK-"].update(disabled=running)
        self.e["-AGG-"].update(disabled=running)
        self.e["-STOP-"].update(disabled=not running)

    # ----------------------------- llama server --------------------------- #
    def _llama_folder(self) -> str:
        return (self.e["-LLAMAFOLDER-"].Get() or "").strip() or DEFAULT_LLAMA_FOLDER

    def _llama_version(self) -> str:
        # mirror kept in Python, NOT the live widget: Get() on a dead/empty
        # widget would silently fall back to the first variant
        return self._llama_ver_sel

    def _llama_script_name(self, version: str) -> str:
        for name, script in self._llama_versions:
            if name == version:
                return script
        return f"run_srv_{version}_p8080.cmd"  # last-resort convention

    def _save_llama_settings(self):
        save_gui_config({"llama_folder": self._llama_folder(),
                         "llama_version": self._llama_version()})

    def _set_llama_ui(self, running: bool):
        btn = self.e["-LLAMARUN-"]
        if running:
            btn.update(text="■ Stop server", button_color=("white", "#b71c1c"))
            self.e["-LLAMASTATUS-"].update(value=f"running ({self._llama_version()})")
        else:
            btn.update(text="▶ Run server", button_color=("white", "#2e7d32"))
            self.e["-LLAMASTATUS-"].update(value="stopped")

    def _llama_kill_tree(self, proc: subprocess.Popen):
        """Kill cmd.exe AND its llama-server child (taskkill /T)."""
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)

    def _start_llama(self):
        version = self._llama_version()
        folder = self._llama_folder()
        script = llama_script_path(folder, self._llama_script_name(version))
        if not Path(folder).is_dir():
            sg.popup_error(f"Folder not found:\n{folder}")
            return
        if not script.is_file():
            sg.popup_error(f"Script not found:\n{script}")
            return
        # Run a temp copy WITHOUT the trailing `pause`: in a hidden console no
        # one could answer it, so a crashed server would look "running" forever.
        tmp = Path(tempfile.gettempdir()) / f"brainbench_{version}_srv.cmd"
        raw = script.read_bytes()
        body = b"\r\n".join(
            l for l in raw.splitlines(keepends=False)
            if l.strip().lower().removeprefix(b"@").strip() != b"pause")
        tmp.write_bytes(body)
        self._save_llama_settings()
        kwargs: dict = dict(cwd=str(Path(folder).resolve()),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, errors="replace")
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(f'"{tmp}"', **kwargs)
        except Exception as ex:
            sg.popup_error(f"Failed to start the llama server:\n{ex}")
            return
        q: "queue.Queue" = queue.Queue()
        self.server = {"proc": proc, "q": q, "tmp": tmp}
        threading.Thread(target=_pump_lines, args=(proc, q), daemon=True).start()
        self._set_llama_ui(True)
        self.log(f"[llama] starting '{version}' server: {script.name}")

    def _stop_llama(self):
        s = self.server
        if s is None:
            return
        self._llama_kill_tree(s["proc"])
        try:
            s["tmp"].unlink(missing_ok=True)
        except Exception:
            pass
        self.server = None
        self._set_llama_ui(False)
        self.log("[llama] server stopped")

    def _drain_server(self) -> bool:
        """Pull server output lines into the shared log pane."""
        s = self.server
        if s is None:
            return False
        added = False
        while True:
            try:
                line = s["q"].get_nowait()
            except queue.Empty:
                break
            if line is None:
                # the script exited on its own (crash or clean exit)
                self.log(f"[llama] server process exited (code {s['proc'].wait()})")
                try:
                    s["tmp"].unlink(missing_ok=True)
                except Exception:
                    pass
                self.server = None
                self._set_llama_ui(False)
                continue
            self.log(line, server=True)
            added = True
        return added

    # --------------------------- reasoning budget --------------------------- #
    def _local_reasoning_budget(self):
        """Current local-llama reasoning_budget_tokens in the config file."""
        cfg = load_yaml(self._cfg_file())
        for m in cfg.get("models") or []:
            if isinstance(m, dict) and m.get("name") == LOCAL_MODEL_NAME:
                v = m.get("reasoning_budget_tokens")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return int(v)
        return None

    def _refresh_rbtokens(self):
        """Mirror the config.yaml value into the combo selection."""
        try:
            cur = self._local_reasoning_budget()
        except Exception:
            cur = None
        if cur in REASONING_BUDGETS:
            self.e["-RBTOKENS-"].update(value=str(cur))

    def _set_local_reasoning_budget(self, value: int) -> bool:
        """Set local-llama's reasoning_budget_tokens in the config file.

        Targeted line edit (no yaml round-trip) so comments and layout are
        preserved; only the local-llama block is touched, never the judge's
        budget. Returns True on success."""
        path = self._cfg_file()
        try:
            raw = path.read_bytes()
        except Exception as e:
            sg.popup_error(f"Cannot read {path.name}:\n{e}")
            return False
        # raw bytes: read_text would normalize CRLF to LF (universal newlines)
        nl = "\r\n" if b"\r\n" in raw else "\n"
        lines = raw.decode("utf-8").splitlines()
        start = None
        for i, l in enumerate(lines):
            if re.match(r"^\s*-\s*name:\s*local-llama\b", l):
                start = i
                break
        if start is None:
            sg.popup_error("Il modello 'local-llama' non è in config.yaml.")
            return False
        # block end: next model entry or next top-level key
        end = len(lines)
        for j in range(start + 1, len(lines)):
            l = lines[j]
            if re.match(r"^\s*-\s*name:\s*\S", l) or (
                    l.strip() and l[0] not in " \t#"):
                end = j
                break
        for j in range(start + 1, end):
            m = re.match(r"^(\s*)reasoning_budget_tokens:", lines[j])
            if m:
                rest = lines[j][m.end():].strip()
                comment = ""
                if rest:
                    cm = re.match(r"^(.*?)\s*(#.*)$", rest)
                    if cm:
                        comment = f"  {cm.group(2).strip()}"
                lines[j] = (f"{m.group(1)}reasoning_budget_tokens: {value}"
                            f"{comment}")
                break
        else:
            # key missing in the block: insert it (after base_url if present)
            indent, ins = "    ", start + 1
            for j in range(start + 1, end):
                m = re.match(r"^(\s*)base_url:", lines[j])
                if m:
                    indent, ins = m.group(1), j + 1
                    break
            else:
                m = re.match(r"^(\s*)-\s*name:", lines[start])
                indent = m.group(1) + "  " if m else "    "
            lines.insert(ins, f"{indent}reasoning_budget_tokens: {value}")
        tmp = path.with_name(path.name + ".tmp")
        # write_bytes: write_text (newline=None) would turn every LF into
        # CRLF on Windows and rewrite the whole file's line endings
        tmp.write_bytes((nl.join(lines) + nl).encode("utf-8"))
        tmp.replace(path)
        return True

    def _set_judge_model(self, value: str) -> bool:
        """Set the judge's model_id in the config file.

        Targeted line edit (no yaml round-trip) so comments and layout are
        preserved; only the top-level 'judge:' block is touched, never the
        model_id of the model entries (they carry a dash). Returns True on
        success."""
        path = self._cfg_file()
        try:
            raw = path.read_bytes()
        except Exception as e:
            sg.popup_error(f"Cannot read {path.name}:\n{e}")
            return False
        # raw bytes: read_text would normalize CRLF to LF (universal newlines)
        nl = "\r\n" if b"\r\n" in raw else "\n"
        lines = raw.decode("utf-8").splitlines()
        start = None
        for i, l in enumerate(lines):
            if re.match(r"^judge\s*:", l):
                start = i
                break
        if start is None:
            sg.popup_error("Il blocco 'judge:' non è in config.yaml.")
            return False
        # block end: next top-level key
        end = len(lines)
        for j in range(start + 1, len(lines)):
            l = lines[j]
            if l.strip() and l[0] not in " \t#":
                end = j
                break
        for j in range(start + 1, end):
            m = re.match(r"^(\s*)model_id:", lines[j])
            if m:
                rest = lines[j][m.end():].strip()
                comment = ""
                if rest:
                    cm = re.match(r"^(.*?)\s*(#.*)$", rest)
                    if cm:
                        comment = f"  {cm.group(2).strip()}"
                lines[j] = f"{m.group(1)}model_id: {value}{comment}"
                break
        else:
            # key missing in the block: insert it right after 'judge:'
            indent = "  "
            for j in range(start + 1, end):
                mm = re.match(r"^(\s*)\S", lines[j])
                if mm:
                    indent = mm.group(1)
                    break
            lines.insert(start + 1, f"{indent}model_id: {value}")
        tmp = path.with_name(path.name + ".tmp")
        # write_bytes: write_text (newline=None) would turn every LF into
        # CRLF on Windows and rewrite the whole file's line endings
        tmp.write_bytes((nl.join(lines) + nl).encode("utf-8"))
        tmp.replace(path)
        return True

    def _on_judgemodel_selection(self):
        sel = str(self.e["-JUDGEMODEL-"].Get() or "").strip()
        if not sel or sel == self._judge_model:
            return
        if self._set_judge_model(sel):
            cfg, _, _ = self._read_config()
            if (cfg or {}).get("judge", {}).get("model_id") != sel:
                self.log(f"Judge model: save di '{sel}' NON verificato in "
                         f"config.yaml", error=True)
                return
            self._judge_model = sel
            self.log(f"Judge model: {sel} (salvato)")

    def _on_rbtokens_selection(self):
        raw = self.e["-RBTOKENS-"].Get()
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return
        if value not in REASONING_BUDGETS:
            return
        if self._set_local_reasoning_budget(value):
            self.log(f"Reasoning budget (local-llama): {value} token "
                     f"→ config.yaml")

    # ---------------------------- model discovery --------------------------- #
    def _local_base_url(self) -> str:
        cfg, _, _ = self._read_config()
        if cfg:
            for m in parse_model_configs(cfg):
                if m.name == LOCAL_MODEL_NAME and m.base_url:
                    return m.base_url.rstrip("/")
        return DEFAULT_LOCAL_BASE_URL

    def _discover_models(self):
        """GET /v1/models on the local server (worker thread → queue)."""
        url = self._local_base_url() + "/models"

        def worker():
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    data = json.load(r)
                ids = [m["id"] for m in data.get("data", [])
                       if isinstance(m, dict) and m.get("id")]
                self._discovery_q.put(("ok", ids, None))
            except Exception as e:
                self._discovery_q.put(("err", None, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_discovery(self):
        """Apply discovery results to the UI (called from the main loop)."""
        while True:
            try:
                kind, ids, err = self._discovery_q.get_nowait()
            except queue.Empty:
                break
            if kind == "ok":
                ids = ids or []
                if ids:
                    self.log(f"Server OK — {len(ids)} model(s) available on "
                             f"{self._local_base_url()}")
                else:
                    self.log(f"Server OK but no models loaded on "
                             f"{self._local_base_url()}")
                vals = ids if ids else ([self.local_model] if self.local_model
                                        else [])
                self.e["-LLMALIST-"].update(values=vals)
                if self.local_model and self.local_model in ids:
                    self.e["-LLMALIST-"].select_index(ids.index(self.local_model))
                self._refresh_models(keep=self.e["-MODEL-"].Get())
                # keep the judge combo in sync with the server's models
                jcombo = self.e["-JUDGEMODEL-"]
                jcombo.update(values=ids if ids else ([self._judge_model]
                                                       if self._judge_model
                                                       else []))
                if self._judge_model in ids:
                    jcombo.update(value=self._judge_model)
                elif ids:
                    jcombo.update(value=ids[0])
                    self.log(f"Judge model corrente '{self._judge_model}' "
                             f"non è nel server: la combo mostra "
                             f"'{ids[0]}' (niente salvato)")
            else:
                self.log(f"llama-server not reachable at "
                         f"{self._local_base_url()}/models: {err}", error=True)

    def _on_model_list_selection(self):
        idx = self.e["-LLMALIST-"].get_indexes()
        vals = self.e["-LLMALIST-"].get_list_values()
        if not idx or not vals:
            return
        i = idx[0] if isinstance(idx, (list, tuple)) else idx
        if isinstance(i, int) and 0 <= i < len(vals):
            name = str(vals[i])
            if name != self.local_model:
                self.local_model = name
                save_gui_config({"local_model_id": name})
                self._refresh_models(keep=name)
            self.log(f"Model selected: {name}")

    # ------------------------------- logging ------------------------------- #
    def log(self, line: str, error: bool | None = None, server: bool = False):
        """Append a line to the app log or the server log tab (errors red).

        With error=None the style is auto-detected from the text; with
        server=True the line goes to the "Server log" tab. Every line is
        also appended immediately to bench_app.log.txt / bench_server.log.txt
        in the project root (for debugging)."""
        if TQDM_LINE.search(line):
            return
        # The server can emit extra trailing newlines (blank lines); strip
        # them upstream so blanks show neither on screen nor in the file.
        line = line.rstrip("\r\n")
        if not line.strip():
            return  # pure-blank (spaces/tabs too), same as a blank line
        last = self._last_srv_log if server else self._last_app_log
        if line == last:
            return  # exact duplicate of the previous line: skip
        if error is None:
            error = bool(ERR_RE.search(line))
        if server:
            self._last_srv_log = line
        else:
            self._last_app_log = line
        self._log_to_file(line, server)
        lines = self._server_log_lines if server else self.log_lines
        lines.append((line, error))
        if len(lines) > LOG_MAX_LINES:
            del lines[:-LOG_MAX_LINES]
        self._append_log_line(line, error,
                              self._srv_log_tree if server else self._log_tree)

    @staticmethod
    def _log_to_file(line: str, server: bool):
        """Append a line to the persistent log file (best effort)."""
        path = SERVER_LOG_PATH if server else APP_LOG_PATH
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass  # never let log persistence break the GUI

    def _append_log_line(self, line: str, error: bool, tree=None):
        if tree is None:
            tree = self._log_tree
        if tree is None:
            return
        item = tree.insert("", "end", values=[line],
                           tags=("logerr" if error else ()))
        children = tree.get_children("")
        if len(children) > LOG_MAX_LINES:
            for c in children[: len(children) - LOG_MAX_LINES]:
                tree.delete(c)
        tree.see(item)

    def _drain_log(self) -> bool:
        """Pull queued CLI output lines. Returns True if new lines arrived."""
        if not self.job:
            return False
        added = False
        while True:
            try:
                line = self.job.q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                continue
            self.log(line)
            added = True
        return added

    # ------------------------------ progress ------------------------------- #
    def _poll_progress(self):
        if not self.job or self.job.finished or self.job_expected <= 0:
            return
        done = 0
        if self.job_kind == "run":
            cfg, _, _ = self._read_config()
            outdir = (cfg or {}).get("output_dir", "results")
            testset = self.e["-TESTSET-"].Get()
            rd = raw_dir_for(outdir, testset, self.job_model)
            st = count_statuses(rd, self.job_scope)
            done = st["done"] + st["pending"]  # saved responses count as done tasks
        elif self.job_kind == "judge":
            cfg, _, _ = self._read_config()
            outdir = (cfg or {}).get("output_dir", "results")
            testset = self.e["-TESTSET-"].Get()
            rd = raw_dir_for(outdir, testset, self.job_model)
            st = count_statuses(rd, self.job_scope)
            done = st["done"]  # judged (non-error) responses
        total = self.job_expected
        frac = min(1.0, done / total) if total else 0
        self.e["-PROG-"].update(current_count=int(frac * 100), max=100)
        self.e["-STATUS-"].update(f"{done}/{total} tasks   ")

    # ------------------------------- events ------------------------------- #
    def _collect_scope(self, s: dict) -> set[int] | None:
        cfg, _, err = self._read_config()
        if err or not cfg:
            raise RuntimeError(err or "config error")
        try:
            ids = scope_question_ids(cfg, s["testset"], s["questions"], s["category"])
        except Exception as e:
            raise RuntimeError(f"bad questions/category spec: {e}") from e
        if not ids:
            raise RuntimeError("no questions match the given filters")
        return set(ids)

    def _cfg_args(self) -> list[str]:
        """--config for the subprocess (default config.yaml is implied)."""
        p = Path(self.e["-CFG-"].Get().strip() or "config.yaml")
        if not p.is_absolute() and p.name == "config.yaml":
            return []
        return ["--config", str(p)]

    def _model_cli_args(self, display: str) -> list[str]:
        """CLI args for the model picked in the combo.

        The local model is shown under the id discovered from the server
        (e.g. 'ManniX-ITA/…'), but the CLI knows it as 'local-llama': pass
        the config name plus --model-name (results folder + API model id)."""
        if display and display != LOCAL_MODEL_NAME and display == self.local_model:
            return ["--model", LOCAL_MODEL_NAME, "--model-name", display]
        return ["--model", display]

    def run_cmd(self):
        s = self._selected()
        args = ["--testset", s["testset"]] + self._model_cli_args(s["model"])
        args += self._cfg_args()
        if s["questions"]:
            args += ["--questions", s["questions"]]
        if s["category"]:
            args += ["--category", s["category"]]
        if s["runs"]:
            args += ["--runs", s["runs"]]
        if not s["resume"]:
            args.append("--no-resume")
        if s["no_judge"]:
            args.append("--no-judge")
        scope = self._collect_scope(s)
        n = len(scope) * (int(s["runs"]) if s["runs"] else self._default_runs())
        self._start(args, "run", s["model"], scope, n)

    def _default_runs(self) -> int:
        cfg, _, _ = self._read_config()
        return int((cfg or {}).get("runs_per_question", 3))

    def judge_cmd(self, re_judge: bool):
        s = self._selected()
        args = ["--testset", s["testset"], "--judge-only"]
        args += self._cfg_args()
        if re_judge:
            args.append("--re-judge")
        if s["model"]:
            args += self._model_cli_args(s["model"])
        # estimate pending total
        cfg, _, _ = self._read_config()
        outdir = (cfg or {}).get("output_dir", "results")
        rd = raw_dir_for(outdir, s["testset"], s["model"])
        st = count_statuses(rd)
        expected = st["pending"] + (st["done"] if re_judge else 0)
        if expected == 0:
            sg.popup("Nothing to judge (no pending responses).")
            return
        self._start(args, "judge", s["model"], None, expected)

    def check_cmd(self):
        s = self._selected()
        args = ["--testset", s["testset"], "--check"]
        args += self._cfg_args()
        if s["category"]:
            args += ["--category", s["category"]]
        self._start(args, "other", None, None, 0)

    def agg_cmd(self):
        s = self._selected()
        args = ["--testset", s["testset"], "--aggregate-only"]
        args += self._cfg_args()
        self._start(args, "other", None, None, 0)

    # ------------------------------ persistence ---------------------------- #
    def _sync_window_size(self, force: bool = False):
        """Persist the current window size to gui_config.json (throttled).

        Called from the <Configure> tkinter event so the file is ALWAYS up
to date, not just at close time: FreeSimpleGUI destroys the tkinter
        window the instant "X" is clicked, so the size can no longer be
        read when the WIN_CLOSED event finally reaches the read loop."""
        try:
            w, h = self.win.size
            if not (w and h):
                return
            size = (int(w), int(h))
        except Exception:
            return
        if size == self._last_saved_size and not force:
            return
        self._last_saved_size = size  # remember even if the write is throttled
        now = time.time()
        if not force and now - self._last_size_save < 0.5:
            return  # throttled; a later event (or close) will persist it
        self._last_size_save = now
        save_gui_config({"window_size": list(size)})

    def _save_window_state(self):
        """Close-time fallback: re-persist the last known window size."""
        size = self._last_saved_size
        if not size:
            try:
                w, h = self.win.size
                if w and h:
                    size = (int(w), int(h))
            except Exception:
                return
        if size:
            save_gui_config({"window_size": [size[0], size[1]]})

    # ------------------------------- main loop ----------------------------- #
    def run(self):
        last_poll = 0.0
        last_res_refresh = 0.0
        while True:
            # wake up often while the llama server runs, so its log streams
            server_alive = bool(self.server) and self.server["proc"].poll() is None
            event, values = self.e.read(timeout=250 if server_alive else 120)
            now = time.time()

            self._drain_server()
            self._drain_discovery()

            job = self.job
            if job:
                added = self._drain_log()
                if job.finished:
                    rc = job.rc
                    self.log(f"[exit code {rc}]", error=(rc != 0))
                    self.e["-PROG-"].update(current_count=100 if rc == 0 else 0, max=100)
                    self.e["-STATUS-"].update(
                        f"finished (exit {rc})   " if rc == 0 else f"FAILED (exit {rc})   ")
                    self._set_running(False)
                    self._refresh_results()
                    if rc == 0 and self.job_kind in ("run", "judge"):
                        self.log("Scores updated. Review the tables above.")
                    # one-shot handling: clear the finished job so the next
                    # loop iterations don't re-log/refresh for it
                    self.job = None
                else:
                    if now - last_poll > 350:
                        self._poll_progress()
                        last_poll = now
                    if added and now - last_res_refresh > 3.0:
                        self._refresh_results()
                        last_res_refresh = now

            if event == sg.WIN_CLOSED or event == "-EXIT-":
                self._save_llama_settings()
                self._save_run_settings()
                if self.server:
                    self._llama_kill_tree(self.server["proc"])
                self._save_window_state()
                if self.job and not self.job.finished:
                    self.job.stop()
                self.win.close()
                return
            if event == "-RUN-":
                try:
                    self._save_run_settings()
                    self.run_cmd()
                except Exception as ex:
                    sg.popup_error(str(ex))
            elif event == "-JUDGE-":
                self.judge_cmd(re_judge=False)
            elif event == "-REJUDGE-":
                self.judge_cmd(re_judge=True)
            elif event == "-CHECK-":
                try:
                    self.check_cmd()
                except Exception as ex:
                    sg.popup_error(str(ex))
            elif event == "-AGG-":
                self.agg_cmd()
            elif event == "-STOP-":
                if self.job and not self.job.finished:
                    self.job.stop()
                    self.log("Stop requested…")
            elif event == "-REFRESH-":
                self._refresh_results()
            elif event == "Delete results for this model":
                self._delete_model_results()
            elif event == "Refresh details for this model":
                self._refresh_model_details()
            elif event == "-RELOADCFG-":
                self._refresh_models(keep=self.e["-MODEL-"].Get())
                self._refresh_rbtokens()
                cfg, _, _ = self._read_config()
                jm = str(((cfg or {}).get("judge") or {}).get("model_id")
                         or "").strip()
                if jm and jm != self._judge_model:
                    self._judge_model = jm
                    self.e["-JUDGEMODEL-"].update(value=jm)
            elif event == "-OPENDIR-":
                cfg, _, _ = self._read_config()
                outdir = (cfg or {}).get("output_dir", "results")
                p = ROOT_DIR / outdir
                p.mkdir(parents=True, exist_ok=True)
                try:
                    import os
                    os.startfile(str(p))  # type: ignore[attr-defined]
                except Exception:
                    pass
            elif event == "-LLAMAVER-":
                # combo selection: persist into the Python mirror (the widget
                # is alive here, so Get() is trustworthy) and save immediately
                self._llama_ver_sel = self.e["-LLAMAVER-"].Get() or self._llama_ver_sel
                self._save_llama_settings()
                self.log(f"Llama server: {self._llama_ver_sel} (salvato)")
            elif event == "-LLAMABROWSE-":
                folder = sg.popup_get_folder("Select the llama.cpp folder",
                                             initial_folder=self._llama_folder() or None)
                if folder:
                    self.e["-LLAMAFOLDER-"].update(value=folder)
                    self._save_llama_settings()
            elif event == "-LLAMARUN-":
                try:
                    if self.server and self.server["proc"].poll() is None:
                        self._stop_llama()
                    else:
                        if self.server:  # stale entry: process died on its own
                            self.server = None
                            self._set_llama_ui(False)
                        self._start_llama()
                except Exception as ex:
                    sg.popup_error(str(ex))
            elif event == "-LLMDISC-":
                self.log(f"Querying {self._local_base_url()}/models …")
                self._discover_models()
            elif event == "-LLMALIST-":
                self._on_model_list_selection()
            elif event == "-RBTOKENS-":
                self._on_rbtokens_selection()
            elif event == "-JUDGEMODEL-":
                self._on_judgemodel_selection()
            elif event == "-TESTSET-":
                self.selected_model = None
                self._refresh_results()
            elif event == "-MODELS-" or (
                    isinstance(event, tuple) and event
                    and event[0] == "-MODELS-"):
                # Left-click on a model row. FSG 5.x emits the tuple
                # (key, '+CLICKED+', (row, col)) as the event -- never the bare
                # key -- so match that form. Prefer the clicked row, then the
                # current selection.
                row = None
                if (isinstance(event, tuple) and len(event) > 2
                        and isinstance(event[2], (list, tuple))):
                    row = event[2][0]
                if row is None or row < 0:
                    idx = self.e["-MODELS-"].Get()
                    row = idx[0] if isinstance(idx, list) and idx else (
                        idx if isinstance(idx, int) else None)
                if row is not None and 0 <= row < len(self._model_rows):
                    self.selected_model = str(self._model_rows[row][0])
                self._refresh_cats()


def main():
    App().run()


if __name__ == "__main__":
    main()
