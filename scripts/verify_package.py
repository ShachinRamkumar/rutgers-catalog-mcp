"""Check release archives and exercise installed catalog MCP executables outside the checkout."""

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent
TOOLS = {
    "rutgers-soc-mcp": {"SoC_Get_Course", "SoC_Search_Courses"},
    "middlesex-courses-mcp": {
        "Middlesex_Search_Options",
        "Middlesex_Search_Classes",
        "Middlesex_Class_Details",
    },
    "nj-transfer-mcp": {
        "NJTransfer_Find_Middlesex_Equivalents",
        "NJTransfer_Middlesex_Course",
        "NJTransfer_Search",
    },
}


def check_archives(wheel: Path, source: Path) -> None:
    allowed_source = {
        "src",
        "tests",
        "scripts",
        ".github",
        ".gitignore",
        ".dockerignore",
        "Dockerfile",
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "PKG-INFO",
    }
    machine_path = re.compile(
        rb"(?:/Users|/home)/[A-Za-z0-9._-]+/|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\"
    )

    def check_private_path(name: str) -> None:
        parts = Path(name).parts
        for part in parts:
            lower = part.lower()
            if (
                lower
                in {
                    ".git",
                    ".codex",
                    ".claude",
                    ".claude.json",
                    ".mcp.json",
                    ".env",
                    "config.toml",
                }
                or (lower.startswith(".env.") and lower != ".env.example")
                or lower.endswith(
                    (".local.json", ".local.env", "-.claude.json", "-config.toml", ".claude.json")
                )
            ):
                raise ValueError(f"Private state or client config in package: {name}")

    with tarfile.open(source) as archive:
        for member in archive.getmembers():
            check_private_path(member.name)
            parts = Path(member.name).parts
            if len(parts) > 1 and parts[1] not in allowed_source:
                raise ValueError(f"Unexpected source-archive path: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Unexpected archive link: {member.name}")
            if member.isfile():
                extracted = archive.extractfile(member)
                if extracted is not None and machine_path.search(extracted.read()):
                    raise ValueError(f"Machine-specific path in source archive: {member.name}")

    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            check_private_path(name)
            top = name.split("/")[0]
            if top not in {
                "rutgers_soc_mcp",
                "middlesex_courses_mcp",
                "nj_transfer_mcp",
            } and not top.endswith(".dist-info"):
                raise ValueError(f"Unexpected wheel path: {name}")
            if machine_path.search(archive.read(name)):
                raise ValueError(f"Machine-specific path in wheel: {name}")
    print("Wheel and source archive contain only project files and portable metadata.")


async def call(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    if result.is_error or result.structured_content is None:
        raise RuntimeError(f"{name} failed: {result.content}")
    print(f"Live tool passed: {name}")
    return result.structured_content


async def check_live(session: ClientSession, executable: str) -> None:
    now = datetime.now(ZoneInfo("America/New_York"))
    term = "Spring" if now.month <= 5 else "Summer" if now.month <= 7 else "Fall"
    semester = f"{term} {now.year}"
    if executable == "rutgers-soc-mcp":
        search = await call(
            session, "SoC_Search_Courses", {"query": "calculus", "semester": semester}
        )
        if not search["matches"]:
            raise RuntimeError("Rutgers live search returned no calculus courses")
        course = await call(
            session,
            "SoC_Get_Course",
            {
                "course_code": search["matches"][0]["course_code"],
                "semester": semester,
                "include_sections": True,
            },
        )
        records = course.get("variants", [course])
        if not all("course_data" in item and "next_section_offset" in item for item in records):
            raise RuntimeError("Rutgers live course omitted provenance or pagination")
    elif executable == "nj-transfer-mcp":
        search = await call(session, "NJTransfer_Search", {"query": "calculus"})
        if not search["matches"]:
            raise RuntimeError("NJ Transfer live search returned no calculus evaluations")
        exact = await call(
            session, "NJTransfer_Middlesex_Course", {"middlesex_course_code": "MAT132"}
        )
        reverse = await call(
            session, "NJTransfer_Find_Middlesex_Equivalents", {"rutgers_course_code": "01:640:152"}
        )
        for data in [exact, reverse]:
            if not data["matches"] or data["detail_errors"] or not data["details_expanded"]:
                raise RuntimeError("NJ Transfer live exact lookup did not expand details")
            if not all(item["detail_data"] for item in data["matches"]):
                raise RuntimeError("NJ Transfer omitted detail provenance")
    else:
        options = await call(session, "Middlesex_Search_Options", {})
        if not options["terms"]:
            raise RuntimeError("Middlesex live options returned no terms")
        search = await call(session, "Middlesex_Search_Classes", {"limit": 1})
        if not search["classes"]:
            raise RuntimeError("Middlesex current-term live search returned no sections")
        if search["returned"] != 1:
            raise RuntimeError("Middlesex did not honor the requested result limit")
        filters = {"term": search["term"]["code"], "subject": "CSC"}
        first = await call(session, "Middlesex_Search_Classes", {**filters, "limit": 10, "page": 1})
        second = await call(
            session, "Middlesex_Search_Classes", {**filters, "limit": 10, "page": 2}
        )
        combined = await call(
            session, "Middlesex_Search_Classes", {**filters, "limit": 20, "page": 1}
        )
        actual = [item["section_id"] for page in [first, second] for item in page["classes"]]
        expected = [item["section_id"] for item in combined["classes"]]
        if actual != expected or len(actual) != len(set(actual)):
            raise RuntimeError("Middlesex pagination skipped or duplicated sections")
        print("Live Middlesex pagination passed across result sizes.")
        await call(
            session, "Middlesex_Class_Details", {"section_id": search["classes"][0]["section_id"]}
        )


async def check_servers(bin_dir: Path, temp: Path, live: bool) -> None:
    for executable, expected in TOOLS.items():
        command = bin_dir / (executable + (".exe" if os.name == "nt" else ""))
        params = StdioServerParameters(
            command=str(command),
            cwd=str(temp),
            env={
                "RUTGERS_SOC_CACHE_DIR": str(temp / "soc-cache"),
                "NJ_TRANSFER_CACHE_DIR": str(temp / "transfer-cache"),
            },
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=120) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
            actual = {tool.name for tool in result.tools}
            if actual != expected:
                raise RuntimeError(f"{executable}: expected {expected}, got {actual}")
            print(f"Installed server passed: {executable}, {len(actual)} tools")
            if live:
                await check_live(session, executable)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    wheels = list(args.dist_dir.glob("*.whl"))
    sources = list(args.dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error("Build into a clean dist directory first with uv build.")
    uv = shutil.which("uv")
    if not uv:
        parser.error("uv is not on PATH")
    check_archives(wheels[0], sources[0])
    with tempfile.TemporaryDirectory(prefix="rutgers-catalog-mcp-pkg-") as directory:
        temp = Path(directory)
        venv = temp / "installed"
        subprocess.run([uv, "venv", "--python", sys.executable, str(venv)], check=True)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        requirements = subprocess.run(
            [uv, "export", "--locked", "--no-dev", "--no-emit-project", "--no-hashes"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        requirements_path = temp / "requirements.txt"
        requirements_path.write_text(requirements, encoding="utf-8")
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "-r",
                str(requirements_path),
                str(wheels[0]),
            ],
            check=True,
        )
        asyncio.run(check_servers(bin_dir, temp, args.live))


if __name__ == "__main__":
    main()
