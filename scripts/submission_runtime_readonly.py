"""Start an existing private runtime in a fresh temporary directory and check reads.

Never uses a production URL or the workspace's databases. Credentials are read
from the copied ACCESS.md and remain in memory. No model/API calls or load tests.
"""
from __future__ import annotations
import argparse
import hashlib
import http.cookiejar
import io
import json
import os
import secrets
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def extract(archive, target):
    for member in archive.getmembers():
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise ValueError("Unsafe runtime member")
    archive.extractall(target, filter="data")


def verify(version, python, release_dir, source_ref=None):
    finance = ROOT.name.startswith("issuertrace")
    archive_name = f"issuertrace-runtime-{version}.tar.gz" if finance else f"courseloop-{version}-runtime.tar.gz"
    runtime = release_dir / archive_name
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    expected = next(line.split()[0] for line in (release_dir / "SHA256SUMS").read_text().splitlines()
                    if line.split()[-1] == archive_name)
    assert runtime_sha == expected
    with tempfile.TemporaryDirectory(prefix=ROOT.name + "-readonly-") as temp:
        target = Path(temp)
        if finance:
            source = subprocess.check_output(["git", "archive", source_ref or version], cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(source)) as archive:
                extract(archive, target)
        with tarfile.open(runtime) as archive:
            extract(archive, target)
        root = target if finance else target / "courseloop-submission"
        rows = []
        for line in (root / "ACCESS.md").read_text().splitlines():
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) == 4 and parts[0].startswith("competition-"):
                rows.append(dict(username=parts[0], role=parts[1], password=parts[2]))
        row = next(r for r in rows if r["role"] == ("reviewer" if finance else "student"))
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(("CL_", "LLM_", "ASSISTANT_", "AUTH_", "SUBMISSION_")) or key == "PYTHONPATH":
                env.pop(key)
        env["AUTH_SECRET"] = secrets.token_hex(32)
        env["CL_SECRET_KEY"] = secrets.token_hex(32)
        env["CL_RAG_MODE"] = "fts5"
        # Ask the kernel for a free loopback port; startup timeout handles a race.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        app = "api.main:app" if finance else "scripts.submission_serve:app"
        with (target / "server.log").open("wb") as log:
            process = subprocess.Popen([str(python), "-m", "uvicorn", app, "--host", "127.0.0.1",
                                        "--port", str(port), "--workers", "1"],
                                       cwd=root, env=env, stdout=log, stderr=log)
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            checks = []
            try:
                for _ in range(120):
                    if process.poll() is not None:
                        raise RuntimeError("Copied runtime exited during startup; no production action taken")
                    try:
                        opener.open(base + "/api/health", timeout=1).close()
                        break
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(.25)
                else:
                    raise RuntimeError("Copied runtime health timeout")
                request = urllib.request.Request(base + "/api/auth/login",
                         data=json.dumps({"username": row["username"], "password": row["password"]}).encode(),
                         headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=10) as response:
                    body = json.load(response)
                    checks.append({"path": "/api/auth/login", "status": response.status})
                headers = {} if finance else {"Authorization": "Bearer " + body["token"]}
                paths = (["/", "/api/health", "/api/overview", "/api/events?limit=1", "/api/risks?limit=1",
                          "/api/search?q=募集资金&limit=1", "/api/benchmark/summary"] if finance else
                         ["/", "/api/health", "/api/courses", "/api/problems?course=ds-algo",
                          "/api/knowledge?course=os", "/api/auth/profile"])
                for path in paths:
                    url = base + urllib.parse.quote(path, safe="/?=&")
                    with opener.open(urllib.request.Request(url, headers=headers), timeout=15) as response:
                        response.read()
                        checks.append({"path": path, "status": response.status})
                return {"passed": True, "version": version, "runtime_sha256": runtime_sha,
                        "environment": "fresh temporary runtime; loopback; login and GET only",
                        "checks": checks, "model_calls": 0, "production_traffic": 0}
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--source-ref", help="Candidate source revision; published verification defaults to the version tag")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    # Preserve the venv's python symlink: resolving it selects the base Python
    # and loses the tested virtual environment's dependencies.
    result = verify(args.version, args.python.absolute(), args.release_dir.resolve(), args.source_ref)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
