#!/usr/bin/env python3
"""afk - run any long command, go outside, get a smart summary on your phone.

    python afk.py --init                     (once: creates afk.json)
    python afk.py --ping                     (sends a test notification)
    python afk.py [--goal "..."] [--style coach] -- <your long command>

The command runs normally. When it ends, a local Gemma model (via Ollama) reads
the log and writes a short report: errors, what got done, what is left and
what to improve. The report is pushed to your phone with ntfy.
Only the Python standard library is used, nothing to pip install.
"""
import argparse
import collections
import copy
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AFK_CONFIG", HERE / "afk.json"))
LOG_DIR = Path(os.environ.get("AFK_LOGS", HERE / "afk_logs"))

DEFAULT_CONFIG = {
    "ntfy_server": "https://ntfy.sh",
    "ntfy_token": "",
    "topic": "",
    "ollama_url": "http://localhost:11434",
    "model": "gemma4:e2b",
    "style": "plain",
    "presets": {
        "plain": "Neutral, short and factual. No jokes.",
        "coach": "A warm, upbeat coach. Remind me to enjoy the rest of my walk.",
        "funny": "Playful and a little silly. Jokes only in the headline.",
        "calm": "Gentle and reassuring, like a friend who says nothing is urgent.",
    },
    "num_ctx": 4096,
    "max_log_chars": 9000,
    "llm_timeout_seconds": 300,
    "priority_ok": 3,
    "priority_fail": 4,
}

KEY_RE = re.compile(
    r"error|fail|exception|traceback|fatal|denied|not found|timed? ?out|panic|cannot|unable|warning",
    re.I,
)
SECRET_RES = [
    re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password|passwd|pwd)\w*\s*[=:]\s*)\S+"),
    re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[bap])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{16,}"),
]


# ---------------------------------------------------------------- config
def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "presets" and isinstance(v, dict):
                cfg["presets"].update(v)
            else:
                cfg[k] = v
    return cfg


def init_config():
    if CONFIG_PATH.exists():
        cfg = load_config()
        print(f"afk.json already exists. Your topic is: {cfg['topic']}")
    else:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["topic"] = "afk-" + secrets.token_hex(5)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"Created {CONFIG_PATH}")
    print()
    print("Next: install the ntfy app on your phone, tap +, and subscribe to this topic:")
    print(f"    {cfg['topic']}")
    print("Then run:  python afk.py --ping")


# ---------------------------------------------------------------- helpers
def fmt_duration(sec):
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def redact(text):
    for rx in SECRET_RES:
        if rx.groups:
            text = rx.sub(lambda m: m.group(1) + "[hidden]", text)
        else:
            text = rx.sub("[hidden]", text)
    return text


def clip_head(text, n):
    return text if len(text) <= n else text[:n] + " ...[cut]"


def clip_tail(text, n):
    return text if len(text) <= n else "[cut]... " + text[-max(n, 200):]


# ---------------------------------------------------------------- run
def run_command(command):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"run-{stamp}.log"
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    head, key = [], []
    tail = collections.deque(maxlen=250)
    total = 0
    start = time.time()
    proc = subprocess.Popen(
        command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    with open(log_path, "w", encoding="utf-8") as f:
        try:
            for line in proc.stdout:
                print(line, end="", flush=True)
                f.write(line)
                line = line.rstrip("\n")[:300]
                total += 1
                if len(head) < 40:
                    head.append(line)
                if len(key) < 40 and KEY_RE.search(line):
                    key.append(line)
                tail.append(line)
        except KeyboardInterrupt:
            proc.terminate()
            tail.append("[run was stopped by the user with Ctrl+C]")
    proc.wait()
    return {
        "exit_code": proc.returncode, "head": head, "key": key, "tail": list(tail),
        "total": total, "seconds": time.time() - start, "log_path": log_path,
    }


def build_excerpt(run, budget):
    if run["total"] <= len(run["tail"]):
        return "[FULL LOG]\n" + clip_tail("\n".join(run["tail"]), budget)
    h = clip_head("\n".join(run["head"]), budget // 5)
    k = clip_head("\n".join(run["key"]), budget // 4)
    t = clip_tail("\n".join(run["tail"]), budget - len(h) - len(k))
    return (
        f"[total lines in log: {run['total']}]\n[FIRST LINES]\n{h}\n\n"
        f"[LINES THAT MENTION ERRORS OR WARNINGS]\n{k or '(none)'}\n\n[LAST LINES]\n{t}"
    )


# ---------------------------------------------------------------- Gemma
SYSTEM_PROMPT = """You summarise the output of a long-running computer task for a person who is away from their screen.
Reply with ONE JSON object and nothing else, with exactly these keys:
"headline": a short title, max 70 characters,
"errors": list of strings. Each is one problem found in the log, quoting the key words of the error message. Empty list if there are none,
"completed": list of strings. Things the log shows were finished,
"remaining": list of strings. Things the goal needs that the log does not show finished. If you cannot tell, use ["unclear from the log"],
"suggestions": list of at most 3 strings. Practical next steps or improvements. These are your advice, not facts from the log.
Rules: errors, completed and remaining must use ONLY facts found in the log. Never invent file names, numbers or messages. Keep every item under 120 characters. The STYLE line changes only your wording, never the facts."""


def ask_gemma(cfg, user_prompt):
    payload = {
        "model": cfg["model"], "stream": False, "format": "json",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0.2, "num_ctx": cfg["num_ctx"]},
    }
    req = urllib.request.Request(
        cfg["ollama_url"].rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=cfg["llm_timeout_seconds"]) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["message"]["content"]


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except ValueError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            return json.loads(text[a:b + 1])
        raise


def clean_list(value, cap):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    items = [clip_head(str(v).strip(), 140) for v in value if str(v).strip()]
    return items[:cap]


def summarise(cfg, run, command, goal, style_text):
    status = "success" if run["exit_code"] == 0 else "failed"
    user_prompt = (
        f"STYLE: {style_text}\n"
        f"GOAL (what the person wanted, may be empty): {goal or '(none given)'}\n"
        f"COMMAND: {command}\n"
        f"EXIT CODE: {run['exit_code']} ({status}). RUN TIME: {fmt_duration(run['seconds'])}\n\n"
        f"LOG EXCERPT:\n{build_excerpt(run, cfg['max_log_chars'])}"
    )
    raw = parse_json(ask_gemma(cfg, user_prompt))
    errors = clean_list(raw.get("errors"), 6)
    if status == "failed" and not errors:
        errors = [f"Exited with code {run['exit_code']} but no clear error line in the log"]
    return {
        "status": status,  # the exit code decides this, not the model
        "headline": clip_head(str(raw.get("headline", "")).strip(), 80) or f"Task {status}",
        "errors": errors,
        "completed": [c for c in clean_list(raw.get("completed"), 6) if not re.search(r"(?i)^command\b|exit code", c)],
        "remaining": clean_list(raw.get("remaining"), 6),
        "suggestions": clean_list(raw.get("suggestions"), 3),
        "ai": True,
    }


def fallback_summary(run, reason):
    status = "success" if run["exit_code"] == 0 else "failed"
    return {
        "status": status,
        "headline": f"Task {status} (no AI summary)",
        "errors": run["key"][:3] or ([f"Exited with code {run['exit_code']}"] if status == "failed" else []),
        "completed": [],
        "remaining": [],
        "suggestions": [f"Gemma summary unavailable ({reason}). Showing the raw log tail below."],
        "tail": run["tail"][-8:],
        "ai": False,
    }


# ---------------------------------------------------------------- report + notify
def render_report(s, run, command):
    out = []

    def block(title, items, empty=None):
        if items:
            out.append(title)
            out.extend(f"- {i}" for i in items)
            out.append("")
        elif empty:
            out.append(title)
            out.append(f"- {empty}")
            out.append("")

    block("ERRORS", s["errors"], "none spotted")
    block("DONE", s["completed"] or (["Finished without errors"] if run["exit_code"] == 0 else []))
    block("STILL TO DO", s["remaining"])
    block("NEXT IDEAS", s["suggestions"])
    if s.get("tail"):
        out.append("LOG TAIL")
        out.extend(s["tail"])
        out.append("")
    out.append(f"Took {fmt_duration(run['seconds'])} | exit code {run['exit_code']} | {command[:50]}")
    text = redact("\n".join(out))
    raw = text.encode("utf-8")
    if len(raw) > 3800:
        text = raw[:3800].decode("utf-8", errors="ignore") + "\n[report cut]"
    return text


def send_ntfy(cfg, title, message, priority, tags):
    payload = {
        "topic": cfg["topic"], "title": redact(title), "message": message,
        "priority": priority, "tags": tags,
    }
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if cfg.get("ntfy_token"):
        headers["Authorization"] = "Bearer " + cfg["ntfy_token"]
    req = urllib.request.Request(
        cfg["ntfy_server"].rstrip("/"), data=json.dumps(payload).encode("utf-8"), headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


# ---------------------------------------------------------------- main
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="Run a long command, walk away, get a summary on your phone.")
    ap.add_argument("--init", action="store_true", help="create afk.json")
    ap.add_argument("--ping", action="store_true", help="send a test notification")
    ap.add_argument("--goal", default="", help="what you wanted the task to achieve")
    ap.add_argument("--style", help="preset name from afk.json, or your own words")
    ap.add_argument("--model", help="Ollama model to use")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--no-llm", action="store_true", help="skip Gemma, send raw log tail")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="the command to run")
    args = ap.parse_args()

    if args.init:
        init_config()
        return 0

    cfg = load_config()
    if not cfg["topic"]:
        print("No topic set. Run:  python afk.py --init")
        return 2
    if args.model:
        cfg["model"] = args.model

    if args.ping:
        send_ntfy(cfg, "afk is connected", "If you can read this on your phone, you are ready to walk away.",
                  3, ["footprints"])
        print("Test notification sent. Check your phone.")
        return 0

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.print_help()
        return 2
    if len(cmd) == 1:
        command = cmd[0]  # a whole quoted command, e.g. "npm run build && npm test"
    elif os.name == "nt":
        command = subprocess.list2cmdline(cmd)
    else:
        command = shlex.join(cmd)

    style_name = args.style or cfg["style"]
    style_text = cfg["presets"].get(style_name, style_name)

    print(f"[afk] running: {command}")
    print("[afk] you can leave now. Your phone will buzz when it is done.\n")
    run = run_command(command)

    print(f"\n[afk] finished with exit code {run['exit_code']}, summarising...")
    if args.no_llm:
        summary = fallback_summary(run, "skipped with --no-llm")
    else:
        try:
            summary = summarise(cfg, run, command, args.goal, style_text)
        except Exception as e:  # Ollama down, bad JSON, timeout...
            summary = fallback_summary(run, type(e).__name__)
    report = render_report(summary, run, command)
    report_path = run["log_path"].with_suffix(".report.txt")
    report_path.write_text(report, encoding="utf-8")

    ok = run["exit_code"] == 0
    title = summary["headline"]
    priority = cfg["priority_ok"] if ok else cfg["priority_fail"]
    tags = ["white_check_mark"] if ok else ["rotating_light"]

    print("\n" + "=" * 60 + f"\n{title}\n\n{report}\n" + "=" * 60)
    if args.dry_run:
        print("[afk] dry run, nothing sent.")
    else:
        try:
            send_ntfy(cfg, title, report, priority, tags)
            print("[afk] notification sent.")
        except Exception as e:
            print(f"[afk] could not send notification ({e}). Report saved to {report_path}")
    return run["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
