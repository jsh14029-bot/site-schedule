"""Distribute only PC bridge source files, never machine-specific credentials."""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

PC_FILES = (
    "kakao_local_agent.py", "kakao_pc_reader.py", "kakao_sync.py",
    "schedule_autofill.py", "requirements-pc.txt", "install_kakao_pc.bat",
    "start_kakao_pc.bat", "카카오톡_직접연결_안내.md",
)


def build_pc_package(root=None):
    root = Path(root) if root else Path(__file__).resolve().parent
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name in PC_FILES:
            data = (root / name).read_bytes()
            # cmd.exe batch files need Windows line endings after ZIP extraction.
            if name.endswith(".bat"):
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            archive.writestr("kakao-pc/" + name, data)
    return output.getvalue()


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "dist" / "kakao-pc.zip"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(build_pc_package())
    print(target)
