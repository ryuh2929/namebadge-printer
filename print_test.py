"""Brother TD-4410D 래스터 명령 테스트 출력.

근거 문서: docs/래스터 명령 참조.pdf (Raster Command Reference v1.03)
  - 4.1 Print data overview      : 전송 순서 (초기화 → 제어코드 → 래스터 → 인쇄명령)
  - 4.3.2 / 4.3.5 Page size, Raster line : 용지별 도트 수, 왼쪽 여백 핀 수
  - 5. Status                    : 32바이트 상태 응답
  - 7. Printing Command Details  : 각 명령 바이트

사용 예:
  uv run print_test.py --backend file                 # 맥: test.bin + test.png 만 생성
  uv run print_test.py --backend usb --status-only    # USB 직접 연결, 상태만 읽기
  uv run print_test.py --backend usb                  # USB 직접 연결로 출력
  uv run print_test.py --backend win                  # 윈도우: 설치된 드라이버 큐에 RAW 전송
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DPI = 203
HEAD_PINS = 832  # 4.3.5: 203dpi 모델 총 핀 수
LINE_BYTES = HEAD_PINS // 8  # 104바이트 (g 명령의 n 값)
INVALIDATE_BYTES = 350  # 7. NULL Invalidate: TD-4410D는 350바이트
BROTHER_VID = 0x04F9
WINDOWS_PRINTER_NAME = "Brother TD-4410D"  # 4.2.1 레지스트리 표의 드라이버 이름


@dataclass(frozen=True)
class Media:
    die_cut: bool
    width_mm: int  # ESC i z n3 / 상태 응답 media width
    length_mm: int  # ESC i z n4 (연속 용지는 0)
    left_pins: int  # 4.3.5 "Number of Pins for Left Margin"
    area_pins: int  # 4.3.5 "Number of Print Area Pins"
    area_lines: int  # 4.3.2 "Print area length" (연속 용지는 --length-mm로 결정)


# 4.3.2 (b) Die-cut labels / 4.3.5 Die-cut labels 표 (203 dpi)
# 4.3.2 (a) Continuous length tape / 4.3.5 Continuous length tape 표 (203 dpi)
MEDIA = {
    "102x152": Media(True, 102, 152, 22, 788, 1170),
    "102x50": Media(True, 102, 50, 22, 788, 351),
    "76x26": Media(True, 76, 26, 124, 585, 156),
    "51x26": Media(True, 51, 26, 225, 382, 156),
    "rd102": Media(False, 102, 0, 22, 788, 0),
    "rd90": Media(False, 90, 0, 69, 695, 0),
    "rd76": Media(False, 76, 0, 125, 583, 0),
    "rd58": Media(False, 58, 0, 196, 440, 0),
}
CONTINUOUS_MARGIN_DOTS = 24  # 4.3.3: 연속 용지 최소 여백 3mm = 24 dots

FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]


# ---------------------------------------------------------------- 이미지

def load_font(size: int, path: str | None) -> ImageFont.FreeTypeFont:
    for p in [path] if path else FONT_CANDIDATES:
        if p and Path(p).exists():
            return ImageFont.truetype(p, size)
    sys.exit("한글 폰트를 찾지 못했습니다. --font 로 .ttf/.ttc 경로를 지정하세요.")


def render_sample(width: int, height: int, name: str, org: str, font_path: str | None) -> Image.Image:
    """인쇄 영역 크기(도트) 그대로의 흑백 명찰 샘플. 1픽셀 = 1도트."""
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width - 1, height - 1], outline=0, width=4)
    name_font = load_font(int(height * 0.32), font_path)
    org_font = load_font(int(height * 0.13), font_path)
    d.text((width / 2, height * 0.45), name, font=name_font, fill=0, anchor="mm")
    d.text((width / 2, height * 0.80), org, font=org_font, fill=0, anchor="mm")
    # 방향 확인용: 왼쪽 위 모서리 표시 (출력물이 좌우반전인지 확인)
    d.polygon([(4, 4), (60, 4), (4, 60)], fill=0)
    return img.convert("1", dither=Image.Dither.NONE)


def to_raster_lines(img: Image.Image, media: Media, mirror: bool) -> list[bytes]:
    """832핀 헤드 기준 한 줄(104바이트)씩. 비트 1 = 검정(점 찍힘)."""
    canvas = Image.new("1", (HEAD_PINS, img.height), 1)  # 1 = 흰색
    canvas.paste(img, (media.left_pins, 0))
    if mirror:
        canvas = canvas.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    data = bytes(b ^ 0xFF for b in canvas.tobytes())  # PIL은 흰색=1 이라 반전
    return [data[i : i + LINE_BYTES] for i in range(0, len(data), LINE_BYTES)]


# ---------------------------------------------------------------- 명령

def build_job(lines: list[bytes], media: Media, autocut: bool) -> bytes:
    n = len(lines)
    out = bytearray()
    # (1) 초기화 명령
    out += b"\x00" * INVALIDATE_BYTES  # NULL Invalidate
    out += b"\x1b\x40"  # ESC @ Initialize
    # (2) 제어 코드
    out += b"\x1b\x69\x61\x01"  # ESC i a 1 : 래스터 모드로 전환
    out += b"\x1b\x69\x21\x00"  # ESC i ! 0 : 자동 상태 알림 켬
    out += bytes([
        0x1B, 0x69, 0x7A,  # ESC i z : Print information
        0x00,  # n1 valid flag (문서 예시와 동일하게 0 → 프린터에 설정된 용지 사용)
        0x0B if media.die_cut else 0x0A,  # n2 media type
        media.width_mm, media.length_mm,  # n3, n4
        n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF,  # n5~n8 래스터 줄 수
        0x00,  # n9 starting page
        0x00,  # n10 fixed
    ])
    out += b"\x1b\x69\x4d" + bytes([0x40 if autocut else 0x00])  # ESC i M : bit6 자동 커팅
    margin = 0 if media.die_cut else CONTINUOUS_MARGIN_DOTS  # 정형 라벨은 0 (4.3.3)
    out += b"\x1b\x69\x64" + bytes([margin & 0xFF, margin >> 8])  # ESC i d : 여백
    out += b"\x4d\x00"  # M 0 : 압축 안 함
    # (3) 래스터 데이터
    for line in lines:
        if any(line):
            out += b"\x67\x00" + bytes([LINE_BYTES]) + line  # g 00 104 <104바이트>
        else:
            out += b"\x5a"  # Z : 빈 줄
    # (4) 인쇄 명령
    out += b"\x1a"  # Control-Z : 마지막 페이지 인쇄 + 배출
    out += b"\x1b\x69\x61\xff"  # ESC i a FF : 기본 명령 모드로 복귀
    return bytes(out)


STATUS_REQUEST = b"\x00" * INVALIDATE_BYTES + b"\x1b\x40" + b"\x1b\x69\x53"  # ESC i S


def parse_status(s: bytes) -> str:
    """5.1 Status overview (32바이트)."""
    if len(s) != 32 or s[0] != 0x80 or s[2] != 0x42:
        return f"알 수 없는 응답: {s.hex(' ')}"
    err1 = {0x02: "용지 없음", 0x04: "커터 걸림", 0x20: "전원 꺼짐"}
    err2 = {0x02: "버퍼 가득참", 0x04: "통신 오류", 0x10: "커버 열림", 0x40: "용지 급지 불가"}
    errors = [v for k, v in err1.items() if s[8] & k] + [v for k, v in err2.items() if s[9] & k]
    mtype = {0x4A: "연속 용지", 0x4B: "정형 라벨"}.get(s[11], f"0x{s[11]:02x}")
    stype = {0: "상태 응답", 1: "인쇄 완료", 2: "오류", 4: "전원 꺼짐", 5: "알림", 6: "단계 변경"}.get(s[18], f"0x{s[18]:02x}")
    model = "TD-4410D" if (s[3], s[4]) == (0x35, 0x37) else f"series={chr(s[3])} model={chr(s[4])}"
    return (
        f"모델={model} 상태={stype} 용지={mtype} {s[10]}mm x {s[17]}mm "
        f"오류={', '.join(errors) or '없음'}"
    )


# ---------------------------------------------------------------- 전송

def usb_open():
    import usb.core
    import usb.util

    dev = usb.core.find(idVendor=BROTHER_VID)
    if dev is None:
        sys.exit("USB에서 Brother 프린터를 찾지 못했습니다. (macOS: brew install libusb 필요)")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    dev.set_configuration()
    intf = dev.get_active_configuration()[(0, 0)]
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    print(f"USB 연결: {dev.manufacturer} {dev.product} (PID 0x{dev.idProduct:04x})")
    return ep_out, ep_in


def usb_read_status(ep_in, timeout_ms: int = 2000) -> bytes | None:
    import usb.core

    try:
        return bytes(ep_in.read(32, timeout_ms))
    except usb.core.USBTimeoutError:
        return None


def send_usb(job: bytes | None) -> None:
    ep_out, ep_in = usb_open()
    # 3장 흐름: (2) 상태 확인 → (3) 데이터 전송 → (5) 완료 상태 확인
    ep_out.write(STATUS_REQUEST)
    status = usb_read_status(ep_in)
    print("상태:", parse_status(status) if status else "응답 없음")
    if job is None:
        return
    ep_out.write(job)
    for _ in range(15):  # 단계 변경 → 인쇄 완료 순서로 들어옴
        status = usb_read_status(ep_in, 3000)
        if status is None:
            continue
        print("상태:", parse_status(status))
        if status[18] in (0x01, 0x02):  # 인쇄 완료 / 오류
            break


def send_windows(job: bytes, printer: str) -> None:
    import win32print  # pywin32

    h = win32print.OpenPrinter(printer)
    try:
        win32print.StartDocPrinter(h, 1, ("TD-4410D raster test", None, "RAW"))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, job)
        win32print.EndPagePrinter(h)
        win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)
    print(f"'{printer}' 큐로 {len(job)}바이트 RAW 전송 완료")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["file", "usb", "win"], default="file")
    ap.add_argument("--media", choices=MEDIA, default="102x50", help="장착된 용지 (기본: 102x50 정형 라벨)")
    ap.add_argument("--length-mm", type=float, default=50, help="연속 용지(rd*)일 때 라벨 길이")
    ap.add_argument("--name", default="홍길동")
    ap.add_argument("--org", default="AJA School")
    ap.add_argument("--font", help="폰트 파일 경로 (기본: 맑은 고딕 / Apple SD 고딕 Neo)")
    ap.add_argument("--mirror", action="store_true", help="출력이 좌우 반전되어 나오면 켜기")
    ap.add_argument("--autocut", action="store_true", help="커터 옵션 장착 시")
    ap.add_argument("--printer", default=WINDOWS_PRINTER_NAME, help="윈도우 프린터 이름")
    ap.add_argument("--status-only", action="store_true", help="usb 백엔드: 상태만 읽기")
    args = ap.parse_args()

    if args.status_only:
        send_usb(None)
        return

    media = MEDIA[args.media]
    height = media.area_lines or round(args.length_mm / 25.4 * DPI) - 2 * CONTINUOUS_MARGIN_DOTS
    img = render_sample(media.area_pins, height, args.name, args.org, args.font)
    job = build_job(to_raster_lines(img, media, args.mirror), media, args.autocut)
    print(f"용지 {args.media}: 인쇄 영역 {media.area_pins} x {height} 도트, 작업 {len(job)}바이트")

    if args.backend == "file":
        img.save("test.png")
        Path("test.bin").write_bytes(job)
        print("test.png(미리보기), test.bin(프린터로 보낼 바이트) 저장")
    elif args.backend == "usb":
        send_usb(job)
    else:
        send_windows(job, args.printer)


if __name__ == "__main__":
    main()
