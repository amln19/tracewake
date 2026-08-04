"""A local page for labeling the blinded packets by clicking a step.

Served from localhost rather than opened as a file, because the page has to
write each answer back to the sheet as it is given — a `file://` page cannot,
and a pass that only saves at the end is a pass that loses work.

Packets are parsed from the exported markdown rather than rebuilt from the
store, so the page shows exactly what the terminal pass shows.
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .label import LABEL_ROOT, _sheet_rows, _write_sheet, failure_length

_STEP = re.compile(r"^ {0,3}(\d+)\. (.+)$")
_HEAD = re.compile(r"^## (SUCCESS|FAILURE)\b.*?\((\d+) steps\)")
_PAGE = Path(__file__).with_name("labelui.html")


def _parse_side(lines: list[str], start: int, end: int) -> list[dict]:
    steps: list[dict] = []
    i = start
    while i < end:
        match = _STEP.match(lines[i])
        if not match:
            i += 1
            continue
        rest = match.group(2)
        status = "error" if rest.rstrip().endswith("[error]") else "ok"
        rest = re.sub(r"\s*\[error\]\s*$", "", rest)
        name, _, target = rest.partition("→")
        args = reason = ""
        j = i + 1
        while j < end and not _STEP.match(lines[j]):
            text = lines[j].strip()
            if text.startswith("args:"):
                args = text[5:].strip()
            elif text.startswith("reason:"):
                reason = text[7:].strip()
                k = j + 1
                while k < end and lines[k].strip() and not _STEP.match(lines[k]):
                    reason += " " + lines[k].strip()
                    k += 1
                j = k - 1
            j += 1
        steps.append(
            {
                "n": int(match.group(1)),
                "name": name.strip(),
                "target": target.strip(),
                "status": status,
                "args": args,
                "reason": reason,
            }
        )
        i += 1
    return steps


def parse_packet(text: str) -> dict:
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines) if _HEAD.match(line)]
    if len(heads) != 2:
        raise ValueError("packet does not have exactly one SUCCESS and one FAILURE heading")
    definition = " ".join(
        line.strip() for line in lines[: heads[0]] if line.strip() and not line.startswith("#")
    ).replace("Label: ________", "").strip()
    return {
        "definition": definition,
        "success": _parse_side(lines, heads[0], heads[1]),
        "failure": _parse_side(lines, heads[1], len(lines)),
    }


def load_state(sheet: str, dest: Path) -> dict:
    packets_dir = dest / "packets"
    rows = _sheet_rows(dest / f"{sheet}.jsonl", packets_dir)
    packets = []
    for row in rows:
        text = (packets_dir / f"{row['packet_id']}.md").read_text(encoding="utf-8")
        parsed = parse_packet(text)
        if len(parsed["failure"]) != failure_length(text):
            raise ValueError(
                f"{row['packet_id']}: parsed {len(parsed['failure'])} failure steps but the "
                f"heading says {failure_length(text)}. The page would offer a step that is "
                f"not there."
            )
        packets.append({"id": row["packet_id"], **parsed})
    return {"sheet": sheet, "packets": packets, "rows": rows}


def serve(sheet: str = "pass1", dest: Path = LABEL_ROOT, port: int = 8765) -> str:
    state = load_state(sheet, dest)
    path = dest / f"{sheet}.jsonl"
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet; the page is the interface
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                payload = {"sheet": state["sheet"], "packets": state["packets"], "rows": state["rows"]}
                self._send(200, json.dumps(payload).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if self.path != "/api/label":
                self._send(404, b"not found", "text/plain")
                return
            size = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(size) or b"{}")
            with lock:
                for row in state["rows"]:
                    if row["packet_id"] == body["packet_id"]:
                        row["label"] = body["label"]
                        row["note"] = body.get("note", "")
                        break
                else:
                    self._send(404, b"unknown packet", "text/plain")
                    return
                _write_sheet(path, state["rows"])
            done = sum(1 for r in state["rows"] if r["label"] is not None)
            self._send(200, json.dumps({"done": done}).encode(), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    done = sum(1 for r in state["rows"] if r["label"] is not None)
    print(f"{sheet}: {done}/{len(state['rows'])} labeled — {path}")
    print(f"open {url}   (ctrl-c here when you are done)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    done = sum(1 for r in state["rows"] if r["label"] is not None)
    return f"{sheet}: {done}/{len(state['rows'])} labeled in {path}"
