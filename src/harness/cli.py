"""harness serve | answer-key | systems | run

harness serve --people 2000 --seed 7 --port 8100 [--days 30] [--systems entra,servicenow]
                  [--difficulty 0|1|2] [--quirk name-variant=0.9 ...]
harness answer-key --people 2000 --seed 7 --days 70 -o key.json
harness systems
harness run --veromesh http://localhost:8700 [--harness http://localhost:8100] [--token ...] [-o report.json]
"""

from __future__ import annotations

import argparse
import json
import os
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
        p.add_argument(
            "--difficulty", type=float, default=1.0, help="quirk scale: 0 none (and no sync lag), 1 as described, 2 twice as many"
        )
        p.add_argument(
            "--quirk",
            action="append",
            default=[],
            metavar="KIND=RATE",
            help="set one quirk's rate by its defect kind, e.g. name-variant=0.9",
        )

    s = sub.add_parser("serve", help="run the simulated systems")
    company(s)
    s.add_argument("--host", default="0.0.0.0")  # noqa: S104 — a container's service
    s.add_argument("--port", type=int, default=8100)
    s.add_argument("--systems", help="comma-separated; default all")
    s.add_argument("--chaos", type=float, default=0.0, help="share of system requests that misbehave (429, 500, 503, expired token, slow)")
    k = sub.add_parser("answer-key", help="print what VeroMesh should find")
    company(k)
    k.add_argument("-o", "--out")
    sub.add_parser("systems", help="list the simulated systems")
    r = sub.add_parser("run", help="put today's scenarios to VeroMesh and score its answers")
    r.add_argument("--veromesh", default=os.environ.get("VEROMESH_API", "http://localhost:8700"))
    r.add_argument("--token", default=os.environ.get("VEROMESH_TOKEN"), help="a token with the steward role")
    r.add_argument("--harness", default=os.environ.get("HARNESS_URL", "http://localhost:8100"))
    r.add_argument("-o", "--out", help="write the answers, gaps and score as JSON")
    m = sub.add_parser("measures", help="Tier 1 measures: today's scenarios with and without the model, M2 and M6")
    m.add_argument("--veromesh", default=os.environ.get("VEROMESH_API", "http://localhost:8700"))
    m.add_argument("--token", default=os.environ.get("VEROMESH_TOKEN"), help="a token with the steward role")
    m.add_argument("--harness", default=os.environ.get("HARNESS_URL", "http://localhost:8100"))
    m.add_argument("-o", "--out", help="write the measures as JSON")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        return run_scenarios(a)
    if a.cmd == "measures":
        return run_measures(a)

    if a.cmd == "systems":
        for name, cls in sorted(REGISTRY.items()):
            print(f"{name:12} {cls.title}")
        return 0
    overrides = {k: float(v) for k, _, v in (q.partition("=") for q in a.quirk)}
    truth = generate(a.profile, a.people, a.seed, difficulty={"scale": a.difficulty, "overrides": overrides})
    if a.days:
        clock.advance(truth, a.days)
    if a.cmd == "answer-key":
        systems = {n: c(truth) for n, c in REGISTRY.items()}
        text = json.dumps(answer_key(truth, systems), indent=1, default=str)
        (open(a.out, "w").write(text) if a.out else sys.stdout.write(text + "\n"))
        return 0
    import uvicorn

    app = create_app(truth, a.systems.split(",") if a.systems else None)
    app.state.chaos["rate"] = a.chaos
    uvicorn.run(app, host=a.host, port=a.port)
    return 0


def run_scenarios(a, veromesh_http=None, harness_http=None) -> int:
    import httpx

    from .runner import VeroMesh, run

    vm = VeroMesh(veromesh_http or httpx.Client(timeout=120), a.veromesh.rstrip("/"), a.token)
    report = run(vm, harness_http or httpx.Client(timeout=120), a.harness.rstrip("/"))
    print("\n".join(report.lines()))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(
                {
                    "today": report.today,
                    "answers": report.answers,
                    "notYet": report.not_yet,
                    "partial": report.partial,
                    "errors": report.errors,
                    "score": report.score,
                },
                f,
                indent=1,
            )
    return 0 if not report.errors else 1


def run_measures(a, veromesh_http=None, harness_http=None) -> int:
    import httpx

    from .runner import NotYet, VeroMesh, measures, run

    vm = VeroMesh(veromesh_http or httpx.Client(timeout=120), a.veromesh.rstrip("/"), a.token)
    h = harness_http or httpx.Client(timeout=120)
    with_model = run(vm, h, a.harness.rstrip("/"))
    without = run(None, h, a.harness.rstrip("/"), without_model=True)
    extra = {}
    for key, path in (("model", "/measures/model"), ("decisions", "/decisions/measures")):
        try:
            extra[key] = vm.get(path)
        except (NotYet, httpx.HTTPError):
            extra[key] = None
    got = measures(with_model, without, extra["model"], extra["decisions"])

    def pct(x):
        return "n/a" if x is None else f"{x:.0%}"

    m1, m5 = got["M1"], got["M5"]
    print(f"Tier 1 measures on {got['today']}")
    print(
        f"  M1 context question accuracy: {m1['withModel']['passed']} of {m1['withModel']['of']} "
        f"({pct(m1['withModel']['share'])}) with the model, {m1['withoutModel']['passed']} "
        f"({pct(m1['withoutModel']['share'])}) without"
    )
    print(
        f"  M5 cross-team impacts routed: {m5['withModel']['passed']} of {m5['withModel']['of']} with the model, "
        f"{m5['withoutModel']['passed']} without"
    )
    if got.get("M2"):
        print(f"  M2 facts sourced automatically: {pct(got['M2']['value'])}")
    if got.get("M6"):
        for src, lat in sorted(got["M6"]["bySource"].items()):
            print(f"  M6 change latency, {src}: median {lat['median']} h, 90th percentile {lat['p90']} h")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(got, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
