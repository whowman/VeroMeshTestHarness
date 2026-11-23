"""harness serve | answer-key | systems

harness serve --people 2000 --seed 7 --port 8100 [--days 30] [--systems entra,servicenow]
harness answer-key --people 2000 --seed 7 --days 70 -o key.json
harness systems
"""

from __future__ import annotations

import argparse
import json
import sys

from . import clock
from .app import answer_key, create_app
from .systems import REGISTRY
from .truth import generate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness", description="VeroMesh test harness: a simulated company and its systems")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def company(p):
        p.add_argument("--profile", default="healthcare")
        p.add_argument("--people", type=int, default=2000)
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--days", type=int, default=0, help="move the clock this many days before starting")

    s = sub.add_parser("serve", help="run the simulated systems")
    company(s)
    s.add_argument("--host", default="0.0.0.0")  # noqa: S104 — a container's service
    s.add_argument("--port", type=int, default=8100)
    s.add_argument("--systems", help="comma-separated; default all")
    k = sub.add_parser("answer-key", help="print what VeroMesh should find")
    company(k)
    k.add_argument("-o", "--out")
    sub.add_parser("systems", help="list the simulated systems")
    a = ap.parse_args(argv)

    if a.cmd == "systems":
        for name, cls in sorted(REGISTRY.items()):
            print(f"{name:12} {cls.title}")
        return 0
    truth = generate(a.profile, a.people, a.seed)
    if a.days:
        clock.advance(truth, a.days)
    if a.cmd == "answer-key":
        systems = {n: c(truth) for n, c in REGISTRY.items()}
        text = json.dumps(answer_key(truth, systems), indent=1, default=str)
        (open(a.out, "w").write(text) if a.out else sys.stdout.write(text + "\n"))
        return 0
    import uvicorn

    app = create_app(truth, a.systems.split(",") if a.systems else None)
    uvicorn.run(app, host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
