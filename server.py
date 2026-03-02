"""
Lightweight server that accepts Python code from a browser-based editor, executes it inside a Modal
sandbox with `kernprof` (`line_profiler`), and returns the profiling results (all within a 10-second
execution limit).
"""

import base64
import textwrap
import traceback

from dotenv import load_dotenv

# Load Modal credentials from `.env` before importing Modal; the SDK reads these environment
# variables on import to authenticate.
load_dotenv()

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import modal  # noqa: E402

app = FastAPI()

# Modal image with `line_profiler` pre-installed; cached after the first build, so only the initial
# invocation pays the image-construction cost.
sandbox_image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "line_profiler"
)


class ProfileRequest(BaseModel):
    code: str


@app.get("/")
def serve_frontend():
    return FileResponse("index.html")


@app.post("/api/profile")
def run_profiler(req: ProfileRequest):
    """
    Run user code with kernprof inside a 10-second Modal sandbox and
    return stdout (profiler output) along with any stderr.
    """
    try:
        # Base64-encode the user's code so it survives shell escaping intact.
        encoded = base64.b64encode(req.code.encode()).decode()

        # Wrapper that runs inside the sandbox: Decode the submitted code, write it to a file, and
        # invoke `kernprof` for line-by-line profiling.
        wrapper = textwrap.dedent(f"""\
            import base64, subprocess, sys
            code = base64.b64decode("{encoded}").decode()
            with open("/root/script.py", "w") as f:
                f.write(code)
            result = subprocess.run(
                ["kernprof", "-l", "-v", "/root/script.py"],
                capture_output=True, text=True,
            )
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            sys.exit(result.returncode)
        """)

        modal_app = modal.App.lookup("line-profiler-sandbox", create_if_missing=True)
        sb = modal.Sandbox.create(
            "python",
            "-c",
            wrapper,
            image=sandbox_image,
            timeout=10,
            app=modal_app,
        )
        sb.wait()

        # Drain both output streams; `stdout` carries profiler results while `stderr` carries
        # runtime errors or warnings from the user's code.
        stdout = sb.stdout.read()
        stderr = sb.stderr.read()
        exit_code = sb.returncode

        return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}

    except Exception as exc:
        msg = str(exc)
        # Modal raises a specific error when the sandbox exceeds its limit.
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            return JSONResponse(
                status_code=408,
                content={"error": "Sandbox exceeded the 10-second execution limit."},
            )
        return JSONResponse(
            status_code=500,
            content={"error": msg, "traceback": traceback.format_exc()},
        )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
