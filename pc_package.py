"""Deliver only the built installer; never include PC tokens or database files."""
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

INSTALLER_NAME = "KakaoPC_Setup.exe"
INSTRUCTIONS = """현장일정관리 카카오톡 PC 연결

KakaoPC_Setup.exe 실행 → 설치 시작 → 설치 완료 후 실행하기
Python 별도 설치는 필요하지 않습니다. Windows 10/11 64비트용입니다.
연결코드 페이지의 코드를 사이트의 PC 연결코드 칸에 입력하세요.
다음부터 바탕화면의 '현장일정 카카오톡 연결' 바로가기로 실행하세요.
카카오톡 '일정공유방☆'을 별도 창으로 열고 사이트에서 가져오기를 누릅니다.
연결코드는 다른 사람과 공유하지 마세요.
"""


@lru_cache(maxsize=2)
def _read_installer(path, modified_ns, size):
    data = Path(path).read_bytes()
    if len(data) != size or data[:2] != b"MZ":
        raise ValueError("설치 EXE 파일이 올바르지 않습니다.")
    return data


def get_pc_installer(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    path = (root / INSTALLER_NAME).resolve()
    stat = path.stat()
    # Cache installer bytes only. A replacement file invalidates this cache.
    return _read_installer(str(path), stat.st_mtime_ns, stat.st_size)


def build_pc_package(root=None):
    """Compatibility for callers that still request a ZIP download."""
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(INSTALLER_NAME, get_pc_installer(root))
        archive.writestr("설치방법.txt", INSTRUCTIONS.encode("utf-8-sig"))
    return output.getvalue()


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "dist" / "kakao-pc.zip"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(build_pc_package())
    print(target)
