# afk

Waiting on a build, a test run or a coding agent usually means staring at the screen. `afk` wraps any long command: you start it, leave, and your phone buzzes when it finishes with a short report written by a local Gemma model.

The report always has the same parts: **errors**, **done**, **still to do**, **next ideas**. You choose the tone of the notification (plain, coach, funny, calm, or your own words).

```
python afk.py --goal "run the full test suite" --style coach -- pytest -x
```

## How it works

1. Your command runs normally and the output is saved to a log file.
2. When it ends, the log (first lines, error-looking lines, last lines) goes to Gemma through Ollama on your own machine.
3. The summary is pushed to your phone with [ntfy](https://ntfy.sh), an open-source notification service.

Pass `--goal` to say what you wanted done. That is how it can tell you what is still left. Without a goal it says "unclear from the log" instead of guessing.

## Setup (Windows)

1. Install Python 3.9+ from python.org (tick "Add Python to PATH").
2. Install [Ollama](https://ollama.com) and pull a model: `ollama pull gemma4:e2b` (small enough for a 4 GB GPU). Tags change, so check `ollama list` and set `"model"` in `afk.json` if yours differs.
3. Install the ntfy app on your phone.
4. In this folder run `python afk.py --init`, then subscribe to the printed topic in the app.
5. `python afk.py --ping` should buzz your phone.

No pip packages needed.

## Usage

```
python afk.py -- npm run build
python afk.py --goal "train for 3 epochs" --style funny -- python train.py
python afk.py -- "npm run build && npm test"
python afk.py --dry-run -- echo hi        # print the report, send nothing
```

Styles live in `afk.json` under `presets`. Add your own or pass free text: `--style "grumpy sysadmin"`.

## Privacy, honestly

- The raw log never leaves your machine, and Gemma runs locally.
- The short summary does travel through the ntfy server (ntfy.sh by default). Obvious secrets such as `api_key=...` and common token formats are hidden first, but that is best-effort only. Self-host ntfy (set `ntfy_server`) if your logs are sensitive.
- The topic name is the only thing protecting your notifications on ntfy.sh, so keep it random.

## Limits

- A small local model can misread a log. The exit code decides success or failure, not the model, and every report ends with the raw facts (time, exit code).
- Suggestions are the model's advice, not facts from the log.

MIT licensed.
