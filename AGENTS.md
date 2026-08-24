# BrainBench — MEMORY per le sessioni future

Aggiornato: 2026-08-23. Questo file è il punto di partenza per una nuova sessione:
leggi questo, poi `README.md` e `benchmark/config.yaml`.

## Cosa è
Benchmark per valutare LLM su "brain teasers" (domande a risposta multipla):
un modello genera la risposta, un *judge* LLM (OpenAI) la giudica, i punteggi
vengono aggregati in `scores.json`.

È una fork di [Lomnus-ai/BrainBench](https://github.com/Lomnus-ai/BrainBench)
(remote `upstream`); home del progetto: https://github.com/dan64/BrainBench-llama.
Aggiunge una GUI (`benchmark/gui.py`, launcher
`run_benchmark_gui.vbs`) e supporto completo ai modelli locali serviti da
`llama-server` (build ggml/turboquant/unsloth). README.md (inglese) la
documenta in cima: sezione fork + configurazione `gui_config.json`.

## Layout
```
benchmark/
  run_benchmark.py   CLI principale (~640 righe)
  models.py          client OpenAI / OpenAI-compatibile (llama.cpp)
  judge.py           judge LLM + _extract_json
  gui.py             GUI FreeSimpleGUI (~970 righe)
  config.yaml        modelli, judge, run settings
  gui_config.json    stato GUI (finestra, llama_folder/versions, opzioni run) — generato
data/                brainteasers.json (v1, 100 domande = 20 categorie × 5) + varianti cinesi
results/<testset>/
  raw/<modello_sanificato>/qNNN_runNN.json   risposte grezze
  scores.json                                       aggregazione
scripts/             analyze_results.py, generate_analysis.py, verify_dataset.py
run_benchmark_gui.vbs  launcher one-click GUI Windows (VBS, console nascosta)
run_benchmark_gui.cmd  wrapper console usato dal .vbs (cd /d + python benchmark\gui.py)
BrainBench-llama_GUI.jpg  screenshot GUI, embedded nel README.md
```

## Flusso dati e CLI (run_benchmark.py)
- `--testset` (v1..v3, v3_chinese; in data/ esiste solo v1), `--model` (nome config),
  `--model-name` (**nome display del GGUF**, distinto dal nome config: serve per
  usare lo stesso server llama.cpp con modelli diversi senza reload. Il server
  ignora `model_id` e risponde a `/v1/models` con il nome reale del GGUF).
- `--questions 1-5`, `--category N|name|N,name`, `--runs`, `--no-resume`,
  `--check`, `--aggregate-only`, `--no-judge`, `--judge-only`, `--re-judge`.
- **Resume**: i task già su disco (file con `error: null` e `judgment != null`)
  vengono saltati. `--no-judge` salva risposte NON giudicate (`judgment: null`)
  → sono "pending" per `--judge-only` / pulsante Judge GUI.
- **Aggregazione**: `aggregate_scores` conta SOLO modelli con file raw reali
  (fix applicato: prima inseriva tutti i modelli della config anche vuoti →
  righe fantasma "local-llama" 0/0 in scores.json).
- **Sanitizzazione nomi**: `safe_dir_name` (regex `[\\/:*?"<>|]` → `_`) è
  l'UNICA fonte di verità per i nomi cartella. La GUI usa la stessa funzione
  (importata da run_benchmark) — NON reinventarla, altrimenti i path raw
  divergono e la GUI non trova i file.

## GUI (benchmark/gui.py) — FreeSimpleGUI 5.0.0
Funzionalità: discovery modelli via `GET /v1/models` del server locale,
avvio/arresto llama-server (script in `D:\Programs\llama.cpp\*.cmd`),
esecuzione test, Judge/Re-judge, tabelle risultati, log, finestra ridimensionabile.
Opzioni run persistenti in `gui_config.json`: chiavi `questions`, `category`,
`runs` (stringhe), `resume` e `no_judge` (bool) — prefill a startup dei campi
Questions/Category/Runs e delle checkbox; salvate a ogni "Run benchmark" e alla
chiusura finestra (`App._save_run_settings`).
**Reasoning budget** (combo `-RBTOKENS-`, valori 512/1024/2048/4096/8192,
sezione Llama server): riflette `reasoning_budget_tokens` di local-llama in
config.yaml a startup e dopo "Reload config"; selezionando un valore scrive
immediatamente il file (`_set_local_reasoning_budget`: edit di riga mirata sul
blocco local-llama, commenti/layout preservati, mai il budget del judge;
se la key manca nel blocco la inserisce dopo `base_url`). Il file è la fonte
di verità: il valore NON va in gui_config.json. È una `sg.Combo` (non più
Listbox, 2026-08-22): `readonly=True, enable_events=True`; a selection
`combo.Get()` (legge il widget live); per impostare la selezione programmatica
`combo.update(value=str(v))`.

### Quirk FreeSimpleGUI 5.0.0 (verificati, costosi)
- **Tutte le `key` SENZA dash**: `key="Models"` genera `-Models-`; scrivere
  `key="-MODEL-"` darebbe `--MODEL--`.
- `sg.Table`: `selection_mode=sg.TABLE_MODE_BROWSE`, `enable_click_events=True`
  (NON `selectable_rows`, NON `single_select`, NON `max_col_width`).
- **`sg.Tabs` NON esiste in FSG 5.x**: il container è `sg.TabGroup` e i Tab
  vivono dentro: `sg.TabGroup([[sg.Tab("t", [[elem]])]])`. Tab e TabGroup
  ricevono layout di RIGHE (liste di liste): un elemento singolo va
  doppinidato `[[elem]]`, altrimenti FSG lo scarta SILENZIOSAMENTE
  ("stripped from your layout") senza crash — la key non si registra.
- **Key mancanti: fallback silenzioso**: `window["-KEY-"]` NON solleva se la
  key non c'è → restituisce l'elemento "closest" (es. un ProgressBar) e si
  ottiene un AttributeError a cascata, non un KeyError.
- **Radice tkinter**: `e.TKroot` (l'attributo `.TK` non esiste più in 5.x;
- **`auto_size_columns` è True di default**: a ogni `update(values=...)` le
  larghezze colonna vengono ricalcolate → `col_widths` apparentemente ignorato
  e i resize manuali "spariscono". Fix: `auto_size_columns=False` + `col_widths`.
- **`justification` di Table è `'right'` di default**: tutte le colonne vengono
  create con `anchor=tk.E` → testo allineato a destra. Fix: `cols_justification`
  (lista 'l'/'r'/'c', un entry per colonna; il valore applicato a creation è
  mantenuto anche dopo `update(values=...)`).
- **`right_click_menu` ha formato `[<ignorato>, [items...]]`**: la libreria
  legge `menu[1]` (slot 0 ignorato!); liste piatte → IndexError alla creation.
  Selezionando un item l'evento è la **stringa esatta dell'item**
  (LastButtonClicked = item). Il menu NON riporta la riga cliccata → bind
  `<Button-3>` sul Treeview + `identify_row` (iid = index+1).
- Combo: per cambiare il valore selezionato `combo.update(value="...")`
  (NON `key=` — FSG 5.0.0 solleva TypeError; NON `combo.set(...)`, non esiste).
  Verifica: `sg.Combo.update(value, values, set_to_index, ...)`.
- `Listbox.select_index` è **cumulativo**: aggiunge alla selezione senza
  cancellare la precedente (`get_indexes()` → `(0, 3, 4)`), a differenza del
  click reale dell'utente (single-select: sostituisce la selezione).
  Test headless: simulare il click con `lb.TKListbox.selection_clear(0,"end")`
  + `selection_set(i)` (e `selection_clear()` da solo NON funziona: `first`
  è obbligatorio). L'handler `-LLMALIST-` usa `get_indexes()[0]`: corretto
  perché un click reale lascia UN solo indice. (`-RBTOKENS-` NON è più una
  Listbox: è una `sg.Combo` che legge la selezione con `combo.Get()`, vedi
  sezione Reasoning budget qui sopra.)
- **Windows + `Path.write_text`**: text mode `newline=None` converte ogni `\n`
  in CRLF alla scrittura → un file YAML in LF diventerebbe CRLF in blocco.
  Per preservare i newline: leggere `read_bytes()` (anche la lettura con
  `read_text` normalizza CRLF→LF, quindi rileva il newline dai byte grezzi)
  e scrivere con `write_bytes(....encode("utf-8"))`.
- `sg.Window(...)` non ha `maximize` (crash); la finestra è resizable nativamente.
- Il log usa `sg.Multiline` nero (background black, justification sinistra);
  i Table della vecchia versione allineavano a destra.
- Layout: tabella Models NON expand_x (large 48 char per nomi GGUF lunghi);
  la colonna Category si espande; colonne risultati al 50% (5-7 char),
  colonna Model allargata del 40%.

## Ambiente (Windows, utente Danil)
- Python: `D:\Programs\Python\Python312` (lanciare sempre con `python` pieno,
  non affidarsi al PATH).
- llama.cpp in `D:\Programs\llama.cpp` (b10269), server su **127.0.0.1:8080**
  (OpenAI-compatibile, nessuna API key), in **router mode** con 16 preset
  (`llama-server_presets.ini`): i modelli si caricano **on-demand** e il nome
  `model` richiesto nell'API conta (routing by name) → `--model-name` deve
  essere il nome esatto di un preset. Nota: la versione singola classica
  ignorava il model id; con il router NON vale più.
- Modelli locali: via Ollama, es. `unsloth/qwen3.8-27b:Q3_K_XL`,
  `ManniX-ITA/qwen3.6-27b-omnimerge-v4-iq3`.
- `.env` in radice per le chiavi API (guardare `.env.example`).

## Stato attuale (lasciato così alla chiusura della sessione)
- **README fork** (2026-08-23): titolo `# BrainBench (llama fork)`; in cima la
  sezione `## This fork: GUI + local llama.cpp models` con le principali
  caratteristiche (one-click GUI, start/stop llama-server dalla GUI con build
  ggml/turboquant/unsloth, discovery GGUF, reasoning budget, Run/Judge/
  Re-judge/Check/Aggregate/Stop, tabelle risultati + 2 tab log, self-judge)
  e screenshot embedded `BrainBench-llama_GUI.jpg` (path relativo alla radice
  → GitHub la risolve su `main`). Sottosezione `### Configuring the llama
  servers (benchmark/gui_config.json)`: `llama_folder` (cartella llama.cpp,
  gli script vivono lì), `llama_versions` (lista {"name","script"}, prima =
  default), `llama_version` (scritta auto dalla GUI), requisiti script
  (.cmd semplice, porta 8080, `pause` stripato) e `base_url 127.0.0.1:8080/v1`
  in config.yaml (`local-llama` + `judge:`). Project Structure aggiornata
  (gui.py, .vbs/.cmd); rimosso l'intestato `## Project Structure` duplicato.
- **Tracked** (2026-08-23): `gui.py`, `run_benchmark_gui.vbs`/`.cmd` e
  `BrainBench-llama_GUI.jpg` sono ora committati (gui.py era untracked).
- **Rename modello** (2026-08-23): cartella raw rinominata
  `unsloth_qwen3.8-27b-ggml_IQ3_XXS` → `unsloth_qwen3.8-27b-mtp_IQ3_XXS`
  e aggiornati i 300 file `q*_run*.json` dentro: campo `model_name`
  → `unsloth/qwen3.8-27b-mtp:IQ3_XXS` (sostituzione a byte, JSON validati,
  0 riferimenti al nome vecchio in `results/`). `scores.json` non toccato:
  usa il nome cartella come chiave (era già coerente).
- **Log GUI pulito** (2026-08-23): info pending nel tab App log (niente più
  Text nell'header), dedup righe identiche consecutive, job completato
  one-shot, loop `<Configure>` spezzato, righe vuote filtrate a monte in
  `log()` (vedi bug 10-11). Testato headless e confermato in GUI reale.
- Pipeline testata end-to-end con `unsloth/qwen3.8-27b-mtp:Q3_K_XL` (q1, cat 1,
  run 1, `--no-judge` poi `--judge-only`): risposta non vuota, `reasoning`
  salvato nel raw, self-judge ok. Con il reasoning budget attivo (2026-08-22):
  il raw porta `reasoning_budget_tokens: 4096` + messaggio, thinking ~2200
  token (sotto budget → finisce naturale), judge (budget 1024) `correct=false`
  (il modello hedge "it depends" invece di "sail" — giudizio legittimo),
  scores 0/1. Raw in `results/v1/raw/unsloth_qwen3.8-27b-mtp_Q3_K_XL/`.
- Le 2 risposte vuote del modello non-MTP sono state eliminate (artefatto del
  vecchio max_tokens 1024). Per i test usare `unsloth/qwen3.8-27b-mtp:Q3_K_XL`
  (già in memoria: è il modello su cui gira la sessione pi — lo usa PI_MODEL).
- `local-llama.max_tokens` ora **8192** (era 1024); judge `_judge_openai`
  max_tokens **8192** (era 512, stesso rischio di troncamento del thinking).
- **Combo "Reasoning budget"** nella GUI (era Listbox, sostituita 2026-08-22):
  valori 512/1024/2048/4096/8192, modifica `reasoning_budget_tokens` di
  local-llama in config.yaml (il risultato dei test dipende da questo
  valore). Scrittura mirata riga-per-riga (preserva commenti; CRLF/LF
  intatti), mai il budget del judge. `sg.Combo(readonly=True,
  enable_events=True)`; selection via `combo.Get()`; refresh via
  `combo.update(value=...)`. Testato headless: selezione iniziale
  = valore nel file, click → write, round-trip byte-identico, main loop
  reale (evento `-RBTOKENS-`).
- **Reasoning budget per-request** (llama-server b10472+, PR #23116): i campi
  `reasoning_budget_tokens`/`reasoning_budget_message` in config.yaml
  (local-llama 4096, judge 1024) vengono spediti in `extra_body` di
  chat.completions — mai come kwargs diretti (l'openai client li rifiuta).
  Nessun flag di server, nessun reload, la sessione pi sullo stesso server è
  intoccata. Al budget il server inietta il messaggio (soft stop del thinking).
  La cappa è un tetto, non un target: se il thinking finisce prima, il budget
  non scatta.
- I raw includono ora il campo `"reasoning"` (thinking, per debug; solo i
  provider OpenAI-compat lo espongono: llama.cpp → `reasoning_content`,
  OpenAI → `reasoning`; estratti in models.py, salvati in run_benchmark.py).
- `gui.py` (e `run_benchmark_gui.vbs`/`.cmd`, `BrainBench-llama_GUI.jpg`) ora **committati** (2026-08-23; prima gui.py era untracked).
- Il log è in un `sg.TabGroup` con 2 tab: **App log** (eventi GUI) e
  **Server log** (stdout di llama-server, via `log(line, server=True)`;
  i lifecycle "[llama] starting/stopped/exited" restano nell'App log).
- Unico commit del repo: `ad8b304 Initial release`.
- Log persistenti per debug: ogni riga mostrata nelle tab va ANCHE su file in radice,
  scritti via `App.log()` → `_log_to_file`: `bench_app.log.txt` (tab App log) e
  `bench_server.log.txt` (tab Server log). Append+flush per riga, best-effort
  (un errore di scrittura non rompe la GUI). Righe tqdm escluse, come in UI;
  inoltre (2026-08-23) le righe vuote/solo-spazi sono filtrate a monte in
  `log()` (né a schermo né nel file) e le righe identiche consecutive sono
  deduplicate.

## Bug già fixati in questa sessione (non ri-rivelare)
1. `import re` mancante in run_benchmark.py (GUI non partiva).
2. `--no-judge`: `result["judgment"].get(...)` in `run_with_progress` crashava
   (judgment=None) → le risposte venivano salvate ma perse in memoria. Fix:
   gestione judgment None, colonna mostra `-`.
3. `raw_dir_for` in gui.py usava il display name non sanificato → "Nothing to
   judge" con 4 pending. Fix: `safe_dir_name` condivisa.
4. `aggregate_scores` inseriva modelli senza file in scores.json. Fix: solo
   modelli con raw reali.
5. Colonne tabella "reset" a ogni refresh → `auto_size_columns=False`.
6. Colonne "Model" e "Category" allineate a destra: default di FreeSimpleGUI
   5.0.0 (`justification='right'` su Table). Fix: `cols_justification=["l",…"r"]`
   su -MODELS- e -CATS- (colonna testo 'l', colonne numeriche 'r').
7. `llama_version` salvata SEMPRE "ggml" anche con un'altra variante scelta nella
   combo: `App._llama_version()` faceva `self.e["-LLAMAVER-"].Get() or LLAMA_VERSIONS[0]`
   → fallback silenzioso se Get() restituisce ""/None. Fix (2026-08-22): mirror in Python
   `self._llama_ver_sel` — inizializzato in `_build_layout` (valore validato dal file),
   aggiornato nell'handler `-LLAMAVER-` (widget vivo: `Get() or mirror`), poi
   `self._save_llama_settings()` + log "Llama server: X (salvato)". `_llama_version()`
   restituisce solo il mirror (nessun Get() live, nessun fallback ggml).
   Verificato con test headless sul VERO main loop (patch di `app.e.read`):
   `TKCombo.current(2)` + evento `-LLAMAVER-` → file con `llama_version: unsloth`,
   mai "ggml"; run-settings utente intatti; gui_config.json poi restaurato.
8. **Risposte VUOTE** con `local-llama` (max_tokens 1024): il modello è
   reasoning e il server preset forza thinking `xhigh` → tutti i 1024 token
   finivano nel thinking, `content=""` salvato nei raw. Fix (2026-08-22):
   max_tokens 8192 in config (local-llama) e in `_judge_openai` (512→8192);
   salvato il campo `reasoning` nei raw per debug (models.py + run_benchmark.py).
   Verificato end-to-end: q1 cat1 run1 → risposta+reasoning salvati,
   self-judge correct=true, scores 1/1.
9. **Reasoning budget per-request** (2026-08-22, su richiesta utente):
   `reasoning_budget_tokens`/`reasoning_budget_message` in config.yaml,
   letti da `parse_model_configs` → `ModelConfig`, inviati in `extra_body`
   (models.py e judge.py — l'openai client li rifiuta come kwargs: TypeError).
   `run_benchmark.py` li salva nei raw per provenienza. Probe live: budget 300
   → thinking tagliato a ~300 token + messaggio iniettato + risposta (348
   total). E2E con budget 4096 (modello) / 1024 (judge): risposta completa,
   thinking ~2200 token < budget (cappa non scattata), judge coerente,
   scores 0/1. Verifica A/B live (2026-08-22): stessa domanda, budget 30,
   unica variabile il campo `reasoning_budget_message` con marker unico +
   istruzione "inizia la risposta con MARK7F3A" → il modello ha risposto
   letteralmente "MARK7F3A 1 trip." (messaggio INIETTATO per-request, non
   solo flag di server); senza il campo → risposta normale, nessun marker.
   A budget raggiunto il thinking TRONCATO viene comunque restituito in
   `reasoning_content`, tagliato esattamente al confine del budget
   (utile per debug).
10. **Log spam / righe duplicate** (2026-08-23, 4 fix in gui.py):
   (a) il `sg.Text` `-PENDINGINFO-` nell'header è rimosso: l'info pending
   (es. "239 pending (unjudged/errored) responses") va nel tab App log via
   `log()` in `_refresh_results`; (b) `log()` salta la riga se identica alla
   precedente (dedup per target: `self._last_app_log` / `self._last_srv_log`,
   prima di `_log_to_file` e dell'inserimento nel tab);
   (c) **job one-shot**: dopo `job.finished` → `self.job = None` (prima il
   blocco di completion si rieseguiva a ogni iterazione del loop → coppie
   duplicate `[exit code 0]` / `Scores updated. Review the tables above.`);
   (d) **loop di feedback `<Configure>`**: il bind chiamava `_refresh_models`
   + `_refresh_results` a ogni evento configure, e l'update delle tabelle
   generava a sua volta nuovi configure (~20 refresh/sec in perpetuo).
   Fix: `_on_configure` ora fa i refresh solo se la dimensione root è
   cambiata davvero (guard `self._last_cfg_size`).
11. **Righe vuote nel server log** (2026-08-23): il filtro delle righe
   vuote era solo in `_log_to_file` → file pulito ma tab a schermo pieno di
   righe vuote (il server llama.cpp emette newline extra). Fix a monte:
   in `log()` subito dopo il filtro tqdm:
   `line = line.rstrip("\r\n")` + `if not line.strip(): return` —
   righe vuote/solo-spazi scartate prima di dedup, file e tab;
   `_log_to_file` ora scrive solo (best-effort). Il testo reale passa
   intatto (solo newline di coda rimossi).

## Convenzioni di lavoro
- L'utente parla italiano, rispondere in italiano.
- **Il progetto è sotto git** (branch `master`): ogni volta che serve ripristinare un file o controllare le modifiche fatte si usa git — `git status`, `git diff`, `git checkout -- <file>`, `git log --oneline`. I file tracciati sono coperti dai commit; per ripristinare un file al suo ultimo commit si usa `git checkout -- <file>` (o `git restore <file>`).
- Mai reloadare il server llama.cpp per testare: usare il modello già carico
  (di solito `unsloth/qwen3.8-27b:Q3_K_XL`).
- Per verificare che un campo per-request arrivi a llama-server: **probe A/B**
  con marker unico in `reasoning_budget_message` (es. "PROBE-MARK: ... inizia
  la risposta con XYZ") — il modello ecoa il token SOLO se il messaggio è
  stato davvero iniettato; la seconda probe senza campo è il controllo.
- Test GUI headless: `timeout 90 python -X utf8 - <<EOF` con `tk.Tk().withdraw()`,
  poi chiudere con `app.win.TK.destroy()`.
- Dopo i test: `rm -rf benchmark/__pycache__`, `rm -f test_*.py` transienti.
