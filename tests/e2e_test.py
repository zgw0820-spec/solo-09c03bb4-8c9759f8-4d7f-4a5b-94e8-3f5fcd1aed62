"""End-to-end tests for the C build cache service.

Usage: python tests/e2e_test.py [base_url]   (default http://127.0.0.1:8000)

Covers: basic build, cache reuse on identical resubmission, header-change
invalidation, same-name header shadowing, concurrent identical builds
(singleflight), compile/link failure diagnostics, and path validation.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(name)


def run_exe(data: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="cbuild-exe-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return subprocess.run([path], capture_output=True, text=True, timeout=10).stdout.strip()
    finally:
        os.unlink(path)


def post_build(client: httpx.Client, files: dict[str, str], output: str = "app") -> httpx.Response:
    payload = {"output": output, "files": [{"path": p, "content": c} for p, c in files.items()]}
    return client.post("/build", json=payload, timeout=60)


def counts(resp: httpx.Response) -> tuple[int, int, int]:
    return (int(resp.headers["X-Compiled-Count"]),
            int(resp.headers["X-Reused-Count"]),
            int(resp.headers["X-Unit-Count"]))


# ---------------------------------------------------------------- project P1
P1_UTIL_H_V1 = (
    "#ifndef UTIL_H\n#define UTIL_H\n"
    "#define UTIL_ANSWER 42\n#define UTIL_MSG \"v1\"\n"
    "int util_answer(void);\nconst char *util_msg(void);\n#endif\n"
)
P1_UTIL_H_V2 = P1_UTIL_H_V1.replace("42", "43").replace('"v1"', '"v2"')

P1 = {
    "main.c": (
        "#include <stdio.h>\n#include \"util.h\"\n#include \"noinc.h\"\n"
        "int main(void) {\n"
        "    printf(\"msg=%s answer=%d extra=%d\\n\", util_msg(), util_answer(), noinc_value());\n"
        "    return 0;\n}\n"
    ),
    "util.c": (
        "#include \"util.h\"\n"
        "int util_answer(void) { return UTIL_ANSWER; }\n"
        "const char *util_msg(void) { return UTIL_MSG; }\n"
    ),
    "util.h": P1_UTIL_H_V1,
    "noinc.c": "#include \"noinc.h\"\nint noinc_value(void) { return 7; }\n",
    "noinc.h": "#ifndef NOINC_H\n#define NOINC_H\nint noinc_value(void);\n#endif\n",
}

# ---------------------------------------------------------------- project P2 (shadowing)
P2_BASE = {
    "main.c": (
        "#include <stdio.h>\n#include \"util.h\"\n#include \"sub/helper.h\"\n"
        "int main(void) {\n"
        "    printf(\"main=%s helper=%s\\n\", TAG, helper_tag());\n"
        "    return 0;\n}\n"
    ),
    "util.h": "#ifndef UTIL_H\n#define UTIL_H\n#define TAG \"root\"\n#endif\n",
    "sub/helper.h": "#ifndef HELPER_H\n#define HELPER_H\nconst char *helper_tag(void);\n#endif\n",
    "sub/helper.c": (
        "#include \"helper.h\"\n#include \"util.h\"\n"
        "const char *helper_tag(void) { return TAG; }\n"
    ),
}
P2_SHADOW = dict(P2_BASE)
P2_SHADOW["sub/util.h"] = "#ifndef SUB_UTIL_H\n#define SUB_UTIL_H\n#define TAG \"sub\"\n#endif\n"


def main() -> int:
    client = httpx.Client(base_url=BASE)
    tmpdir = tempfile.mkdtemp(prefix="cbuild-e2e-")
    os.chdir(tmpdir)

    r = client.get("/healthz", timeout=10)
    check("healthz", r.status_code == 200 and r.json()["status"] == "ok")

    stats0 = client.get("/stats", timeout=10).json()

    # 1. fresh build: all units compiled
    r = post_build(client, P1)
    check("P1 first build -> 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    c, u, n = counts(r)
    check("P1 first build compiles all 3 units", (c, u, n) == (3, 0, 3), f"compiled={c} reused={u} units={n}")
    out1 = run_exe(r.content)
    check("P1 executable runs", out1 == "msg=v1 answer=42 extra=7", f"output={out1!r}")

    # 2. identical resubmission: everything reused
    r = post_build(client, P1)
    c, u, n = counts(r)
    check("P1 resubmission fully reused", r.status_code == 200 and (c, u) == (0, 3), f"compiled={c} reused={u}")

    # 3. header change: only units including util.h recompile
    p1_v2 = dict(P1)
    p1_v2["util.h"] = P1_UTIL_H_V2
    r = post_build(client, p1_v2)
    c, u, n = counts(r)
    check("P1 util.h change recompiles affected units only",
          r.status_code == 200 and (c, u) == (2, 1), f"compiled={c} reused={u}")
    out2 = run_exe(r.content)
    check("P1 rebuilt executable reflects new header",
          out2 == "msg=v2 answer=43 extra=7", f"output={out2!r}")

    # 4. reverting the header hits the old cache entries again
    r = post_build(client, P1)
    c, u, n = counts(r)
    check("P1 revert to v1 fully reused", r.status_code == 200 and (c, u) == (0, 3), f"compiled={c} reused={u}")

    # 5. same-name header shadowing changes include resolution
    r = post_build(client, P2_BASE)
    c, u, n = counts(r)
    check("P2 first build compiles both units", r.status_code == 200 and (c, u, n) == (2, 0, 2),
          f"compiled={c} reused={u} units={n}")
    out3 = run_exe(r.content)
    check("P2 resolves util.h to project root", out3 == "main=root helper=root", f"output={out3!r}")

    r = post_build(client, P2_SHADOW)
    c, u, n = counts(r)
    check("P2 adding sub/util.h recompiles only the shadowed unit",
          r.status_code == 200 and (c, u) == (1, 1), f"compiled={c} reused={u}")
    out4 = run_exe(r.content)
    check("P2 shadowed include takes effect", out4 == "main=root helper=sub", f"output={out4!r}")

    # 6. concurrent identical builds compile each unit exactly once
    nonce = os.urandom(8).hex()
    p3 = {
        "main.c": (
            f"/* {nonce} */\n"
            "int fa(void); int fb(void);\n"
            "#include <stdio.h>\n"
            "int main(void){ printf(\"%d\\n\", fa()+fb()); return 0; }\n"
        ),
        "a.c": f"/* {nonce} */\n#include \"shared.h\"\nint fa(void){{ return SHARED; }}\n",
        "b.c": f"/* {nonce} */\n#include \"shared.h\"\nint fb(void){{ return SHARED * 2; }}\n",
        "shared.h": f"/* {nonce} */\n#ifndef SHARED_H\n#define SHARED_H\n#define SHARED 5\n#endif\n",
    }
    s_before = client.get("/stats", timeout=10).json()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post_build(client, p3), range(8)))
    s_after = client.get("/stats", timeout=10).json()
    ok = all(r.status_code == 200 for r in responses)
    compiled_sum = sum(int(r.headers["X-Compiled-Count"]) for r in responses)
    delta_compiled = s_after["compiled_total"] - s_before["compiled_total"]
    waits = s_after["singleflight_waits_total"] - s_before["singleflight_waits_total"]
    check("8 concurrent identical builds all succeed", ok)
    check("each of the 3 units compiled exactly once globally",
          compiled_sum == 3 and delta_compiled == 3,
          f"sum(X-Compiled-Count)={compiled_sum} stats_delta={delta_compiled} singleflight_waits={waits}")
    check("concurrent executable output correct", run_exe(responses[0].content) == "15")

    # 6b. concurrent *different* projects (parallel CI branches) all succeed
    nonce2 = os.urandom(8).hex()
    p3b = {p: c + f"\n/* branch {nonce2} */\n" for p, c in p3.items()}
    mixed = [P1, p1_v2, P2_BASE, P2_SHADOW, p3b,
             {"main.c": "int main(void) { return broken(; }\n"}]
    with ThreadPoolExecutor(max_workers=6) as pool:
        mixed_resps = list(pool.map(lambda pf: post_build(client, pf), mixed))
    statuses = [r.status_code for r in mixed_resps]
    check("6 concurrent different builds behave independently",
          statuses == [200, 200, 200, 200, 200, 400], f"statuses={statuses}")

    # 7. compile failure: diagnostics returned, nothing cached
    cached_before = client.get("/stats", timeout=10).json()["cached_objects"]
    bad = {"main.c": "int main(void) { return does_not_exist(; }\n"}
    r = post_build(client, bad)
    detail = r.json().get("detail", {})
    check("syntax error -> 400 with compile diagnostics",
          r.status_code == 400 and detail.get("stage") == "compile"
          and "error" in detail.get("diagnostics", "").lower(),
          f"status={r.status_code} stage={detail.get('stage')}")
    cached_after = client.get("/stats", timeout=10).json()["cached_objects"]
    check("failed compile is not cached", cached_after == cached_before,
          f"cached_objects {cached_before} -> {cached_after}")

    # 8. link failure diagnostics
    r = post_build(client, {"foo.c": "int foo(void) { return 1; }\n"})
    detail = r.json().get("detail", {})
    check("missing main -> 400 with link diagnostics",
          r.status_code == 400 and detail.get("stage") == "link"
          and "main" in detail.get("diagnostics", ""),
          f"status={r.status_code} stage={detail.get('stage')}")

    # 9. validation
    r = post_build(client, {"../evil.c": "int x;\n"})
    check("path traversal rejected", r.status_code == 400
          and r.json().get("detail", {}).get("stage") == "validation")
    r = post_build(client, {"readme.txt": "hello\n"})
    check("non-C file rejected", r.status_code == 400)

    stats1 = client.get("/stats", timeout=10).json()
    print("\nstats before:", stats0)
    print("stats after: ", stats1)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
